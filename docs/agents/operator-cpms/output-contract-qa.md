# 管家输出契约验证计划

环境：当前任务checkout，一次性Gateway数据库、动态回环端口、锁定JDK与依赖；
使用真实Java节点、Spring AI适配器与Gateway，Dify草稿、公司身份、CPMS、模型为合成。
不得对真实`AIOPS_DATA_HOME`执行。

| 编号 | 场景与数据 | 操作 | 可观察预期 |
| --- | --- | --- | --- |
| OC-01 | 业务SOP没有格式指令；模型未收到JSON模式就返回散文 | 原问答入口提交合成订单，读取结果 | 请求含`response_format={"type":"json_object"}`，报告完成且有两项证据 |
| OC-02 | 已保存草稿与未保存的Dify提示词、双工具 | 分别调用普通试运行与原生预览，回读报告及正式路由 | 同样完成；分别使用对应草稿；正式路由不变 |
| OC-03 | 工具轮先输出进度文字，再输出最终JSON | 重复OC-01 | 报告完成，进度文字没有拼进最终结果 |
| OC-04 | 供应商错误、截断、散文、错字段、假证据、虚假写操作、空/超长正文 | 分别注入每种输出并回读 | 全部失败，没有结果；凭据和原始回答不进入日志 |
| OC-05 | 原C端清单及各种清单漂移 | 运行节点守卫验收与反向控制 | C端协议不变；模型/提示词/工具漂移仍拒绝 |

执行沿用[现有运行方法](README.md)，命令为
`uv run python tools/experiments/m2_close/m3node/verify_operator_cpms.py --out dist/verification/operator-output`。
检查脚本创建的`report.json`及编译/节点日志，保留源码摘要、基线提交、时间与锁定依赖标识。
脚本结束关闭本次HTTP服务与容器，临时数据库自动清理；报告目录保留。

离线构建复用宿主已校验Maven与本地依赖缓存，不安装系统软件。将下列路径变量设为
当前开发机的受控位置，所有产物留在checkout的`services/execution-node/target/`：

```bash
umask 027
docker run --rm --pull=never --network=none --cap-drop=ALL \
  --user "$(id -u):$(id -g)" -v "$PWD:/repo" -v "$AIOPS_M2_CACHE:/m2" \
  -v "$AIOPS_MAVEN_HOME:/maven:ro" -w /repo "$M3NODE_JDK_IMAGE" \
  /maven/bin/mvn -o -Dmaven.repo.local=/m2/repository \
  -f services/execution-node/pom.xml -DskipTests=false package
```

Maven显示`No tests to run`是此模块没有JUnit目录，不能计作测试通过；行为证据来自上述
Java整链及`tools/experiments/m2_close/m3node/run.sh`的守卫验收。比较构建jar的SHA-256与部署回执。

本地通过仅证明上述契约，不证明真实供应商服从参数、ulink线上恢复或故障准确率。
上线后按[41运维手册](../env-41-runbook.md)在已授权账号与订单上分别驱动原生预览、
AI-Ops试运行和正式入口，记录运行制品版本、时间、结果及拒绝场景；未执行则标待验证。
