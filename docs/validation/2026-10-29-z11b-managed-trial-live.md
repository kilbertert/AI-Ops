# 受管试用修复部署与 ulink 四入口验收

日期：2026-10-29（北京时间）。PR #1156 合并 `3ac6115c`，CD `deploy-41` success。
本轮只把「受管试用不再解析正式空清单」和「带编号站名不误触订单诊断」这两处修复上线并验收，
不代表诊断准确率、长期稳定性或原生 APK/IPA 已验收。

## 部署

| 组件 | 修订 | 证据 |
|---|---|---|
| Gateway | `3ac6115c` | `gateway_api.py:2699` `order_context`、`operator_cpms.py:352` `if not task.trial` 均在 `/opt/aiops-41/src` 回读一致；服务 18:18:48 重启 |
| Dify 发布桥 | `3ac6115c` | `build-bundle.sh` 制品 SHA256 `e354ad8b…514c`；36 上 `sha256sum` 逐字节一致；`deploy.sh` 备份至 `bridge-backup-3ac6115c`，`REVISION` 回读一致，服务 active |
| Java 执行节点 | 未变更 | 本次 diff 不含 `java/`、`services/`；仍为 `2da807b4` |

排队期间先发现自托管 runner 被一条已合并 PR 的 `Agent Update Branch`（run `40853443765`）
占住 20 分钟以上，CD 一直 queued。该运行的 PR 已合并，取消后 CD 立即出队。

## ulink 真实试用

Dify 公司登录态已过期（控制台显示「公司登录已过期」），试运行以 403
`OPERATOR_TRIAL_LOGIN_REQUIRED` 失败。用 ulink 经正常验证码登录后重跑，全部为真实
浏览器请求经隧道到 36，未伪造会话或回放响应。

| 用例 | 结果 |
|---|---|
| 通用（`订单状态3是什么意思`） | completed，说明 status=3 是历史已处理，并请用户提供单号 |
| 缺单号的诊断请求 | completed，以对话方式要求补单号，未进入诊断链 |
| 广州天河找站 | completed，明确「当前账号可见范围内未找到匹配站点」，带站点时间戳 |
| 带日期站名 `ZDH--Test2026-10-29 03:00:09` | completed，返回该站与地址 `984HF52X+QW`——编号不再被当成订单号 |
| 原订单状态（`2115642007348932609`） | completed，CPMS 站点/设备/创建与停止时间/0.013kWh/0 条监测样本逐项呈现 |
| 原订单诊断（同一单） | 首次 failed（`json_parse` / `OPERATOR_INVALID_ANSWER`），同一问法重试 completed，输出同上字段 |

`json_parse` 当日 4 次、本轮 1 次。这是既有间歇故障：`#1149` 已把节点改为每轮强制 JSON 对象
并只提交最后一个正常结束的模型轮，Gateway 校验保持严格。本轮未放宽任何校验，也未因此宣称
诊断稳定性通过。

## 边界

- 只证明受管试用能进入模型并返回结构化回答；站点搜索仍受当前账号可见范围限制，未验证实时空闲枪口或价格。
- 一次 `json_parse` 失败原样保留，未用改问法或放宽判据来「消掉」它。
- 未验证原生 APK/IPA、跨租户正例、诊断结论准确率与长期稳定性。
