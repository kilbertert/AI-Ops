# 188. 网关不能先于节点上线

日期：2026-10-30
状态：已采纳（设计定型，已实施）

## 决定

两件事，缺一不可：

1. **`deploy-41.sh` 增加「节点制品闸门」**：部署网关之前，取目标 commit 祖先里**最近一次**
   改 `services/execution-node/` 的 commit，要求 41 上实际在跑的节点（读它自己的
   `AIOPS_NODE_BUILD`）等于它或其后代。否则停止部署并说明怎么修。
2. **`images.lock.json` 增加 `node.source_sha256`**，由
   `deploy/node-runtime/source_digest.py` 计算，并由 CI 里的测试比对。源码变了而记录没变 ⇒ 红。

## 依据：一次真实故障

2026-10-30 16:20 CST，CD 把网关自动升到 #1210，节点还停在 #1189 之前。管家侧所有工具
领授权一律 `403 OPERATOR_TOOL_CONTRACT_OUTDATED` —— 混版被两端显式拒绝，正是 ADR-0175
设计的行为，但**没有任何机制阻止它发生**。

查下来是四个事实叠在一起：

| 事实 | 后果 |
| --- | --- |
| `cd.yml` 的 `paths` 只有 `src/**` 等六条 | 节点源码变更不触发 CD |
| `deploy-41.sh` 里零处引用节点 | CD 部署的「运行时」只有网关 |
| 没有任何 workflow 构建 Java | 「节点源码变了」全程没有信号 |
| `images.lock.json` 的 `node.sha256` 是死常量 | 既不等于实际部署的 jar，也不等于 `HOST-CHANGE.md` 声称的源码修订 |

最后一条尤其值得记：当时唯一的测试断言的是「lock 与 `HOST-CHANGE.md` **两个常量彼此一致**」。
两个常量互相点头，谁也没看真实源码 —— 一条给了「制品已核对」假象的测试。

## 为什么判据是「最近一次改节点的 commit」，不是「本次 commit」

后者会漏掉跨 commit 的情形：A 改节点、B 只改 src，B 的网关照样依赖 A 的节点。
「祖先里最近一次改节点的那个」对两种形状都成立。

## 为什么源码摘要放在 CI，jar 摘要放在部署时

CI 不构建 Java（`ci.yml` 只有 ruff / pytest / compileall），所以「用当前源码构建出的 jar
摘要 == lock」这条断言在 CI 里跑不起来 —— 于是「节点源码改了但制品记录没更新」永远没有
信号。拆成两半，各自落在能验证它的地方：

- **CI**：源码摘要。不需要 JDK，也不需要制品。
- **部署时**：jar 摘要与 lock 的 `sha256` 比对。那是制品身份，只能在有制品的地方验
  （`deploy/node-runtime/README.md` 的切换规程第 2 步已经在做）。

## 被否掉的替代形态

**把 `services/execution-node/**` 加进 `cd.yml` 的 `paths`。** 最强的地方是让闸门在
「只改节点」的合并上也会跑。它输在代价与收益倒挂：一次只改节点源码的合并会拉起 CD、
重部署**同一份网关**并重启生产，而这时两端并没有不同版的风险（网关没动）。风险来自
「网关动了而节点没动」，所以闸门只需要在网关真要部署时说话。

**让 CD 顺便构建并部署节点。** 最强的地方是根治：两端永远同版，闸门都不需要。它输在
范围 —— 节点重启有自己的回滚点、就绪等待与验收（`deploy/node-runtime/README.md` 的五步
规程），把它塞进 CD 等于把一套已验证的手工切换流程重写成自动化，那是另一件事，
不该搭在这次止血的车上。

**在 `HOST-CHANGE.md` 里人工记录源码修订。** 这正是现状，也正是它失败的原因：人能忘，
而忘了没有信号。

## 后果与验证

- **闸门读的是运行中的容器，不是 `runtime.env`**：规程先改 env 再重建容器，中间那一段
  env 已指向新节点而旧节点还在跑；读 env 会在这段窗口里误放行，中断在两步之间时那个
  不一致还会一直留着。所以读 `docker inspect aiops-runtime-node` 的 `AIOPS_NODE_BUILD`
  （形如 `<commit12>-<jar12>`），前缀在仓库里解析成 commit，用
  `git merge-base --is-ancestor` 判先后。
- **源码摘要按字节算**：只改注释也要更新记录。这是有意的 —— 制品记录必须指向真实源码，
  而不是「大概那一版」。
- `source_digest.py` 的排除规则只排除 `target/`、`__pycache__`、`.idea`；`pom.xml` 在内。
- 验证：`tests/test_cd_deploy_scripts.py` 钉住闸门的判据来源与 `--is-ancestor` 的**方向**
  （用真实 git 验，因为这条判据的全部风险就在参数顺序上）；
  `tests/test_node_runtime_deploy.py` 钉住「源码摘要 == lock」与「源码变了摘要就变」。
- **CI 仍然不构建 Java**：本记录没有改善这一点，只是让「忘了重建」这件事在 CI 里有了信号。
- **未完成业务验收**：本记录是流程判据，不产出任何业务结论。

方案与分期见 [工具架构](../agents/operator-cpms/tool-architecture.md)。
