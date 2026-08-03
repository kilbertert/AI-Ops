# 充电桩问题排查SOP

## 1.前置数据结构说明

### 1.1 mysql

数据库：cloud_charging_pile

```sql
-- 充电订单表结构
create table ch_order_info
(
    id                              varchar(32)                     not null comment 'ID'
        primary key,
    order_no                        varchar(32)                     null comment '订单编号',
    user_id                         varchar(32)                     null comment '用户id',
    is_enterprise                   tinyint(1)     default 0        not null comment '是否为企业订单',
    wallet_type                     int                             null comment '使用钱包类型',
    enterprise_user_id              varchar(32)                     null comment '承运商、车队、企业会员id',
    team_price_strategy_id          varchar(32)                     null comment '车队定价id或电站活动id',
    team_price_strategy_type        int                             null comment '0:车队定价1:电站活动',
    team_verify_type                int                             null comment '车队策略校验方式',
    team_verify_result              int                             null comment '车队策略校验结果',
    card_id                         varchar(64)                     null comment '卡启动充电',
    card_key                        varchar(32)                     null comment 'ch_rfid_card表的主键,卡的唯一标识',
    hlht_no                         varchar(32)                     null comment '互联互通订单编号',
    operator_id                     varchar(32)                     null comment '互联互通商户id',
    device_id                       varchar(32)                     null comment '设备id',
    device_code                     varchar(32)                     null comment '设备编码',
    device_name                     varchar(128)                    null comment '订单设备名称快照',
    child_device_id                 varchar(32)                     not null comment '子设备id',
    child_device_code               varchar(32)                     null comment '枪编码',
    site_id                         varchar(32)                     null comment '所属场地id',
    partner_b_id                    varchar(32)                     null comment '代理商B端账户id',
    app_id                          varchar(64)                     null comment 'app_id',
    client_type                     varchar(32)                     null comment '客户端类型',
    electricity                     decimal(24, 6)                  null comment '充电电量',
    voltage                         int                             null comment '充电电压',
    time                            int                             null comment '充电时长(分钟)',
    issue_soc                       int                             null comment '下发soc',
    issue_amount                    decimal(16, 2)                  null comment '下发金额',
    work_order_no                   varchar(32)                     null comment '工单单号',
    consumer_id                     varchar(32)                     null comment '充电用户的id,工单场景',
    issue_energy                    double                          null comment '下发充电电量',
    issue_time                      int                             null comment '下发充电时长',
    biz_status                      tinyint                         null comment '业务状态',
    status                          tinyint                         null comment '状态(0:充电中 1:充电结束 2:充电异常)',
    package_id                      varchar(32)                     null comment '套餐id',
    billing_type                    int                             null comment '计费方式',
    launch_type                     varchar(32)                     null comment '启动类型',
    type                            int                             null comment '充电订单类型 0：四轮 1：2轮',
    zero_stop                       tinyint(1)                      null comment '是否充满自停',
    is_pre_pay                      int            default 0        not null comment '是否充电前支付,适用于套餐',
    is_test                         int            default 0        not null comment '是否测试订单',
    user_recharge_card_id           varchar(32)                     null comment '用户充值卡id',
    pre_pay_amount                  decimal(10, 2)                  null comment '提前支付的套餐金额',
    electricity_fee                 decimal(10, 2)                  null comment '充电费',
    ds_electric_fee                 decimal(24, 2) default 0.00     not null comment '损尖电费',
    service_fee                     decimal(10, 2)                  null comment '服务费',
    ds_service_fee                  decimal(24, 2) default 0.00     not null comment '电损尖费用',
    occupy_fee                      decimal(10, 2)                  null comment '占位费金额',
    launch_fee                      decimal(10, 2)                  null comment '启动费用',
    occupy_time                     int            default 0        not null comment '占位时长',
    park_fee                        decimal(10, 2) default 0.00     not null comment '停车费',
    appointment_fee                 decimal(10, 2)                  null comment '预约费用',
    appointment_order_id            varchar(32)                     null comment '关联核销的预约单id',
    fine                            decimal(10, 2)                  null comment '罚金',
    market_amount                   decimal(10, 2)                  null comment '营销优惠金额',
    discount_details                varchar(512)                    null,
    platform_amount                 decimal(10, 2)                  null comment '平台优惠金额',
    coupon_type                     int                             null comment '优惠券类型 1：平台券 2：店铺券',
    coupon_id                       varchar(32)                     null comment '优惠券id',
    buy_insurance                   tinyint(1)     default 0        not null comment '是否购买保险',
    insurance_amount                decimal(10, 3) default 0.000    not null comment '保险金额',
    insurance_pay_type              varchar(16)                     null comment '安心充支付方式',
    insurance_point_award           varchar(32)                     null comment '安心充点位奖',
    pay_amount                      decimal(10, 2)                  null comment '支付金额',
    pay_type                        varchar(16)                     null comment '支付方式',
    total_amount                    decimal(10, 2)                  null comment '订单总金额',
    reduce_balance                  decimal(10, 2)                  null comment '赠送余额抵消',
    is_occupy                       tinyint(1)                      null comment '是否产生占位费',
    pay_time                        datetime                        null comment '支付时间',
    pay_card                        varchar(64)                     null comment '支付卡(第三方支付)',
    pay_channel                     varchar(64)                     null comment '支付渠道',
    stop_time                       datetime                        null comment '停止时间',
    draw_gun_time                   datetime                        null comment '拔枪时间',
    tip_electricity                 decimal(24, 6)                  null comment '尖电量',
    tip_fee                         decimal(10, 2)                  null comment '尖费用',
    tip_electricity_fee             decimal(10, 2)                  null comment '尖电费',
    tip_service_fee                 decimal(10, 2)                  null comment '尖服务费',
    peak_electricity                decimal(24, 6)                  null comment '峰电量',
    peak_fee                        decimal(10, 2)                  null comment '峰费用',
    peak_electricity_fee            decimal(10, 2)                  null comment '峰电费',
    peak_service_fee                decimal(10, 2)                  null comment '峰服务费',
    flat_electricity                decimal(24, 6)                  null comment '平电量',
    flat_fee                        decimal(10, 2)                  null comment '平费用',
    flat_electricity_fee            decimal(10, 2)                  null comment '平电费',
    flat_service_fee                decimal(10, 2)                  null comment '平服务费',
    valley_electricity              decimal(24, 6)                  null comment '谷电量',
    valley_fee                      decimal(10, 2)                  null comment '谷费用',
    valley_electricity_fee          decimal(10, 2)                  null comment '谷电费',
    valley_service_fee              decimal(10, 2)                  null comment '谷服务费',
    sync_mall_order                 tinyint(1)                      null comment '是否同步到商城',
    is_pay                          tinyint(1)     default 0        not null,
    need_stop                       tinyint(1)                      null comment '是否需要停止',
    refund_status                   int                             null comment '0:未退款 1:全额退款 2:部分退款',
    refund_type                     int                             null comment '退款类型  1=原路退回，2=退回余额',
    refund_amount                   decimal(10, 2)                  null comment '退款金额',
    is_receive_tx_data              tinyint(1)     default 0        not null comment '是否收到交易数据上报',
    order_res                       json                            null comment '创建的商城订单',
    out_trade_no                    varchar(32)                     null comment '支付流水号',
    stopped_reason_code             varchar(32)                     null comment '停止原因编码',
    stopped_reason_content          varchar(64)                     null comment '停止原因描述',
    tx_data                         json                            null comment '硬件交易数据上报',
    tenant_id                       varchar(32)                     not null comment '租户ID',
    created_by                      varchar(32)                     null comment '创建人',
    created_time                    datetime                        not null comment '创建时间',
    updated_by                      varchar(32)                     null comment '更新人',
    updated_time                    datetime                        not null comment '更新时间',
    error_time                      datetime                        null comment '异常时间',
    error_info                      varchar(32)                     null comment '异常信息',
    last_report_amount              decimal(10, 2)                  null comment '最后上报金额',
    last_report_message             json                            null comment '最后上报数据',
    transaction_id                  varchar(32)                     null comment '交易id',
    meter_start                     decimal(18, 4)                  null comment 'ocpp开始电量',
    meter_end                       decimal(18, 4)                  null comment 'ocpp结束电量',
    appointment_meter_start         decimal(10, 2)                  null comment '履约开始的电表值',
    appointment_charging_start_time datetime                        null comment '开始履约时间',
    appointment_meter_end           decimal(10, 2)                  null comment '履约结束的电表值',
    appointment_charging_end_time   datetime                        null comment '结束履约时间',
    pre_coupon_user_id              varchar(32)                     null comment '预选优惠券Id',
    white_flag                      tinyint(1)     default 0        null comment '是否是白名单标志 0-否；1-是',
    balance_insufficient_stop       tinyint(1)     default 0        null comment '是否因余额限制停止（0-否，1-是）',
    prepay_id                       varchar(128)                    null comment '抵扣id',
    pre_freezing                    tinyint(1)     default 0        not null comment '是否预冻结',
    addition_data                   json                            null comment '拓展字段',
    point_award                     varchar(32)                     null comment '是否预冻结',
    ocpi_session                    json                            null comment 'ocpi会话数据',
    custom_charging_electricity     decimal(24, 6)                  null comment '自定义充电电量',
    ds_tip_electricity              decimal(24, 6) default 0.000000 not null comment '电损尖电量',
    ds_tip_fee                      decimal(24, 2) default 0.00     not null comment '电损尖费用',
    ds_peak_electricity             decimal(24, 6) default 0.000000 not null comment '电损峰电量',
    ds_peak_fee                     decimal(24, 2) default 0.00     not null comment '电损峰费用',
    ds_flat_electricity             decimal(24, 6) default 0.000000 not null comment '电损平电量',
    ds_flat_fee                     decimal(24, 2) default 0.00     not null comment '电损平费用',
    ds_valley_electricity           decimal(24, 6) default 0.000000 not null comment '电损谷电量',
    ds_valley_fee                   decimal(24, 2) default 0.00     not null comment '电损谷费用',
    ds_electricity                  decimal(24, 6) default 0.000000 not null comment '电损电量',
    ds_fee                          decimal(24, 6) default 0.000000 not null comment '电损费用',
    has_electricity_loss            tinyint(1)     default 0        not null comment '是否有电损',
    plate_no                        varchar(32)                     null comment '车牌号',
    vin                             varchar(32)                     null comment 'vin码',
    car_id                          varchar(32)                     null comment '车辆id',
    group_id                        varchar(32)                     null comment '车组id',
    refund_time                     datetime                        null comment '退款时间',
    start_soc                       int                             null,
    end_soc                         int                             null,
    device_type_id                  varchar(32)                     null,
    fee_template_id                 varchar(32)                     null,
    is_parallel                     int            default 0        null comment '是否并冲',
    type_name                       varchar(32)                     null comment '设备类型名',
    device_protocol                 varchar(32)                     null comment '设备协议',
    fee_name                        varchar(512)                    null comment '计费模板名',
    settlement_type                 int                             null comment ' 结算方式 0-电费单独结算 1-电费随服务费结算',
    vat                             decimal(24, 2) default 0.00     null comment '增值税',
    vat_fee                         decimal(24, 2)                  null comment '增值税费',
    balance_card_id                 varchar(32)                     null comment '余额卡号',
    mac                             varchar(32)                     null comment '启动mac地址'
)
    comment '充电订单表' charset = utf8mb4;
```

