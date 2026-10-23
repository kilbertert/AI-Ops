## 2026-10-23 · 发布步骤在 runner 上静默跳过的根因

> **未完成业务验收。** 这一篇记的是 CI 步骤的失败模式：证据来自在全新 clone 上复现
> 的两次执行。它**不**包含工程师确认的真实故障案例，也**不**是用户侧业务验收。

### 一、症状

`Publish release notes` 步骤失败：

```
cp: cannot stat 'CHANGELOG.md': No such file or directory
```

而 `dev-changelog` 那一行返回 **0**，日志里没有任何说明——因为 stdout 被
`>/dev/null` 吞掉了。

### 二、根因：声明是本机 git config，runner 上没有

`dev-changelog` 只为**声明了** `serverPolicy.publishesVersions` 的仓库渲染。那条声明
是 local git config，**从不入库**（与仓库类同一形态）。runner 的 checkout 是全新 clone，
没有这条声明。

在全新 clone 上复现：

```
$ git clone … && git checkout --detach HEAD
$ dev-changelog .
skip /tmp/rt3: serverPolicy.publishesVersions is not declared
$ echo $?
0
$ ls CHANGELOG.md
ls: cannot access 'CHANGELOG.md': No such file or directory
```

**`skip` 与 `wrote` 都是 exit 0。** 只有文件能区分两者，而当时没人看文件。

### 三、同一次排查里发现的第二个缺陷

第一版写的是 `dev-changelog . >/dev/null`，依赖 cwd 是仓库根、依赖文件名为
`CHANGELOG.md`。状语一多就无法判断失败发生在哪一步。改为 `git rev-parse
--show-toplevel` 显式取根，并**保留 `dev-changelog` 的输出**——它自己会打印
`wrote <path>`，那正是唯一能说明「写到了哪里」的证据。

### 四、改法

```bash
repo_root=$(git rev-parse --show-toplevel)
git config --local serverPolicy.publishesVersions true
git config --local serverPolicy.changelogAtRelease true
dev-changelog "$repo_root"
if [ ! -s "$repo_root/CHANGELOG.md" ]; then
  echo "::error::dev-changelog exited 0 but produced no …/CHANGELOG.md"
  echo "::error::usual cause: serverPolicy.publishesVersions is not declared …"
  exit 1
fi
```

### 五、验证

全新 clone、无声明、detached HEAD：

```
修改前：dev-changelog . → "skip … not declared", rc=0, 无文件
修改后：声明 + dev-changelog "$repo_root" → "wrote …", 文件存在, GUARD PASSED
```
