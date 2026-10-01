# 公司源码只读探查规程（#414 的方法基准）

> **读者**：任何要回答「公司业务侧到底是怎么实现的」的人——**先读本页，再动手查**。
> **状态**：2026-10-01 定稿。**适用范围**：`docs/agents/company-code-baseline.md` 的
> L1/L2/L3 三层，以及后续任何按需求深化的 L4/新模块探查。
> **它回答的是「怎么查」，不是「查到了什么」**——查到的东西写在基线文档里，不写在这里。
>
> **L1/L2/L3 三层文档都从本页执行**（#476 / #477 / #478 各自在开头声明一句）。

---

## 0. 一句话

**API 优先、不 clone；`--ref` 必填、默认分支不算数；凭据只写位置、源码不进仓。**

三条都不是风格问题，各自对应一次已经发生过的、得出**错误结论**的教训。下面逐条给出
判据命令与错误结论的样子。

---

## 1. 唯一取数入口：`deploy/company-gitlab-api.sh`

本规程里的每条命令都由 `deploy/company-gitlab-api.sh` 提供，**不要另写临时 curl**：
把端点形状固化在一个地方，是让第二个人的复现结果与第一个人相同的前提。

```bash
deploy/company-gitlab-api.sh pin
deploy/company-gitlab-api.sh projects  --search <词>          # ① 按关键词找仓
deploy/company-gitlab-api.sh group     --id <组id>            # ② 列域内全部项目
deploy/company-gitlab-api.sh branches  --project <id>         # ③ 分支探测第一步
deploy/company-gitlab-api.sh tree      --project <id> --ref <分支> [--count-java]
deploy/company-gitlab-api.sh blobs     --project <id> --ref <分支> --search <词>   # ④ 跨文件搜代码（主力）
deploy/company-gitlab-api.sh raw       --project <id> --ref <分支> --path <路径>   # ⑤ 读单文件原文
```

**脚本只做只读 GET。** 写方法（POST/PUT/DELETE）不在它的能力范围内——见 §4 边界。

### 1.1 一个必须做、但容易漏的对照

`projects --search` 是**按仓库名**搜，**不是全文搜**。找代码要用 `blobs`，
且 `blobs` **必须带 `--project`**：全局 blobs 搜索在这台实例上**直接不支持**。

```
GET /api/v4/search?scope=blobs&search=…          →  400
    {"message":{"error":"Scope not supported without Elasticsearch!"}}
```

这是 GitLab CE/EE 的已知形态（Advanced Search 未启用时 blobs 只在项目内可用），
**不是配置错误**。判据：跨文件搜索一律走 `blobs --project <id> --ref <分支>`。

### 1.2 实测：`projects --search` 是**仓库名**匹配，不是路径匹配

搜 `cloud-charging-pile` 得到 3 个条目，**不是 1 个**：

```
363  iot/cloud-charging-pile      default=master  activity=2026-09-30T09:51:26Z
526  mtz/cloud-charging-pile      default=master  activity=2024-04-10T02:01:53Z
524  mtw/cloud-charging-pile      default=master  activity=2024-04-10T01:57:21Z
```

三条都是**同名仓**（`path` 相同、命名空间不同），不是同名不同物。取用时**必须按
`last_activity_at` 与命名空间判断哪个是活的**——526/524 停在 2024-04，363 停在昨天。
「搜到了唯一一个」这种前提在本实例上不成立。

---

## 2. 🛑 `--ref` 必填：默认分支可能是脚手架

**这是本规程里唯一一条会把「没查」变成「查错了」的规则。**

`iot/cloud-charging-pile` 的默认分支 `master` 只承载脚手架，真代码在 `release`：

```
$ deploy/company-gitlab-api.sh tree --project 363 --ref master  --count-java
ref=master blobs=46 java=34
$ deploy/company-gitlab-api.sh tree --project 363 --ref release --count-java
ref=release blobs=2480 java=2012
```

**漏做这一步会得到什么错误结论**：本仓那份「Java 快照只有 3 个文件」就是这么来的——
当时只取了默认分支，于是得出「这个仓库是空的」。实际差了两个数量级（34 → 2012）。
同理，`blobs` 搜索不带 `--ref` 时，GitLab 在**默认分支**上搜：

```
$ … blobs --project 363 --ref release --search '@Inside'
cloud-charging-pile-core/…/DataDashboardController.java:49     ← release 上有 3 处
$ … /projects/363/search?scope=blobs&search=@Inside            （不带 ref）
cloud-charging-pile-core/…/DemoController.java                  ← 默认分支上只有 1 处，且是脚手架
```

⇒ **搜索也一样受默认分支影响**，不只是读文件。