tx_data数据结构

```json
{
    "vin": "",
    "type": 0,
    "tipFee": 0.0,
    "endTime": "2026-07-22 16:13:43",
    "flatFee": 0.0,
    "peakFee": 0.0,
    "tipPrice": 0.02,
    "totalFee": 0.0,
    "beginTime": "2026-07-22 16:13:39",
    "flatPrice": 0.02,
    "peakPrice": 0.02,
    "valleyFee": 0.0,
    "txSerialNo": "2079842220423700481",
    "valleyPrice": 0.02,
    "meterEndValue": 10307921.5104,
    "meterBeginValue": 0.0,
    "stoppedReasonCode": "0",
    "electricityQuantity": 0.0024,
    "stoppedReasonContent": "未知原因",
    "tipElectricityQuantity": 0.0,
    "flatElectricityQuantity": 0.0024,
    "lossElectricityQuantity": 0.0,
    "peakElectricityQuantity": 0.0,
    "valleyElectricityQuantity": 0.0,
    "lossTipElectricityQuantity": 0.0,
    "lossFlatElectricityQuantity": 0.0,
    "lossPeakElectricityQuantity": 0.0,
    "lossValleyElectricityQuantity": 0.0
}
```

tx_data每个字段含义见此类的注释

```java
public class TxData implements Serializable {

	private static final long serialVersionUID = -7562127514292123253L;
    /**
     * 0: 充电 1: 放电
     */
    private Integer type = 0;
	/**
	 * 交易流水号
	 */
	private String txSerialNo;
	/**
	 * 开始时间
	 */
	private Date beginTime;
	/**
	 * 结束时间
	 */
	private Date endTime;
	/**
	 * 尖单价
	 */
	private Double tipPrice;
	/**
	 * 尖用电量
	 */
	private Double tipElectricityQuantity;
	/**
	 * 尖计损电量
	 */
	private Double lossTipElectricityQuantity;
	/**
	 * 尖费用
	 */
	private Double tipFee;
	/**
	 * 峰单价
	 */
	private Double peakPrice;
	/**
	 * 峰用电量
	 */
	private Double peakElectricityQuantity;
	/**
	 * 峰计损电量
	 */
	private Double lossPeakElectricityQuantity;
	/**
	 * 峰费用
	 */
	private Double peakFee;
	/**
	 * 平单价
	 */
	private Double flatPrice;
	/**
	 * 平用电量
	 */
	private Double flatElectricityQuantity;
	/**
	 * 平计损电量
	 */
	private Double lossFlatElectricityQuantity;
	/**
	 * 平费用
	 */
	private Double flatFee;
	/**
	 * 谷单价
	 */
	private Double valleyPrice;
	/**
	 * 谷用电量
	 */
	private Double valleyElectricityQuantity;
	/**
	 * 谷计损电量
	 */
	private Double lossValleyElectricityQuantity;
	/**
	 * 谷费用
	 */
	private Double valleyFee;
	/**
	 * 总电量
	 */
	private Double electricityQuantity;
	/**
	 * 总计损电量
	 */
	private Double lossElectricityQuantity;
	/**
	 * 总消费金额
	 */
	private Double totalFee;
	/**
	 * 车辆唯一识别码
	 */
	private String vin;
	/**
	 * 停止原因编码
	 */
	private String stoppedReasonCode;
	/**
	 * 停止原因描述
	 */
	private String stoppedReasonContent;

	/**
	 * 表计开始读数 ocpp
	 */
	private Double meterBeginValue;
	/**
	 * 表计结束读数 ocpp
	 */
	private BigDecimal meterEndValue;
	/**
	 * 总电费
	 */
	private BigDecimal totalPowerAmount;
	/**
	 * 总服务费
	 */
	private BigDecimal totalServiceAmount;

    /**
     * 运维拓展参数
     */
    private JSONObject extInfo;
}
```



