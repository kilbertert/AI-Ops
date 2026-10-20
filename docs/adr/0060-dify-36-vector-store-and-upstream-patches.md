# 60. 36 上的 Dify 栈：补一个向量库，用版本化补丁改上游三处行为，凭据加密落库

日期：2026-10-20
状态：已采纳（部署件已备，**尚未部署到 36**）
影响版本：`deploy/dify-36/`（`docker-compose.yaml`、`env.example`、`check.sh`、新增
`patches/`、`migrate_20261020.py`、`acceptance.feature`、`qa-plan.md`）、
`tests/test_dify36_deploy_assets.py`

## 背景

36 上的 Dify 1.17.1 是**未改动的上游镜像**，PRD #577 当时给它定的形态是「9 个容器、
向量库 0 个」，理由是「向量初始化是惰性的，本实例的知识面全走 External Knowledge API」。
三件实测把它打穿了：

1. **惰性 ≠ 碰不到。** 运营在控制台里建了一个 vendor 数据集并上传 PDF —— 那是产品允许的
   动作 —— 1564 个分段全部停在 `indexing`（`enabled=f`），`documents.error` 是
   `Vector store must be specified.`，`embeddings` 表 0 行，worker 的 retry 与 duplicate
   两条任务以同一个 `ValueError` 结束。失败形状很隐蔽：分段都建出来了，界面上看着有内容。
2. **`POST /v1/datasets/{id}/retrieve` 对 external 数据集返回「成功但为空」。**
   `HitTestingService.retrieve` 收了 `external_retrieval_model` 参数，却从不使用；
   路由无条件走 vector 路径。上游 1.17.1 与当天的 `origin/main` 都是这个形状，
   所以这不是「等上游修」。
3. **库卫生两处**：`external_knowledge_bindings.dataset_id` 没有外键、删库路径也不清理，
   留下 6 条指向已删数据集的孤儿；`external_knowledge_apis.settings` 是 JSON 文本，
   两行 `api_key` 明文可直读。

第 2 件在 Dify 仓库里的正确修法是改 `HitTestingService.retrieve`（按 `dataset.provider`
分派），第 3 件里加密的部分要改 `external_knowledge_service` 的写入与读取两侧。

## 决定

### 一、向量库补一个 weaviate，而不是"再确认一次零个也行"

**加 `weaviate:1.39.2`**（上游 1.17.1 的 compose 钉的就是这个版本），不发布宿主端口，
只用 compose 内网名 `weaviate:8080`，数据卷 `./volumes/weaviate`，遥测关闭。

`VECTOR_STORE=weaviate` 与 `WEAVIATE_ENDPOINT` 写进 `env.example`，`check.sh` **两条都断言**：
只起容器不改变量等于白起一个容器，那正是这次故障的第二种形状。
`WEAVIATE_API_KEY` 用 `${VAR:?}` 写成必填 —— 缺了它 compose 直接拒绝解析，而不是静默起一个
无鉴权的向量库。

### 二、改上游用**版本化补丁 + 逐文件只读挂载**，不重做镜像、不挂整文件

```
镜像原文件 --sha256 核对--> 打 patches/1.17.1/*.patch --> volumes/app/patched-api/ (0444)
                                                            └─ 逐文件 :ro 挂进 api/worker
```

`patches/apply.sh` 从**同一镜像**取原文件、核对固定 sha256、`patch --fuzz=0`。摘要不匹配
就非零退出 —— 镜像升级时补丁必须重做，不能把陈旧结果挂上去静默运行。

补丁内容：`external` 数据集在 service API 的 `retrieve` 上走 `HitTestingService.external_retrieve`；
API 模板的 `api_key` 落库加密、读出解密；删 dataset 时一并删外部绑定。

`api` 与 `worker` 的挂载集合必须**逐文件**（挂目录会把整个 `/app/api` 遮住），
且集合必须等于补丁覆盖的文件集合 —— `check.sh` 与 `tests/test_dify36_deploy_assets.py`
各断言一次。

### 三、凭据加密落库，加密与轮换分成两步

`settings.api_key` 用 Dify 自己的 tenant 密钥提供者加密，存成 `enc:v1:<base64>`；
读出时解密；**没有该前缀的旧值是明文，仍然可读** —— 所以迁移可以逐行做，不是一次断裂式
sweep。控制台响应返回明文（编辑框要回填，`[__HIDDEN__]` 往返语义不变）。

**加密不等于止损。** 历史上已明文落过盘的两把共享凭据必须视为已泄露：换值是独立一步，
在 41 与 Dify 两侧同步进行，旧值作废。迁移脚本不换值。

**顺序是硬约束**：先起打完补丁的容器，再跑迁移。反过来会让运行中的旧代码把密文当明文
发出去 —— 外部检索开始 401，而报错看起来像"供应商拒绝了凭据"。

## 被否的方案

- **再确认「零向量库」可行**：等于要求运营永远不自建 vendor 数据集。那是产品允许的动作，
  而约束只在文档里、没有任何检查 —— 已经发生过一次，且失败只留一行 error。
- **用 pgvector 复用现有 postgres**：现镜像 `postgres:15-alpine` 没有 `vector` 扩展，
  要换镜像并重建 DB 容器。为省一个容器去动全栈唯一的状态数据库，风险与收益倒挂。
- **重做镜像**：自建镜像要把整条构建链（`uv sync`、插件包、2.9 GB 上下文）搬上服务主机，
  爆炸半径从「5 个文件」变成「一整条链」。
- **仓库里存打补丁后的整文件并挂载**：上游升级时要整份重拷，`diff` 不显眼 —— "上游改了什么"
  会隐身于两份长文件之间。补丁带上下文，sha 核对会逼着重做。
- **只记录为上游限制，不改代码**：external 数据集就只能用控制台的
  `external-hit-testing`，service API 永远返回"空"。但 `/v1/datasets/{id}/retrieve`
  是**同一个端点**服务两种数据集，返回空是**看起来成功**的错误答案 —— 比 4xx 更糟。
- **只加密不轮换**：新写入是密文，但历史上已落盘的值继续有效，等于只修了"以后"。
- **清空 `SSRF_PROXY_*` 以便 external 走内网**：与本变更无关，且是既有决定明确否掉的路。

## 后果

- 本栈从 9 个容器变成 10 个；weaviate 静置实测 52.7 MiB，远小于其余部分。
- **补丁绑定 1.17.1 的原文件摘要**。升级镜像时 `apply.sh` 会拒绝执行 —— 那是设计，
  不是故障：补丁必须按新版本重做，摘要与新补丁同批更新。
- 加密把「读 settings」变成**必须走 `external_api_settings`** 的不变式。任何新增读路径
  直读 `api_key` 都会把密文当 Bearer 发出去；目前三条读路径（create/edit 回填、
  控制台响应、检索）都已改，测试与 `BEHAVIOUR_OK` 自检覆盖。
- 迁移只做 dry-run 之前不算完成：孤儿绑定与加密行都要在部署窗口里落库，随后按
  `qa-plan.md` 的 VEC-02 / RET-01 / SEC-02 / BIND-02 取证。
- 凭据轮换未做。两把共享凭据仍然是原来的值，历史上已明文落盘。