**强制步骤**（每次探查一个新仓都要做，不能只做一次）：

1. `branches --project <id>` 列出分支与各自的 `committed_date`；
2. 对候选分支数 `.java` 文件数：`tree --project <id> --ref <分支> --count-java`；
3. **把选中的分支写进结论**，格式 `仓:分支:文件:行`。只写 `仓:文件:行` 的结论**不算数** ——
   读者无法判断你当时看的是哪一份。

**「真分支」不止一个，`release` 不是通解。** 同一份 `README`/`pom.xml` 在多个分支上可能
长期分叉，`committed_date` 最近的那个未必是线上那份。实测 `iot/cloud-charging-pile`：
`release` 与 `uat` 的头几天内都在提交（`2026-09-29` / `2026-09-30`），而
`refactor/order-refactor` 也在动。**结论要指明它落在哪个分支上**；跨分支的分歧是
**结论的一部分**，不是可以四舍五入掉的噪音。

> **判据补充**：分支名不可猜。`release` 只是**这一个仓**的答案；`iot/cloud-charging-pile`
> 上另有 `dev` / `uat` / `dev_hlht` 等 10 个分支，`committed_date` 最近的可能是 `uat`
> 而不是 `release`。**「哪个分支是真分支」要按 `committed_date` + `.java` 计数当场判**，
> 不能沿用上一个仓的结论。

---

## 3. 凭据：只写位置，不写值

| 项 | 位置 | 用法 |
|---|---|---|
| 令牌 | `~/.git-credentials` 中**公司 GitLab 主机**那一条的口令字段 | 作为 HTTP 头 `PRIVATE-TOKEN` |
| 用户字段 | 同一行的用户字段 | 取值 `oauth2`——**它不是账号名，是这一行表示「令牌」的标记** |
| 身份锚点 | `.scratch/company-repos/gitlab-qushiyun.pem`（**已 gitignore**） | 见 §3.1 |

脚本自己读这两样，**令牌不进命令行、不进输出、不进本仓任何文件**。凭据文件的位置
**不可由环境变量改**——一个能被改路径的凭据读取器就是一个能读任意文件的原语。
写文档时写「哪个文件、哪个头、哪个字段」，**永远不写值本身**——本仓是公开仓库。

> ⚠️ **不要用 `bash -x` 调这个脚本**：xtrace 会把 `-H 'PRIVATE-TOKEN: …'` 打印出来。
> 脚本头部写了这一句，因为「调试时开一下 xtrace」是最容易把令牌送进日志的一个动作。

### 3.1 自签证书：钉指纹，不是「-k 跳过」

公司实例用的是自签证书（`subject = issuer = CN=git.qushiyun.com`，2021 年签发、
2021 年 11 月到期），系统 CA 不认它。但**光加 `-k` 等于完全不做证书校验**——
任何能顶替这个地址的人都能拿到令牌。所以脚本的做法是：

1. 把证书留存在 `.scratch/` 下（`openssl s_client … | openssl x509 -outform PEM > <文件>`）；
2. 从它算出 **SPKI 指纹**；
3. 用 `curl --pinnedpubkey sha256//<指纹>` 请求——**`-k` 只保留给「证书已过期」这一件事，
   主机公钥仍被钉住**。指纹对不上就失败（实测：换一个指纹 → `curl: (90) public key
   does not match pinned public key`）。

当前锚点（可公开，是公钥摘要，不是凭据）：

```
subject=CN=git.qushiyun.com
SPKI sha256 = sha256//0E4usLaswMAYis+NuEpr5B2untcdBdi6AQgz7ISMjhQ=
证书 sha256   = 3E:D6:18:D9:40:9C:C6:6B:02:36:4C:00:2E:7C:C0:A4:9B:18:B9:F0:69:19:F7:F7:98:F7:94:47:A8:FB:28:D4
```

**换锚点是一次人的决定**：证书换了、脚本会失败，此时**核对指纹后手工更新**那份留存证书，
不要改成忽略校验。`deploy/company-gitlab-api.sh pin` 单独打印当前锚点，供比对。

> ⚠️ 与既有做法的差异要写清楚：本机 `git` 对这台主机是 `http.sslverify=false`
> （`git ls-remote` 因此可用）。**本脚本不沿用那个设置**——配置文件里的一条
> `sslverify=false` 会让这台主机上的**每一次** git 操作都不校验，而 `--pinnedpubkey`
> 只影响本次请求。前者是整个账号的口子，后者是一次调用的锚点。

---

## 4. 边界：公司源码是外部治理资产

**不可让步**：