### 1.2TDengine

超级表：charging-gun_property 的字段含义参考此java实体类的注释

```java

public class ChargingGunProperties implements Serializable {
	/**
	 * 交易流水号，充电中状态对应的交易流水号
	 */
	private String txSerialNo;
	/**
	 * 状态
	 * 0：离线
	 * 1：故障
	 * 2：空闲
	 * 3：充电中
	 */
	private Integer status;
	/**
	 * 协议原始状态
	 */
	private String sourceStatus;
	/**
	 * 是否归位
	 * 0：否
	 * 1：是
	 */
	private Integer isReturn;
	/**
	 * 是否插枪
	 * 0：否
	 * 1：是
	 */
	private Integer isInsert;
	/**
		 * 输出电压 V
	 */
	private Double outputVoltage;
	/**
		 * 输出电流 A
	 */
	private Double outputCurrent;
	/**
	 * 输出功率
	 */
	private Double power;
	/**
	 * 充电时间，单位：min
	 */
	private Integer chargingTime;
	/**
	 * 剩余时间，单位：min
	 */
	private Integer remainingTime;
	/**
	 * 充电度数 (桩计费才有) kWh
	 */
	private Double chargingElectricityQuantity;
	/**
	 * 当前上报电表度数 wh (ocpp有) chargingElectricityQuantity = (meterNow-MeterStart)/1000
	 */
	private BigDecimal meterNow;
	/**
	 * 计损充电度数
	 */
	private Double lossElectricityQuantity;
	/**
	 * 已充金额 (桩计费才有)
	 */
	private Double chargingFee;
	/**
	 * soc 快充才有
	 */
	private Integer soc;
	/**
	 * 枪线温度 云快充
	 * 温度 ocpp
	 */
	private Double temperature;
	/**
	 * 电池组最高温度
	 */
	private Double batteryMaxTemperature;
	/**
	 * 最高动力蓄电池温度检测点编号
	 */
	private Integer batteryMaxTemperatureVerifyNo;

	/**
	 * 电池组最低温度
	 */
	private Double batteryMinTemperature;
	/**
	 * 最低动力蓄电池温度检测点编号
	 */
	private Integer batteryMinTemperatureVerifyNo;

	/**
	 * bms需求电压
	 */
	private Double bmsVoltageDemand;
	/**
	 * bms需求电流
	 */
	private Double bmsCurrentDemand;

	/**
	 * bms最高单体动力蓄电池电压
	 */
	private Double maxCellVoltage;
	/**
	 * bms最高单体动力蓄电池电压组号
	 */
	private Integer groupNumber;

	/**
	 * 故障码
	 */
	private String errorCode;
	/**
	 * 故障原因
	 */
	private String errorReason;

	/**
	 * vin
	 */
	private String vin;
}

```



