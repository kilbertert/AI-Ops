# 客户端回答面功能图

一行一个用户可见面，并写明**证明它能用的终态**。判定列才是这张图区别于列表的地方：
一个可达但只吐兜底文案的面，不算能用。

调用技能（`verify-aiops-client-e2e`）；本目录只说**该传哪些路径**、**该看什么**。
运行器接的是路径，不是功能名：

```bash
# 回答面；输出逐语言一行（退出码 0=全过 / 1=有失败 / 2=未取证）
python /tmp/probe.py --accept-live-app \
  --paths /v1/faq/recommendations,/v1/shortcuts --languages zh,en,zh-Hant,vi,mn,th,km

# 换内容域（管家端）
python /tmp/probe.py --accept-live-app --entry operator --paths /v1/faq/catalog --languages zh,en
```

## 各个面

| 面 | 路由 | 证明它能用的判据 |
|---|---|---|
| 固定问答目录 | `GET /v1/faq/recommendations`、`/v1/faq/catalog` | 200 **且** `language` == 请求语言 **且** 条目非空 |
| 固定问答答案 | `POST /v1/faq/answer`（需 `question_id`） | 同上，且 `answer` 非空 |
| 快捷动作 | `GET /v1/shortcuts` | 同上，且**逐行**读 `language` —— 在语言存在之前发布的行走 `zh` 并**如实上报**，那是诚实，不是缺陷 |
| 统一助手 QA | `POST /v1/assistant/questions` | 路由可达；**模型行为在这里证不了**（需 provider） |
| 单问诊断 | `POST /v1/standard/diagnoses` | 路由可达；存储侧的 `language` 列是「语言标签到达了持久化层」的证据 |
| 健康报告 | `POST /v1/health-report-jobs` | 可达；该面在生产从未运行过时则无法证明 |

## 设备令牌够不到的路由

`/v1/faq/*`、`/v1/shortcuts`、`/v1/assistant/*`、`/v1/health-report-jobs` 都要求平台身份链。
本地注册的设备令牌打它们**全部 401** —— 这是鉴权模型，不是缺陷。用真会话探测。

## 怎么读流量探测

`what_does_the_client_call.py --missing <path>` 回答「部署出去的客户端到底调没调」。
有用的三列：请求**计数**（零 vs 多次）、**状态**分布（401 / 404 / 200）、
**UA 分桶**（`Html5Plus` = App 内置 webview；`curl` / `Python-urllib` = agent 自己的探测）。

**不要把 agent 自己的 curl 行当作客户端行为的证据。** 它们落在同一个日志里，
除 UA 外长得一模一样。
