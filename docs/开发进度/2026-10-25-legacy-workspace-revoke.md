## 2026-10-25 · 撤旧 workspace 访问权（#888）

**目标/分支**：`docs/legacy-workspace-revoke`。动作发生在服务主机 36 的 Dify 数据上。

用户要求退役 `aiops-admin-legacy`。按 #888 从"撤旧 workspace 访问权"开始 —— 只撤成员，
**不删 workspace**（票面 AC 明写，且 Dify 没有删 tenant 的 API）。

**撤权前查清屋子里有什么**：2 个 `[归档]` app、1 个 vendor 数据集、1,564 个切片、**0 条
embeddings**、`indexing_status=error`。那个知识库从未可用过（`VECTOR_STORE` 未配置），
不是"待迁移的数据"。

**撤权门槛满足**：新 workspace 上跑过真实发布与试运行（#887）、`ulink` 真实登录验收通过
（今天）；全部 6 个 app 与 5 个 dataset 的 `maintainer` 都是 `AI-Ops Admin`，`system` 名下
0 行 ⇒ 移除它在结构上不可能触发归属转移。

**做了什么**：`DELETE /console/api/workspaces/current/members/<system>` 带
`X-WORKSPACE-ID: 2db44904-…` 与 admin key（从容器自己的 env 读，不进 argv）。

**四条验证**：撤权后 `system` 不在 legacy（但在另外两间仍是 editor，这是对的）；
app/dataset 归属前后都是 `a53ad893`；`dify_app_registry` 两行逐字未变；
恢复路径用 member 端点往返一次（join 200 → 再撤），证明 `RBAC_ENABLED=False` 下
"移出后无法重新邀请"有绕法。

**记录到的陷阱**：`/inner/api/enterprise/workspace/member` 用 `X-Inner-Api-Key`，
用 `Authorization: Bearer` 会 401。

**后果**：回滚点变成只读（只剩 service identity owner）。可接受 —— 新屋子已验收过，
旧副本从未被真实用户打开。**没有删 workspace**；那需要另一张票，风险不同。

详见 `docs/validation/2026-10-25-legacy-workspace-revoke.md`。
