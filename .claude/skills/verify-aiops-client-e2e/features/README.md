# Client-surface feature map

One row per user-facing surface, with the END STATE that proves it works. The
verdict column is what makes this a map rather than a list: a surface that is
reachable but serves fallback copy is not working.

Invoke the skill (`verify-aiops-client-e2e`); these files say WHICH routes to
pass and what to look for. The runner takes paths, not feature names:

```bash
# answer surfaces, one language per line in the output
python /tmp/probe.py --paths /v1/faq/recommendations,/v1/shortcuts --languages zh,en,zh-Hant,vi,mn,th,km

# a different content domain (operator)
python /tmp/probe.py --entry operator --paths /v1/faq/catalog --languages zh,en
```

## Surfaces

| Surface | Route | Proves it works |
|---|---|---|
| 固定问答目录 | `GET /v1/faq/recommendations`, `/v1/faq/catalog` | 200 **and** `language` == requested **and** every entry non-empty |
| 固定问答答案 | `POST /v1/faq/answer` (needs a `question_id`) | same, plus a non-empty `answer` |
| 快捷动作 | `GET /v1/shortcuts` | same, plus per-row `language` — **rows published before a language existed serve `zh` and SAY SO**; that is honest, not a defect |
| 统一助手 QA | `POST /v1/assistant/questions` | route reachable; **model behaviour unprovable here** (needs a provider) |
| 单问诊断 | `POST /v1/standard/diagnoses` | route reachable; the stored `language` column is the evidence that the tag reached persistence |
| 健康报告 | `POST /v1/health-report-jobs` | reachable; unprovable if the surface has never run in production |

## Routes that are NOT device-token reachable

`/v1/faq/*`, `/v1/shortcuts`, `/v1/assistant/*`, `/v1/health-report-jobs` all
require the platform identity chain. A local device token gets 401 on every one
of them — that is the auth model, not a defect. Use the real-session probe.

## Reading the log probe

`what_does_the_client_call.py --missing <path>` answers "did the deployed client
even ask". Useful columns: the request COUNT (zero vs many), the STATUS mix
(401 vs 404 vs 200), and the UA bucket (`Html5Plus` = inside the app's webview,
`curl`/`Python-urllib` = an agent's own probe, which is NOT client behaviour).

**Do not read an agent's curl lines as evidence about the client.** They land in
the same log and look the same except for the UA.
