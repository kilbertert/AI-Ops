# 提示词生命周期：受管 UI 页面闭环验收（2026-10-31）

补齐 `BLOCKED.md` 里的最大缺口：**用将要部署的受管 Chatflow 重做「编辑 → 预览 → 发布 → 恢复」**。
此前的 119/119 用的是旧注入按钮，不能代表受管界面。

## 被测对象：自己构建，不借用他人产物

- 基线 `5f1ebe2c`（`origin/main`，含 #1232 + #1244/#1245/#1247/#1248）
- 镜像 `aiops/dify-web-patched:prompt-managed-ui-5f1ebe2c`，由
  `deploy/dify-36/web-patches/build-web-image.sh` 在**独立工作树**
  （`DIFY_WEB_WORK=…/dify-web-work`）从上游 tag `1.17.1` 重新构建。
  已存在的同机镜像（`dual-entry-merged-*` 等）**没有来源标签**，无法证明可追溯，因此未采用。
- 补丁确已编译进镜像：`grep -rl 'aiops-workflow-managed' /app/targets/next/web/.next` → 5 个
  server chunk 命中。

## 结果：117/117 通过

```
report.passed = true
checks: 117 total, 117 passed, 0 failed
cleanup_ok = true, cleanup_errors = []
substituted = ["model"]        # 只有模型是合成的
real = ["Dify 1.17.1", "official provider", "Service API client"]
```

覆盖到的关键链路（全部真实页面操作）：

| 检查 | 结果 |
|---|---|
| `指定SYSTEM页面编辑与预览` | 通过 |
| `browser selects B in knowledge node` / `browser selection persisted` | 通过 |
| `draft B keeps published A` | 通过（草稿不污染正式版） |
| `trial searches B and delivers reviewed answer` | 通过 |
| `publish B` → `App reads B after publish` | 通过 |
| `restore A` → `restore searches A` | 通过 |
| 文档流：新建/更新/删除文档不产生工作流版本 | 全通过 |
| 多语言 CPMS：zh-Hant/en/de/fr/es/pt/vi/mn/th/km | 10/10 通过 |

页面确为**受管**编排页，非旧注入按钮 —— `prompt-browser-text.txt` 里可见
「发布到 AI-Ops」「撤回正式版本」「恢复历史版本」「在 AI-Ops 试运行」「自动保存 / 已发布」。

## 原件

一次性私有根 `/tmp/consumer-chatflow-nd1kzhaz/`（已清理容器与卷）：

- `prompt-browser-final.png` —— 页面终态截图
- `prompt-browser-saves.json` —— 实际保存的提示词正文
- `prompt-browser-responses.json` —— 页面发出的请求
- `prompt-browser-text.txt` —— 页面可见文本
- `report.json` —— 117 项明细

汇总报告：`~/.cache/aiops-prompt-continue-20261031/managed-ui-acceptance.json`。

## 边界（不宣称通过）

- **模型是合成的**（`substituted=["model"]`）。本验收证明的是**页面与版本接线**，
  不证明真实模型的表达质量。
- **固定 32 次真实模型样本仍未执行**；上一轮 16 格为 15/16（简洁账单把片段放进行内被拒），
  该失败保留。
- **41 原应用 / 原 H5 的页面验收**未执行 —— 本次跑在隔离栈，不是生产。

即：**受管 UI 的页面闭环已验；真实模型批次与生产原应用仍待验。**
