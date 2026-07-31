# 充电桩问题排查标准作业SOP（完整版）
## 文档说明
1. 文档用途：充电桩订单异常、计费错误、设备停机、通讯故障统一标准化排查流程，适用于开发、运维、售后人员
2. 数据源：MySQL业务库`cloud_charging_pile`、TDengine时序库（设备实时状态+原始报文）
3. 适用协议：云快充YKC1.6/YKC1.8/YKC2.1、OCPP设备
4. 核心排查对象：充电订单`ch_order_info`、设备实时时序`charging-gun_property`、桩原始通讯报文`charging-pile_comm`、硬件上报交易数据`tx_data`

# 一、前置准备（排查前必做）
## 1.1 权限与工具清单
1. MySQL数据库账号：拥有`cloud_charging_pile`查询权限，禁止UPDATE/DELETE生产数据
2. TDengine查询账号：可查询`charging-gun_property`、`charging-pile_comm`超级表
3. 辅助工具：Redis客户端（查看同步队列`third.order.sync.queue`）、防火墙端口放行工具、设备后台管理页
4. 必备信息收集（用户反馈故障时优先收集）
    - 订单编号`order_no`
    - 设备编码`device_code`、枪编码`child_device_code`
    - 故障现象：金额异常/电量不准/中途停机/启动失败/设备离线
    - 故障时间范围：订单创建时间、停止时间
    - 停止原因（如有）、车辆VIN、场地ID

## 1.2 数据源结构速查（原有内容整理精简）
### 1.2.1 MySQL 订单表 ch_order_info
库：`cloud_charging_pile`，核心关键字段：
| 字段 | 作用 |
| ---- | ---- |
| order_no | 订单唯一编号，排查主索引 |
| device_code/child_device_code | 桩+枪编号，用于查TD时序 |
| tx_data | JSON，硬件上报原始交易电量、费用、停机原因 |
| stopped_reason_code/content | 停机编码+文字描述 |
| electricity/electricity_fee/service_fee | 平台计算电量、电费、服务费 |
| status | 0充电中 1充电结束 2充电异常 |
| pay_amount/total_amount | 用户实际支付金额 |
| created_time/stop_time | 订单起止时间，TD查询时间区间 |
| device_protocol | YKC云快充 / OCPP，区分设备协议 |

### 1.2.2 tx_data 硬件上报交易JSON
硬件充电结束主动上报的原始计费数据，**计费异常第一核对对象**
核心字段：`electricityQuantity`总电量、`tip/peak/flat/valley`分时电量/单价/费用、`totalFee`硬件总费用、`stoppedReasonCode`停机码、`meterBeginValue/meterEndValue`电表起止读数

### 1.2.3 TDengine 时序表
1. `charging-gun_property`：充电过程实时快照（电压、电流、功率、SOC、温度、故障码、实时电量）
   关联字段：`txSerialNo`交易流水号、`device`设备编码、`_ts`上报时间
2. `charging-pile_comm`：桩与平台原始上下行报文
   tag：`device`设备编号；direction=1下行(平台下发)、2上行(桩上报)；code协议指令码

# 二、通用标准化排查总流程（全故障通用步骤）
## Step1 根据订单号查询MySQL基础订单数据
```sql
SELECT * FROM cloud_charging_pile.ch_order_info WHERE order_no = '输入订单号';
```
核查要点：
1. 订单状态status：充电中/已结束/异常
2. 提取关键参数：device_code、child_device_code、txSerialNo、created_time、stop_time、device_protocol、tx_data
3. 确认`is_receive_tx_data`：0=未收到硬件上报交易数据，1=正常接收
4. 记录停机编码`stopped_reason_code`、异常信息`error_info`

## Step2 核对硬件上报tx_data与平台订单计算数据
### 2.2.1 判定标准
平台计费逻辑：
总电费 = 分时尖峰平谷电费求和
服务费 = 硬件上报totalFee - 总电费
> 异常判定：服务费 ≤ 0 大概率是硬件上报电量/金额异常

核对项：
1. tx_data总电量`electricityQuantity` 和订单表`electricity`是否一致
2. tx_data`totalFee` 和订单`electricity_fee + service_fee`总和是否匹配
3. 分时尖/峰/平/谷电量、单价是否符合场地计费模板

### 2.2.2 分根因判断
1. tx_data为空 / is_receive_tx_data=0：桩未上报交易数据，走「通讯丢失排查分支」
2. tx_data电量/金额为负数、超大值、0：硬件上报异常，走「设备上报故障分支」
3. tx_data正常，但平台计算金额偏差：计费模板、时段匹配

## Step3 TDengine查询时序过程数据（定位充电过程异常）
### 3.3.1 查询单订单全流程实时枪状态
```sql
SELECT * FROM `charging-gun_property` WHERE txSerialNo = '交易流水号';
```
核查内容：
1. status状态流转：2空闲→3充电中→2空闲，是否存在中途切故障1
2. power功率、voltage电压、current电流是否断崖下跌（断电/枪脱离）
3. temperature、bms温度是否超阈值（高温停机）
4. errorCode故障码、soc电量变化、实时累计电量是否正常

