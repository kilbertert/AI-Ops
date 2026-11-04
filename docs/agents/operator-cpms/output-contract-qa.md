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

## 额度失败回归（2026-11-04）

- OC-06：同一隔离Java/Gateway脚本中注入首轮403，分别带
  `insufficient_user_quota`、中文额度不足消息和普通权限拒绝；前两者显示额度不足，
  普通403只能显示依赖不可用。无正文、不重派，未知错误正文不进入Gateway或页面。
- OC-07：`uv run pytest tests/operations/test_operator_failure.py -n 0`。
  真实Gateway、合成节点失败回调，覆盖有/无订单、派发返回/抛异常、原问答与试运行。
  断言分类、中文提示、无额外CPMS读取、无TypeError；成功路径仍执行原授权校验。
- 生产只读核对精确QA的`agent_runs.reason`与同时间节点日志；只提取HTTP状态和固定
  失败类别，禁止输出上游正文、密钥片段、余额或账号。充值由操作者完成后，再以原页面
  和同一已授权订单复测；没有真实复测不得宣称线上恢复。临时环境由脚本清理。

只读定位顺序：先在服务身份下用SQLite `mode=ro`按本次`qa_id`读取
`agent_runs`的`created_at/state/reason/gateway_version`，再按该时间窗口取日志：

```bash
# 日志可能带密钥片段或业务正文；只在远端归类后打印布尔值，禁止原样回传。
dev-host exec 41 --allow-service-exec --timeout 45 -- python3 - <<'PY'
import subprocess
text = subprocess.check_output(
    ["docker", "logs", "--since", "<UTC开始>", "--until", "<UTC结束>", "aiops-runtime-node"],
    stderr=subprocess.STDOUT, text=True,
)
print({"http_403": "403:" in text, "quota_exhausted": "用户额度不足" in text})
PY
```

时间取自精确QA，journal显示的本地时间必须先换算；不要用最新成功记录替代本次失败。
充值后重新从原页面提交，核对同一问题的新QA、实际运行版本及可见正文。
