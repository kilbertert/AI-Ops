# AFK development workflow

The project uses the official Matt Pocock planning skills and this repository's
Sandcastle execution adapters:

```
idea -> /grill-with-docs -> /to-spec -> /to-tickets -> implement -> review -> merge
```

## Phase boundaries

1. `/grill-with-docs` resolves terminology and decisions. When its frontier is
   empty, it reports `GRILLING_COMPLETE`, asks for confirmation, and ends the
   turn. Confirmation does not authorize a later phase.
2. `/to-spec` publishes the confirmed requirements as a GitHub spec/PRD issue.
3. `/to-tickets` creates native sub-issues and native blocking edges in the
   order approved by the user.
4. Implementation starts only after an explicit invocation or authorization:
   `agent:implement` for the matching GitHub workflow, `pnpm ralph` for the
   host planner, or `pnpm afk -- <issue>` for one controlled issue.
5. Review runs the harness-neutral Sandcastle two-axis orchestration, then a
   fixer handles confirmed findings and PR conversation.

## Labels

| Label | Meaning | Engine |
| --- | --- | --- |
| `ready-for-agent` | Complete spec, eligible leaf issue | `pnpm ralph` |
| `agent:implement` | Explicit execution authorization | issue or PR workflow |
| `agent:queued` | Ready but blocked by an open native dependency | promotion workflow |
| `agent:in-progress` | AFK run is active | workflow state |
| `agent:blocked` | Failed run or invalid shape | human triage |
| `agent:review` | Explicit PR review authorization | PR review workflow |

`agent:implement` is not a planning label. The planner never selects PRDs,
parents with sub-issues, nested sub-issues, open native blockers, or issues
already targeted by an open PR. It has no forced fallback when every candidate
is blocked.

## Truth sources

- `GLOSSARY.md` is the glossary only.
- `docs/adr/` records durable implementation decisions and trade-offs.
- The spec/PRD issue records requirements.
- Native sub-issues and dependency edges record execution slices.
- `docs/agents/` tells the official skills how to read the tracker, labels,
  and domain docs.

## Delivery

Agents commit on task branches and run deterministic checks. The host runner
pushes branches, opens draft PRs, and merges only after CI and human review.
No agent pushes the default branch directly.

Pull request mutation jobs accept only repository-owner-authored branches from
the same repository. The current `main` checkout supplies the trusted
controller, candidate commands run in Docker with the read token, and verified
Git bundles enter a clean delivery checkout before the host write token is used.
Missing delivery credentials produce `agent:blocked`; there is no non-triggering
`GITHUB_TOKEN` fallback.

## 模型 provider

配置的 Sandcastle 档案是**服务端全局**的：`claude` 或 `claude-deepseek`。
本机运行设 `AFK_PROFILE`，Actions 上设同名 repo variable；项目侧无需任何凭据。

两者都指向宿主持有的一个 settings 文件，只读挂入沙箱：

- `claude`：用宿主 shell 已导出的凭据直连 Anthropic API。
- `claude-deepseek`：经宿主本机的 `cli-proxy-api` 中继（127.0.0.1:8317）
  访问 Anthropic Messages API，配置文件为 `~/cliproxyapi/settings.deepseek.json`，
  其中含 `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN` 以及三个
  `ANTHROPIC_DEFAULT_*_MODEL` 条目。文件在别处时用 `AFK_DEEPSEEK_SETTINGS` 覆盖路径。

**该中继绑在宿主 loopback 上，因此沙箱必须共享宿主网络命名空间**
（默认 bridge 的容器到不了宿主 127.0.0.1）。代价是沙箱失去 Docker bridge 隔离，
可达宿主**其它** loopback 服务。这是知情的取舍，不是可忽略的细节；
缓解方式是按 host 隔离——AFK 跑在 loopback 只有中继的宿主上。

Because the endpoint is mounted rather than baked, rotating the token is an
edit to that host file plus a container restart — there is no image rebuild,
and no `--no-cache` to remember. The base URL is the provider root preceding
`/v1`; Claude Code appends `/v1/messages` itself, so a URL ending in `/v1`
would be requested as `/v1/v1/messages`.
