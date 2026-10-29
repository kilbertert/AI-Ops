# 知识库登录形状修复

分支 `fix/dify-knowledge-tenant-login-hash-shape`，基线 `3ac6115c`（#1156 之后的主线）。

- #1155 合并后用合并制品部署 Gateway（`1be3f067`）、发布桥、Dify Web 镜像，均逐字节核对修订。
- 原开通任务重试仍失败在 `knowledge` 步：`verify-password` 401，凭据读得到却登录不上。
- 用上游自己的 `crypt/decrypt/check_password_hash` 实跑定位真因：RAGFlow 登录比对的是
  `base64(明文)`，恢复工具却写入了明文散列，因此任何登录都不可能匹配。
- 修复恢复工具：写入 `base64(明文)` 的散列，并在写完后回放上游登录判据，不通过就报错并保留备份。
- 真实重跑恢复（0600 备份先写）后 `verify_login OK`、`repair_models` 幂等成功、
  embedding 真实调用返回 3072 维。
- 平台账号正常登录后点原任务「重试」：`state=completed`、`knowledge_ready=true`，
  复用原工作区与应用，`model_checked_at` 落值。
- 41 回读正式路由：优链充仍 V2 / revision 1 / `349a637c-…`；原租户 V22 / revision 3 未动。
- 平台「进入工作区」「进入知识库」两个入口上线并验通：落地身份为优链充租户账号，
  空间存在且为空。

未验收：知识库上传/解析/Dify 同步（租户空间为空，属独立记录项）；用户浏览器里的目视一次
受开发机到 41 公网不可达所限，改用取票＋在 41 兑换证明链路与身份。

下一步：知识库上传解析与 Dify 同步的独立验收；随后按 ADR-0166 把该三项补进客户开通验收。
