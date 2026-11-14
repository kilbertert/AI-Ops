# 95共享模型与知识服务接入

95的公网HTTPS到共享Dify实测被连接重置，既有知识SSH通道仅批准9380，不能借用其密钥扩大访问。
使用95项目身份的独立密钥，复用已登记的共享服务宿主与现有受限SSH账户；不新增公网入口。

## 配置与安装

1. 在95的`/etc/aiops-95/ssh/`生成独立ed25519密钥，目录0700/文件0600、aiops所有，私钥不出主机。
   主机公钥来自受控主机清单，写`known_hosts`；禁止临时信任未知主机或关闭校验。
2. 共享宿主的既有受限账户authorized_keys备份后追加该公钥，选项固定为
   `restrict,port-forwarding,permitopen="127.0.0.1:10008",permitopen="127.0.0.1:15001",permitopen="127.0.0.1:9380",command="/bin/false"`。
   公钥唯一注释`aiops-env95-shared`，重复即核对不追加；旧key及权限不改。
3. 95的`tunnel.env`（0600）只设置`AIOPS_SHARED_SSH_TARGET`为清单指定账号/主机；具体地址留私有记录。
   从合并修订安装`ops/environments/env95/shared-services-tunnel.service`为`aiops95-shared-services-tunnel.service`，daemon-reload并启用。
4. 端口29508/29501/29580仅绑定95回环，安装前检查没有其他监听或预留用途并记录95主机端口分配。
   `/console/api/setup`通过证明传输，inner-api匿名拒绝及错误key拒绝证明接口鉴权仍生效；隧道不提供用户权限。
5. Gateway使用29508访问Dify、29501访问inner-api、29580访问知识服务。共享服务管理key运行时从受控位置取得，
   不复制其他环境的会话、工作区、订单或节点身份；95工作区按环境/租户独立创建。

## 恢复

停止/禁用95新隧道服务，恢复本轮修改的Gateway配置；共享宿主只移除注释完全匹配的新增公钥行。
原知识隧道和原环境服务不变。记录前后配置摘要、拒绝验证及生效端口。