``` sql
CREATE STABLE `charging-pile_comm` (`_ts` TIMESTAMP, `raw` VARCHAR(512), `decoded` VARCHAR(1024), `direction` DOUBLE, `code` VARCHAR(4)) TAGS (`device` NCHAR(16))

# _ts 时间
# raw 原始报文
# decoded 解析后的语意
# direction: 1 下行； 2 上行
# code 指令标识 比如13指令  3b指令，需要结合云快充协议看
```





## 2.常见问题处理

### 2.1.当电量或金额是一些不符合常量的值的时候

当收到充电订单号时，使用数据库驱动绑定 `:tenant_id` 和 `:order_no`，执行有界查询：

```sql
SELECT order_no, tenant_id, status, type, launch_type, device_id, device_code,
       child_device_code, device_protocol, created_time, stop_time, electricity,
       electricity_fee, service_fee, total_amount, is_receive_tx_data,
       stopped_reason_code, stopped_reason_content, error_info, transaction_id, tx_data
FROM cloud_charging_pile.ch_order_info
WHERE tenant_id = :tenant_id AND order_no = :order_no
ORDER BY created_time DESC
LIMIT 3;
```

device_protocol：设备协议，以YKC开通的是云快充协议，通常有YKC1.6、YKC1.8、YKC2.1这3种云快充协议

