# 管家派发期限验证

| ID | 环境与操作 | 预期 |
|---|---|---|
| OD-01 | 真Gateway/存储，平台替身模拟82秒完成并驱动节点回调 | QA completed，不被旧75秒提前失败 |
| OD-02 | 已过期deadline_at，监测平台调用 | AgentRunDispatchError，零出站 |
| OD-03 | 全量CI及临时Gateway | 原授权/终态/取消/其他入口不回归 |
| OD-04 | CD后原管家H5正常登录选单提交 | 正文可见、同QA唯一run succeeded/matched |

时间模拟不睡82秒；真实验证保留修复前失败并按同次QA关联，不以历史最新成功代替。
