# 模型代理目录迁移验证

基线`1c1b7316`，分支`refactor/model-proxy-layout`。在退役切换真实业务链路验收通过、Java节点归位后，
将固定目标模型代理从`tools/m2_live/model_proxy.py`迁到`services/model-proxy/model_proxy.py`。
原Python文件逐字不变，更新正式/隔离Compose、新镜像上下文与原测试导入，不保留旧源码转发壳。

本片不改变HTTP、TLS、鉴权或配置契约，复用既有模型代理与部署边界用例。
- 原TLS响应帧化、令牌隔离与部署边界专项32 passed。
- 新镜像使用Compose声明的实际入口，临时受信任证书与合成上游下6项通过：错令牌、错路径、完整TLS响应、供应商凭据隔离、原路径透传、源码一致且旧路径不存在。
- 镜像上下文158文件，摘要`35daa5dc38e3238798ec24af838288e293a79f5f2de6d05b6cbadec03b81df6e`，内容锁匹配；wheel/sdist成功。
- ruff和policy通过，spex 0错误/43警告；本地全量3318 passed / 9 skipped；远端CI待验证。
- 原源码SHA-256为`6d7bc1dd9e21aeeb5730973d66325204834ab20efbc4454c59259514fbb1d2f7`，移动前后相同。
生产部署需新Compose与新镜像成对发布，旧发布包不能混搭新入口。当前生产未切换，本片**未完成业务验收**；
临时TLS/合成供应商验证不等于真实模型或历史故障准确率。原源码等字节，未引入新业务分支，不追加变异或复杂度分析。