### 3.3.2 查询设备原始通讯报文（时序区间取自订单created_time~stop_time）
```sql
SELECT * FROM `charging-pile_comm`
WHERE device='设备编码'
AND _ts >= '2026-01-01 00:00:00'
AND _ts <= '2026-01-01 01:00:00';
```
排查点：
1. 上行报文是否缺失：桩没上报充电数据
2. 下行指令是否下发成功：平台下发停机、启动指令桩无应答
3. 协议code报错报文（云快充13/3b指令、OCPP标准报错）

## Step4 Redis消息队列排查（订单同步、消息丢失场景）
队列Key：`third.order.sync.queue`
适用场景：订单生成但下游商城/对账系统无数据、同步延迟
1. 查看队列全部消息
```redis
XRANGE third.order.sync.queue - + COUNT 100
```
2. 消费组说明：`mall.third.order.sync.group`，lag=0代表无未消费消息
3. 问题判定：
    - 订单同步消息存在队列：下游消费者阻塞，需手动消费/重置游标
    - 队列无对应订单消息：订单发送逻辑异常，服务未推送消息

## Step5 根因分类判定 & 对应处理方案
# 三、分场景专项排查SOP
## 场景1：订单电量/金额异常（负数、0、金额偏差、服务费为0）
1. 执行Step1拉取订单，提取tx_data
2. 对比tx_data分时电量、totalFee与平台计算结果
3. 分支A：tx_data数值异常
    - TD查询报文，确认桩上报报文原始字段是否本身错误
    - 处理：联系硬件厂商调试桩计量模块，订单走人工对账补偿流程
4. 分支B：tx_data数据正常，平台计算偏差
    - 核对场地fee_template_id计费模板时段、单价配置
    - 核对电损ds_electricity相关计算逻辑
    - 处理：修复计费配置/代码，后台重算订单金额
5. 分支C：未收到tx_data（is_receive_tx_data=0）
    - 查看TD报文：充电结束后桩未上传交易报文
    - 处理：检查桩网络、设备协议版本，补推交易数据

## 场景2：充电中途异常停机（status=2，异常订单）
1. Step1查询`stopped_reason_code`停机编码
2. TD时序表查看停机前1分钟快照：温度、功率、故障码
3. TD报文查询停机上行告警报文
4. 常见根因：
    - 高温停机：电池/枪线温度超阈值 → 场地散热、检修充电桩
    - 余额不足：balance_insufficient_stop=1 → 用户充值/调整预冻结规则
    - 硬件故障：errorCode存在故障码 → 设备售后检修
    - 主动拔枪：is_insert=0 → 用户操作问题，无需处理

## 场景3：扫码/刷卡启动充电失败
1. 核对订单launch_type启动类型、card_id/mac启动参数
2. TD报文查询平台下发启动指令、桩上行应答报文
3. 排查方向：设备离线、协议版本不兼容、枪未归位is_return=0、场地权限白名单white_flag

## 场景4：设备离线、通讯中断
1. TD时序`charging-gun_property`最新上报时间，长时间无数据判定离线
2. 报文表无上下行数据：桩网络故障（4G/网线）
3. 服务器侧排查：防火墙6041端口是否放行、设备接入服务运行状态

## 场景5：订单同步下游丢失（商城对账无数据）
1. Redis Stream查看`third.order.sync.queue`是否存在该订单同步消息
2. 消费组lag>0代表消息堆积，lag=0代表已全部消费
3. 处理：
    - 消息存在队列：临时手动消费；长期阻塞重启消费者
    - 队列无消息：服务发送同步消息逻辑异常，补发同步事件

# 四、线上操作规范&风险禁止事项
1. 禁止直接UPDATE/DELETE生产ch_order_info订单数据
2. TDengine查询一定要加上设备和时间的筛选条件，避免查询超时影响数据库性能
3. 批量查询线上SQL必须加LIMIT，禁止全表扫描

# 五、排查闭环归档要求
故障排查完成后工单必须记录以下信息：
1. 故障订单号、设备编号、故障现象
2. 数据源核查结果：MySQL订单、TD时序、tx_data、报文关键截图
3. 根因分类：硬件上报异常/平台计费bug/通讯故障/用户操作/配置错误
4. 处理动作：人工对账、修复配置、厂商检修、补发同步消息等
5. 预防优化方案（如调整计费模板、升级桩协议、优化消息消费）

# 六、辅助附录
## 附录1：常用一键查询SQL/Redis命令
### MySQL
```sql
-- 按设备查当日所有异常订单
SELECT order_no,stopped_reason_code,error_info FROM ch_order_info
WHERE device_code='xxx' AND status=2 AND DATE(created_time) = CURDATE();
```
### TDengine
```sql
-- 查询设备当日全部原始报文
SELECT * FROM `charging-pile_comm` WHERE device='xxx' AND _ts >= 'created_time' and _ts<='stop_time';
```
### Redis Stream
```redis
# 查看异步订单是否有消息堆积
XLEN  third.order.sync.queue
XLEN  third.order.sync.notify.queue
```

## 附录2：停机原因编码快速说明
0：未知原因
余额类：余额不足停机编码
设备故障类：过温、过流、通讯失败
用户操作类：拔枪、手动停止、充满自停


## 附录3：名词释义
1. YKC：云快充私有协议，版本1.6/1.8/2.1
2. txSerialNo：桩本地交易流水号，时序表关联主键
3. 电损ds_xxx：平台配置损耗分摊电量/费用
4. lag：Redis Stream消费组未消费消息条数，lag=0无堆积
5. pending：消费组待确认消息，正常应为0