- **只读**。不提交、不建分支、不改配置。脚本只做 GET。
- **不提交进我们仓库**：不粘贴源码正文、不镜像、不放 diff 片段。基线文档只写
  **结论与出处**（`仓:分支:文件:行`），结论要用源码支持时**引行号，不引正文**。
- **本地克隆位置固定**在 `.scratch/company-repos/`（`.scratch/` 已在 `.gitignore`）。
  ⚠️ **本规程不推荐 clone**（API 已经够用，且 clone 会把整份源码落到磁盘上）；
  确需 clone 时也只放这里。
- **不写任何凭据、token、口令明文**，只写位置。
- **不外传**：本页与基线文档是内部文档。**本仓是公开仓库**——写进 `docs/` 就等于公开，
  所以「内部文档」这四个字在这里的含义是「内容仍然要按可公开的标准写」。
  具体：主机名与路径这类**已经在本仓既有文档里出现过**的可以写；**新增的**基础设施
  细节（新地址、新端口、新账号、内部拓扑）先问，不要顺手写进公开仓。

**可复现性判据**（验收用）：文档里的每条命令，第二人能独立执行并得到**相同结论**。
做不到的命令不要写进文档——写「我用某个脚本跑过」不算证据。

---

## 5. 一次完整探查的最小骨架

任何 L1/L2/L3 的条目都按这五步走；每一步的产物都留在 `docs/agents/company-code-baseline.md`
对应的层里：

1. **定位仓**：`projects --search <业务词>` → 按 `last_activity_at` 与命名空间选；
   同一业务词可能命中多个**同名仓**（§1.2），全部列出来再选。
2. **定分支**：`branches` + `tree --count-java` → 选中**真分支**（§2），写进结论。
3. **找代码**：`blobs --project <id> --ref <真分支> --search <业务词>`。
   ⚠️ 这只给 `路径:行号`。**要判断语义必须再读原文**（下一步）——只看命中行容易
   把「有这段代码」读成「这段代码在跑」（本仓已有的同形教训：
   `AdminProxyHeadFilter` 的函数体**整段被注释掉**，命中行并不区分注释内外）。
4. **读原文**：`raw --project <id> --ref <真分支> --path <路径>`，行号以原文为准。
5. **落结论**：`仓:分支:文件:行` + 一句结论 + **这条结论的适用边界**
   （哪些情况下不成立）。没有第 3 段的结论不要落库。

> **第 3 步的补充判据**：注释掉的代码与在跑的代码**在 blobs 搜索里长得一样**。
> 凡结论是「某机制生效/不生效」，必须在原文里确认它**不在注释里**，并把这一点写进
> 结论——这是「别从『它是网关』推出『它注入身份』」那条判据在源码层面的同一形态。

---

## 6. 当场跑通留下的真实输出（2026-10-01）

供复核用；命令与输出逐字粘贴，未修饰。

```
$ deploy/company-gitlab-api.sh pin
sha256//0E4usLaswMAYis+NuEpr5B2untcdBdi6AQgz7ISMjhQ=

$ deploy/company-gitlab-api.sh projects --search cloud-charging-pile
363	iot/cloud-charging-pile	default=master	activity=2026-09-30T09:51:26.553Z
526	mtz/cloud-charging-pile	default=master	activity=2024-04-10T02:01:53.593Z
524	mtw/cloud-charging-pile	default=master	activity=2024-04-10T01:57:21.980Z

$ deploy/company-gitlab-api.sh group --id 90 | wc -l
28

$ deploy/company-gitlab-api.sh tree --project 363 --ref master --count-java
ref=master blobs=46 java=34

$ deploy/company-gitlab-api.sh tree --project 363 --ref release --count-java
ref=release blobs=2480 java=2012

$ deploy/company-gitlab-api.sh blobs --project 363 --ref release --search '@Inside'
cloud-charging-pile-core/src/main/java/com/qushiyun/cloud/charging/pile/core/application/controller/DataDashboardController.java:49
cloud-charging-pile-core/src/main/java/com/qushiyun/cloud/charging/pile/core/application/controller/DataDashboardController.java:65
…（release 共 3 处）

$ deploy/company-gitlab-api.sh projects --search cloud-charging-pile | wc -l
3
```

**脚本的守卫也当场验过**：`blobs` 不带 `--ref` → `--ref 是必填。`（退出码 2）；
`--pinnedpubkey` 换成错误指纹 → `curl: (90) public key does not match pinned public key`。

---

## 7. 本页不是什么

- **不是结论集**。结论在 `company-code-baseline.md`，本页只写方法。
- **不是权限**。能读到公司源码不等于能对外说它；见 §4。
- **不是公司侧问题的答案**。公司后端「该不该改」是公司自己的事，本页只描述怎么**读**它。