云快充协议排查是否是设备上报问题：直接看tx_data的数据是否符合常规并与数据库中数据是否一致

程序计算服务费的金额是通过设备上报数据(tx_data)中的总金额-计算出来的电费金额，可能由于电量上报存在问题导致电费计算高于了设备上报的总金额到至服务费为0的情况。



### 2.2 抓取设备报文或过程中时序数据

查询时序数据需要再TdEngine数据库中查询

结合订单的 `device_code,created_time,stop_time` 按以下的sql通常可以再时序数据库中查询出所有过程中数据

```sql
# 查询订单充电过程中的时序数据，时间参数取订单实际 created_time/stop_time
SELECT _ts, `txSerialNo`, status, `isReturn`, `isInsert`, `outputVoltage`,
       `outputCurrent`, power, `chargingTime`, `chargingElectricityQuantity`, soc,
       temperature, `batteryMaxTemperature`, `batteryMinTemperature`, `errorCode`,
       `errorReason`, `meterNow`
FROM `charging-gun_property`
WHERE device = :device_code
  AND _ts >= :created_time
  AND _ts <= :stop_time
  AND `txSerialNo` = :tx_serial_no
ORDER BY _ts ASC
LIMIT 2000;

# 查询指定设备某段时间内的原始报文
SELECT _ts, direction, code, decoded
FROM `charging-pile_comm`
WHERE device = :device_code
  AND _ts >= :created_time
  AND _ts <= :stop_time
ORDER BY _ts ASC
LIMIT 2000;

```
