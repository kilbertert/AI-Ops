# Java执行节点目录迁移验证

本片在退役切换真实验收通过后开始；基线`19f09c82`，分支`refactor/execution-node-layout`。
移动`tools/m2_close/node/`至`services/execution-node/`，16个原文件逐字未改，未保留旧路径兼容壳。

- JDK21锁定镜像下从新路径编译，Java守卫与同夹具反向控制均STATUS=PASS。
- 真Java工具隔离整链通过：两项CPMS、三轮模型、工具全关拒绝；上游与身份是合成，不冒充生产业务。
- Maven离线package成功；Maven项目自身没有src/test，真实测试来自现有m3node驱动，不能把“No tests”称作Maven测试通过。
- Gateway镜像重建成功，内容摘要核对一致；157文件，节点源码移出镜像上下文。
- 本地全量3317 passed / 9 skipped，ruff和policy通过；跨语言清单PASS、wheel/sdist成功。
- 新旧路径分别离线Maven构建的jar均为`96eb342864e77ce82e255fffa22b526f933db915d1c2d74c86e69e5ee261703e`，逐字一致。
  真实Gateway→Java→原GET的HTTP隔离整链PASS、失败0；spex 0错误/43警告。PR CI待记录。

源码位置改变不触碰服务安装路径、Java包名、配置与业务数据。本片不需要重新部署等字节节点；
以原旧路径/新路径各自独立构建的jar一致为门槛，若不一致继续调查。
本片**未完成业务验收**，只验证无语义变化的目录迁移；此前真实链路证据见退役验收回执。
未新增业务分支或授权规则，无新增复杂度/变异要求；复用原权限与工具边界验证。
