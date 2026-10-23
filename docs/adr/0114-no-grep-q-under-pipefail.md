# 114. `set -o pipefail` 下不得用 `grep -q`

日期：2026-10-23
状态：已采纳
影响版本：`deploy/classify-remote-result.sh`、`deploy/deploy-41.sh`、
`deploy/test-rollback-classifier.sh`、`tests/test_cd_deploy_scripts.py`

## 背景：一个「重跑就过」的失败

`verify` 在 `test_rollback_classifier_branches` 上间歇性失败：期望 exit 2 且含「严重」，
实际 exit 1。同一 SHA 重跑即通过，本地连跑 40 次全过。

这种失败最费时间，因为它的形状与「代码有 bug」完全一样，而代码是对的。

## 根因：四步链条

```sh
set -euo pipefail
if printf '%s' "$OUT" | grep -q 'rollback=also-failed'; then … fi
```

1. `grep -q` 在**第一个命中**处立即退出，不读完 stdin；
2. 生产者 `printf` 还没写完，内核向它发 **SIGPIPE**；
3. `pipefail` 把整条管道的状态取为最右失败值 → **141**；
4. `if` 于是把**命中**判成**未命中**。

只在「命中点之后还有大量数据」时触发。数据少时管道缓冲区吸收掉，SIGPIPE 根本不发生 ——
**这就是它在小 fixture 上永远复现不出来、在 CI 上随机出现的原因。**

## 实测

构造「命中在首行 + 其后 20 万行」：

```
grep -q  → hit=0   miss=40     ← 40/40 全部漏判
grep -c  → ok=30   bad=0       ← 稳定
```

在生产分类器上同样复现：

```
$ printf '<200k 行，含 rollback=also-failed>\n' | REMOTE_RC=1 classify-remote-result.sh 41 svc
部署失败（退出码 1），且未产生 selfcheck/rollback 标记 —— 状态未知，须人工确认 41
```

它把「回滚也失败，须立即人工介入」误报成「状态未知」。**误报方向是最坏的那一侧**：
真正的严重故障被降级成「不确定」。

`deploy-41.sh` 的 `/health` 断言同样命中：100k 行时 30/30 误判。今天 `/health` 的响应
很小才没爆——这正是它危险的地方，**它会在响应变大的那天开始失败，而那与本次改动无关。**

## 决定

**在 `set -o pipefail` 的作用域内，不得用 `grep -q` 作为管道的判真条件。** 用
`grep -c … >/dev/null`：`-c` 必须读完 stdin，SIGPIPE 不可能发生，退出码仍为 0/1。

## 不做的事（以及为什么）

`deploy/dify-36/check.sh`（3 处）与 `deploy/m2-live-03/make-certs.sh`（3 处）是同型写法，
本片**未改**：

- `dify-36/check.sh` 的输入是几行配置，远小于管道缓冲区；
- `make-certs.sh` 用的是 `grep -qx`（整行匹配），必须读完才可能命中。

它们将来输入变大时，会以完全相同的方式失败。这条 ADR 的意义就是让那一次能被立刻认出来，
而不是再花一轮把它当 bug 查。

## 备选方案与取舍（被否的路线）

- **去掉 `set -o pipefail`。** 被否：`pipefail` 是这些脚本唯一能发现「管道中段失败」的
  机制；为了让一个判据写法工作而全局拆掉它，会把真正的管道错误也一起放过去。
- **把输入先落到临时文件，再 `grep -q 文件`。** 可行但被否：每条判据多一次 I/O 与
  一个待清理的路径，而 `grep -c` 达到同样效果、不引入新状态。
- **用 bash 内建 `[[ $OUT == *pattern* ]]`。** 被否：它不是正则，要匹配的 `rollback-health=`
  这类前缀语义在多处依赖 `sed`/`grep` 的一致性；换掉会让「读什么」与「发什么」两处
  实现开始漂移——`test_classifier_markers_match_deploy_emitters` 正是为防止这个而存在的。
- **只在分类器里修，不碰 `deploy-41.sh`。** 被否：`/health` 断言实测 30/30 误判，误报
  方向同样是「把成功判成失败」，不修等于留着一个同型缺陷等它随机发作。

## 守卫

`deploy/test-rollback-classifier.sh` 新增用例「大输入下的标记识别（SIGPIPE 回归）」：
标记在首行、其后 20 万行。**把修复撤回，这条转红，且失败文本与当初 CI 的失败一致。**

一个诚实的边界：测试层自己的 `grep -q` 也改成了 `-c`，但**没有测试钉住那一处** ——
实测把测试层改回 `-q` 仍然全绿，因为分类器的输出不够大。它是防御性改动，不是被验证的
改动，记录在此以免被当成「已覆盖」。
