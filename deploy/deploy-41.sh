#!/usr/bin/env bash
#
# deploy-41.sh — 把本仓 src/ 部署到 41（AI-Ops Gateway 生产环境）。
#
# USAGE:
#   deploy/deploy-41.sh --commit <sha> [--dry-run]
#   deploy/deploy-41.sh --rollback-to <sha> [--dry-run]
#
#   --commit <sha>       部署该 commit 的 src/（通常由 cd.yml 传 $GITHUB_SHA）
#   --rollback-to <sha>  用同一套流程重部署一个已知良好的 commit
#   --dry-run            打包并打印将要执行的远端命令；**不碰 41**
#
# Why this script exists: the deploy was runbook prose (§2 of
# docs/agents/env-41-runbook.md) executed by hand. It is now a script so that
# the automated path and the manual path cannot drift apart — the drift that
# §2's tar/rsync pairing bug came from (see docs/validation.md 2026-09-23).
#
# Two things it deliberately does NOT do:
#   * business acceptance — that needs a thirdSession issued by the business
#     side; §5/§6 forbid CI from self-certifying it. This proves technical
#     health only.
#   * invent its own host transport — it drives `dev-host`, which already
#     enforces the policy gate (identity assertion + --artifact-sha256).
#     A hand-rolled ssh+rsync here would bypass "changes reach a service host
#     only as an identified artifact".
set -euo pipefail

PROG=${0##*/}
HOST_TARGET=41
REMOTE_SRC=/opt/aiops-41/src
SERVICE=aiops-gateway-41.service
HEALTH=http://127.0.0.1:8788/health
# 41 本机 health 只在 loopback；公网入口是 api.mall.qushiyun.com，但那只用于
# 业务验收（§5），不放这里做。

usage() {
  sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
}

die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 1; }
# 暂存目录：同时承载 CD registry 视图与打包内容。尽早创建，因为身份准备在
# 取源码之前就要用到它。
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
STAGE_REGISTRY="$STAGE/registry"
mkdir -p "$STAGE_REGISTRY"
step() { printf '\n== %s ==\n' "$*"; }
info() { printf '   %s\n' "$*"; }

COMMIT=""
ROLLBACK_TO=""
DRY_RUN=0
while [ $# -gt 0 ]; do
  case $1 in
    --commit) [ $# -ge 2 ] || die "--commit 需要参数"; COMMIT=$2; shift 2 ;;
    --rollback-to) [ $# -ge 2 ] || die "--rollback-to 需要参数"; ROLLBACK_TO=$2; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（-h 看用法）" ;;
  esac
done

if [ -n "$COMMIT" ] && [ -n "$ROLLBACK_TO" ]; then
  die "--commit 与 --rollback-to 只能给一个"
fi
TARGET_COMMIT=${COMMIT:-$ROLLBACK_TO}
[ -n "$TARGET_COMMIT" ] || die "必须给 --commit <sha> 或 --rollback-to <sha>"

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

# ---- 0. 环境前置检查 -------------------------------------------------------
# dev-host 用 `python3 -c` 解析 TOML，需要 tomllib（Python 3.11+）。在自托管
# runner 的身份下 PATH 不含 miniconda，裸 python3 会落到 /usr/bin/python3 (3.10)
# 并抛 ModuleNotFoundError。那是「环境不对」，不是「部署失败」——分开报，否则
# 会把人引向错误的排查方向。
step "环境前置检查"
if ! command -v dev-host >/dev/null 2>&1; then
  die "dev-host 不在 PATH。自托管 runner 的 PATH 应含 /home/claude/.local/bin"
fi
if ! dev-host show "$HOST_TARGET" >/dev/null 2>&1; then
  cat >&2 <<'EOF'
环境前置失败：`dev-host show 41` 跑不通。

最可能的原因：python3 解析到 /usr/bin/python3 (3.10)，没有 tomllib。
修法（本机已验证）——把 miniconda 前置到 PATH 再调用本脚本：

  PATH=/home/claude/miniconda3/bin:$PATH deploy/deploy-41.sh ...
EOF
  exit 65
fi
info "dev-host 可用（$(command -v dev-host)）"

# ---- CD 身份：用 CD 专用别名，而不是人工别名 --------------------------------
# dev-host 不做 identity 覆盖（裸 ssh/scp，无 -F），而 `HOME` 对 ssh 无效 ——
# OpenSSH 从 passwd 展开 `~`，`env -i HOME=x` 也照样读 ~/.ssh/config（实测）。
# 所以「隔离 HOME」不是可用的手段；能改变身份的只有 ssh 别名。
#
# 这里不往主清单里加第二条 41 记录 —— 策略规定角色/信任平面/归属只记一处，
# 重复过就是两份记录打架的来源。改为**运行时派生**一份视图：从主清单读全部字段，
# 只把 ssh_alias 换成 CD 别名。角色与归属的唯一真值仍是主清单。
# **默认人工别名**，不是 CD 别名。同一个脚本既服务自动化部署，也服务人工应急回滚；
# 而应急场景恰恰可能是「CD 密钥已被吊销」（CD 出事或被入侵时第一件事就是吊销它）。
# 若默认选 CD 身份，那条应急路径会在最需要它的时候认证失败。
# 自动化路径由 cd.yml 显式设 CD_SSH_ALIAS=aiops-41-cd 选用 CD 身份。
CD_ALIAS=${CD_SSH_ALIAS:-aiops-41}
CANONICAL_REGISTRY=${DEV_HOST_REGISTRY:-$HOME/.config/dev-host/hosts.toml}
[ -r "$CANONICAL_REGISTRY" ] || die "读不到主机清单：$CANONICAL_REGISTRY"

CD_REGISTRY="$STAGE_REGISTRY/hosts.toml"
python3 - "$CANONICAL_REGISTRY" "$CD_REGISTRY" "$CD_ALIAS" <<'PY'
import sys, tomllib
src, dst, alias = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src, 'rb') as f:
    data = tomllib.load(f)
entry = (data.get('hosts') or {}).get('aiops-41')
if not isinstance(entry, dict):
    sys.exit('canonical registry has no [hosts.aiops-41] entry')
view = dict(entry)
view['ssh_alias'] = alias
with open(dst, 'w') as f:
    f.write('[hosts.aiops-41]\n')
    for k, v in view.items():
        f.write('%s = "%s"\n' % (k, str(v).replace('\\', '\\\\').replace('"', '\\"')))
PY

# 断言派生视图与主清单除 ssh_alias 外完全一致 —— 这样「唯一真值在主清单」不是
# 靠约定，而是每次运行都验证。
python3 - "$CANONICAL_REGISTRY" "$CD_REGISTRY" <<'PY'
import sys, tomllib
def load(p):
    with open(p, 'rb') as f:
        return (tomllib.load(f).get('hosts') or {}).get('aiops-41', {})
a, b = load(sys.argv[1]), load(sys.argv[2])
diff = {k for k in set(a) | set(b) if k != 'ssh_alias' and a.get(k) != b.get(k)}
if diff:
    sys.exit('CD registry view diverged from the canonical registry: ' + ', '.join(sorted(diff)))
PY
export DEV_HOST_REGISTRY="$CD_REGISTRY"
info "CD 身份别名=$CD_ALIAS（角色/归属来自主清单，已核对一致）"

# 身份断言：dev-host 自己会比对主机公钥，这里只确认角色与归属没变。
host_role=$(dev-host show "$HOST_TARGET" | awk '$1=="role"{print $2}')
host_status=$(dev-host show "$HOST_TARGET" | awk '$1=="status"{print $2}')
[ "$host_role" = "service-host" ] || die "41 角色变了（$host_role），期望 service-host；停下"
[ "$host_status" = "active" ] || die "41 状态是 $host_status，不是 active；停下"
info "41 角色=$host_role 状态=$host_status（写入将需要产物身份）"

# ---- 1. 取出目标 commit ----------------------------------------------------
step "准备 $TARGET_COMMIT 的源码"
git cat-file -e "${TARGET_COMMIT}^{commit}" 2>/dev/null \
  || die "仓库里没有 commit $TARGET_COMMIT（自托管 runner 需要完整 main 历史）"
FULL_SHA=$(git rev-parse "$TARGET_COMMIT^{commit}")
SHORT_SHA=$(git rev-parse --short=12 "$FULL_SHA")
# FULL_SHA/SHORT_SHA 会被插进远端命令（文件名、断言文本）。git rev-parse 的契约是
# 产出 hex，所以实践上安全；但「实践上安全」不该是这里唯一的保证 —— 下面显式断言，
# 把这条不变量变成脚本自己维护的，而不是依赖外部工具当前的行为。
case $FULL_SHA in
  *[!0-9a-f]*) die "commit 解析出了非 hex 的 sha：$FULL_SHA" ;;
esac
case $SHORT_SHA in
  *[!0-9a-f]*) die "short sha 含非 hex 字符：$SHORT_SHA" ;;
esac
info "commit=$FULL_SHA (${SHORT_SHA})"
info "subject=$(git log -1 --format=%s "$FULL_SHA" | cut -c1-70)"

WORK="$STAGE/src"

# 用 git archive 取该 commit 的 src/，而不是用工作树 —— 交付物必须是 commit 的
# 内容，不是某人 checkout 到哪。落地即：src/aiops_diagnostics/...
git archive "$FULL_SHA" src/aiops_diagnostics | tar -x -C "$STAGE"
[ -d "$WORK/aiops_diagnostics" ] || die "该 commit 没有 src/aiops_diagnostics"

# 诊断运行时会从 reference_root()（解析到 /opt/aiops-41）读取这几份文件并拷进每个
# workspace。它们不是 src/ 下的代码，若不同步，`/health` 会报新 commit 而诊断实际用的是
# 旧 SOP/架构文档 —— 「部署了什么」与「跑了什么」不一致。
# 只纳入 git 跟踪、且解包后仍保留仓库相对路径的那几份。
REFERENCE_FILES="SOP.md 充电桩问题排查SOP.md docs/architecture.md"
for ref in $REFERENCE_FILES; do
  if ! git cat-file -e "$FULL_SHA:$ref" 2>/dev/null; then
    # **不跳过，停止部署。** 跳过意味着「不同步也不删除」：41 上的旧副本会留下，
    # 而诊断每次仍从 /opt/aiops-41 读它 —— 于是 main 删掉某份参考资料后，生产继续
    # 使用已删除的内容，且没有任何信号。
    #
    # 有意**不**自动删除 41 上的旧副本：那是诊断的运行输入，由 CI 单方面删掉比留下
    # 更难恢复。把决定交回给人：要么恢复该文件，要么明确把 REFERENCE_FILES 改掉
    # （那是一次单独的、被评审核过的改动）。
    cat >&2 <<EOF
目标 commit ($SHORT_SHA) 缺少受管的运行时参考资料：$ref

诊断会从 /opt/aiops-41 读这份文件并拷进每个 workspace。若在此跳过，41 上的旧副本
会继续被使用 —— main 已删除的内容仍在生产生效，且不会报错。

已停止，未上传、未写入、未重启。请二选一：
  1. 恢复 $REFERENCE_FILES 中列出的这份文件；或
  2. 明确把 $ref 从 deploy/deploy-41.sh 的 REFERENCE_FILES 移除
     （同时处理 41 上的旧副本），走一次单独评审的改动。
EOF
    exit 1
  fi
  mkdir -p "$WORK/$(dirname "$ref")"
  git show "$FULL_SHA:$ref" > "$WORK/$ref"
done
info "参考资料已纳入产物：$(printf '%s' "$REFERENCE_FILES" | tr ' ' ',')"

# ---- 1a. 过期审批门禁（只对 --commit；--rollback-to 是有意的回退）--------------
# 场景：commit A 与 B 依次合并，两个 CD run 都停在审批门。审核者先批 B（生产变成 B），
# 之后误批队列里仍在的 A —— A 的 job 拿到并发锁，按**自己的** GITHUB_SHA 部署，
# 生产从 B 退回 A。
#
# 这是把并发控制放到 job 级的代价（评审指出）：job 级锁只保证**串行**，不保证**顺序** ——
# 谁先获批谁先跑，而 workflow 级原本会取消旧的等审批 run，不存在陈旧 run。
# 所以补这道门禁：**目标 commit 若是当前生产版本的祖先，说明它已被更新部署取代，拒绝。**
#
# 只对 --commit 生效。`--rollback-to` 的目的就是部署一个更旧的 commit，那是显式的人为
# 决定，不该被这道门禁挡住。
if [ -z "$ROLLBACK_TO" ]; then
  step "过期审批门禁"
  CURRENT_VERSION=$(dev-host exec "$HOST_TARGET" --allow-service-exec -- \
    "curl -s --max-time 6 $HEALTH" | tail -1 \
    | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("version",""))
except Exception:
    print("")' 2>/dev/null)
  CURRENT_SHA=$(printf '%s' "$CURRENT_VERSION" | sed -n 's/.*+\([0-9a-f]\{7,\}\)$/\1/p')
  if [ -z "$CURRENT_SHA" ]; then
    info "读不到 41 当前版本（$CURRENT_VERSION）—— 无法做顺序检查，继续"
  elif ! git cat-file -e "${CURRENT_SHA}^{commit}" 2>/dev/null; then
    info "41 当前版本 $CURRENT_SHA 不在本仓历史里 —— 无法做顺序检查，继续"
  elif [ "$CURRENT_SHA" = "$FULL_SHA" ]; then
    info "41 已跑该 commit（$SHORT_SHA）—— 幂等重跑，继续"
  elif git merge-base --is-ancestor "$FULL_SHA" "$CURRENT_SHA" 2>/dev/null; then
    cat >&2 <<EOF
拒绝：目标 commit 已被更新部署取代（过期审批）

  本次要部署   $SHORT_SHA（$FULL_SHA）
  41 当前在跑  $(git rev-parse --short=12 "$CURRENT_SHA")（$CURRENT_SHA）

目标 commit 是当前生产版本的**祖先**，说明它之后已经有更新的部署成功过。此刻部署它
等于把生产**回退**到旧版本。这通常意味着：两个 run 都停在审批门，审核者先批了较新的
那个，之后又误批了队列里仍在的旧 run。

已停止，未上传、未写入、未重启。请二选一：
  1. 若确实要停在当前版本 —— 什么都不做，关掉那个旧 run 的审批请求即可；
  2. 若确实要回退到该版本 —— 这是一次**有意的回退**，用显式命令：
       deploy/deploy-41.sh --rollback-to $FULL_SHA
     （--rollback-to 不受本门禁限制，因为它表达的就是"我要回到旧版本"。）
EOF
    exit 1
  else
    info "顺序检查通过（目标 $SHORT_SHA 不是当前 $CURRENT_SHA 的祖先）"
  fi
fi

# ---- 1b. 参考资料前置检查（**保守**：不存在就拒绝部署，不猜回滚）--------------
# 每份受管的参考资料在 41 上都必须**已经存在**。理由：
#
#   1. 它们本来就是生产上既有的运行输入（`reference_root()` 读 /opt/aiops-41）。
#      部署只负责把它们更新到目标 commit 的版本，不负责从无到有地引入。
#   2. 「部署前不存在」意味着回滚要**删除**它 —— 那是比更新更重的动作，且必须知道
#      它原本不该在。评审指出：旧实现只按「有没有备份」判断，会把新建的文件留在
#      盘上却报「完整回滚」（假声明）。
#   3. 我们试过实现「按部署前存在状态决定回滚动作」，但在真机上无法可靠复现 ——
#      主机侧探针显示检查时文件已存在、而部署前我们刚确认它不在（根因未定位）。
#      与其交付一个自己都不信的回滚分支，不如把这种情况挡在部署之前。
#
# 代价：首次引入一份新参考资料时需要人工在 41 上先放置它（一次性动作），
# 以及显式更新 REFERENCE_FILES。这是有意的取舍 —— 宁可要人做一次，
# 不要脚本去猜一个删文件的回滚。
step "参考资料前置检查"
for ref in $REFERENCE_FILES; do
  present=$(dev-host exec "$HOST_TARGET" --allow-service-exec -- \
    "[ -f /opt/aiops-41/$ref ] && echo yes || echo no" | tail -1 | tr -d '[:space:]')
  if [ "$present" != "yes" ]; then
    cat >&2 <<EOF
受管的运行时参考资料在 41 上不存在：$ref

部署只把它更新到目标 commit 的版本，不负责从无到有地引入。若在此继续，回滚就得
**删除**它 —— 那需要知道它原本不该在，而按「有没有备份」判断会把新建的文件留在
盘上却报「完整回滚」（假声明，评审指出过）。

已停止，未上传、未写入、未重启。请二选一：
  1. 在 41 上放置该文件（人工一次性动作）：
       install -o aiops41 -g aiops41 -m 0640 <file> /opt/aiops-41/$ref
     再重跑本部署；或
  2. 若它确实不该再是受管参考资料，把它从 deploy/deploy-41.sh 的 REFERENCE_FILES
     移除（走一次单独评审的改动）。
EOF
    exit 1
  fi
  info "$ref 在 41 上存在 ✓"
done

# ---- 2. 注入 commit 标识 ---------------------------------------------------
# /health 的 version 来自 src/aiops_diagnostics/__init__.py 的 __version__，是个
# 静态 semver —— 部署后无法回答「现在跑的是哪个 commit」。在**暂存副本**上把它
# 改写成 <semver>+<short-sha>，让 /health 能自证。不动工作树。
#
# pyproject.toml 里另有一份独立的 version，与 __init__.py 本就不同步；这里只改
# 运行时那份（editable 安装实际 import 的那个），不去合并两者。
step "注入 commit 标识"
INIT="$WORK/aiops_diagnostics/__init__.py"
[ -f "$INIT" ] || die "缺少 $INIT"
BASE_VERSION=$(sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$INIT" | head -1)
[ -n "$BASE_VERSION" ] || die "无法从 __init__.py 读出 __version__"
STAMPED="${BASE_VERSION}+${SHORT_SHA}"
python3 - "$INIT" "$BASE_VERSION" "$STAMPED" <<'PY'
import re, sys, pathlib
path, base, stamped = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
source = p.read_text(encoding="utf-8")
# 只替换第一个赋值，且要求行首锚定，避免误伤别处的同名子串。
new, n = re.subn(rf'(?m)^__version__ = "{re.escape(base)}"$',
                 f'__version__ = "{stamped}"', source, count=1)
if n != 1:
    sys.exit(f"expected exactly one __version__ line to rewrite, got {n}")
p.write_text(new, encoding="utf-8")
PY
info "__version__ → $STAMPED"

# 文件数：部署后用它探测 sync-check 残留污染（--delete 只管目标侧，见 §2）
EXPECTED_FILES=$(find "$WORK/aiops_diagnostics" -type f -not -path '*__pycache__*' | wc -l | tr -d ' ')
info "文件数（预期）= $EXPECTED_FILES"

# ---- 3. 打包 + 首层断言 ----------------------------------------------------
step "打包"
TARBALL="$STAGE/aiops-sync.tar.gz"
# 包内首层仍是 aiops_diagnostics/（下面的断言依赖这一点）；参考资料作为同层的额外条目。
# 显式构造条目列表：REFERENCE_FILES 是有意按空白拆分的路径清单，用数组承载，
# 避免把「未加引号的命令替换」当成约定（那样既难读，也容易在改动时出错）。
TAR_ENTRIES=(aiops_diagnostics)
for ref in $REFERENCE_FILES; do
  [ -e "$WORK/$ref" ] && TAR_ENTRIES+=("$ref")
done
tar -czf "$TARBALL" -C "$WORK" "${TAR_ENTRIES[@]}"
# 先把清单读进变量，再取首行。**不要**写 `tar tzf ... | head -1`：head 读一行即退出，
# 关掉管道，tar 收到 SIGPIPE 而死；在 `set -o pipefail` 下 141 会被当成脚本失败
# （实测）。断言本身没错，是取首行的手法会自杀。
TAR_LIST=$(tar tzf "$TARBALL")
FIRST=$(printf '%s\n' "$TAR_LIST" | sed -n '1p')
[ "$FIRST" = "aiops_diagnostics/" ] \
  || die "包内首层是 $FIRST，期望 aiops_diagnostics/（rsync 源会随之错位，停下）"
ARTIFACT_SHA=$(sha256sum "$TARBALL" | cut -d' ' -f1)
info "首层=$FIRST"
info "大小=$(du -h "$TARBALL" | cut -f1)  产物 sha256=${ARTIFACT_SHA:0:16}…"

# SHORT_SHA 已断言为纯 hex，因此这个路径不含空格或 shell 元字符；它在远端命令里
# 以未加引号的形式出现（远端块本来就是一段 shell 文本），这条断言是该做法成立的前提。
REMOTE_TARBALL="/tmp/aiops-sync-${SHORT_SHA}.tar.gz"

# ---- 3b. 依赖漂移检查 ------------------------------------------------------
# 产物只含 src/，41 上的依赖环境（/opt/aiops-41/.venv）在机上有它自己的 pyproject/uv.lock。
# 两者不一致时，restart 会以 ModuleNotFoundError 起不来 —— 服务从 active 掉到 failed。
#
# 这一版**检测并拒绝**，不自动在 41 上装依赖：那是一台承载生产、没有 uv 的公司主机，
# 装依赖属于「改运行环境」，不是「传一次源码」，需要独立决定与独立验证（见本 PR 说明的
# 「依赖漂移」一节）。宁可停止部署并说清楚，也不把生产交给一个可能起不来的进程。
#
# 注意 pyproject/uv.lock 目前不在 cd.yml 的触发路径里；若哪天它们变了，下面的检查会
# 在这里拦下，而不是让部署"成功"后服务挂掉。
step "依赖漂移检查"
# **读的是 TARGET_COMMIT 的 manifest，不是当前 checkout。** 回滚场景下两者可能不同：
# 若拿当前 checkout 去比，只能证明「操作者的工作区与主机一致」，证明不了「要部署的那个
# commit 与主机环境兼容」—— 于是旧源码会被放进新依赖环境里跑。
# 从 git 取目标 commit 的这两份文件，与 41 上的比。
for spec in "pyproject.toml" "uv.lock"; do
  local_sha=$(git show "$FULL_SHA:$spec" 2>/dev/null | sha256sum | cut -d' ' -f1 || true)
  if [ -z "$local_sha" ]; then
    info "$spec：目标 commit 里没有该文件 —— 跳过（无法比较）"
    continue
  fi
  remote_sha=$(dev-host exec "$HOST_TARGET" --allow-service-exec -- \
    "sha256sum /opt/aiops-41/$spec 2>/dev/null | cut -d' ' -f1" | tail -1 | tr -d '[:space:]')
  if [ -z "$remote_sha" ]; then
    info "$spec：41 上没有该文件 —— 无法验证依赖一致性（记为已知缺口）"
    continue
  fi
  if [ "$local_sha" != "$remote_sha" ]; then
    cat >&2 <<EOF
依赖漂移：目标 commit 的 $spec 与 41 上的不一致
  目标 commit ($SHORT_SHA) $local_sha
  41                       $remote_sha

产物只含 src/，不会同步依赖。此时若继续部署，restart 可能因缺包而失败
（服务从 active 掉到 failed）。已停止，未上传、未写入、未重启。

处理方式（不在本脚本范围 —— 它改的是运行环境）：
  按 docs/agents/env-41-dependency-update.md 的人工流程更新 41 的依赖环境，再重跑
  部署。该流程要求备份、editable 守卫与留记录。**不要在这里顺手装包**：
  uv sync 会把 editable 安装换成实体目录，之后 src/ 同步会静默失效
  （import 仍成功，但拿的是旧代码，而 /health 还报新 commit）。
EOF
    exit 1
  fi
  info "$spec 与目标 commit 一致 ✓"
done

# 备份保留份数（本机侧决定，插值进远端命令）。用环境变量可覆盖，用于验证修剪分支。
KEEP_BACKUPS=${KEEP_BACKUPS:-20}
case $KEEP_BACKUPS in
  ''|*[!0-9]*) die "KEEP_BACKUPS 必须是正整数，得到：$KEEP_BACKUPS" ;;
  0) die "KEEP_BACKUPS=0 会把本次刚建的备份也删掉 —— 重启失败就没有恢复点了" ;;
esac

# 远端块的构造：把它写成一条已引用好的命令交给 dev-host exec（它按 ssh argv 语义
# 转发，需要 shell 语法时必须整体引用）。
#
# 关键：解包前 `rm -rf /tmp/sync-check`。rsync --delete 只删目标侧多出的文件，
# 不管源侧 —— 残留目录会被当成本次内容同步到生产。这是真实踩过的坑（#386）。
read -r -d '' REMOTE_CMD <<EOF || true
REFERENCE_FILES="${REFERENCE_FILES}"
set -eu
TS=\$(date +%Y%m%d-%H%M%S)
B=/var/backups/aiops-41/backup-\$TS
mkdir -p "\$B/refs"
cp -a $REMOTE_SRC "\$B"/
# 清理上一次被 kill 的部署留下的产物。正常路径与回滚路径都会删掉自己的 tar，
# 但**被超时/断线 kill 的那次**删不了 —— 实测留下过 24MB 残留（2026-09-23 钻演）。
#
# **必须排除本次的包**：本次的 tar 在部署命令之前就已上传，用通配全部删除会把它一并
# 删掉，紧接着的解包必然失败（钻演实测：mutate-failed=extract, rsync-src）。
#
# 保留判据是**精确路径相等**，不是「文件名含本次 SHA」：后者会放过一个同 SHA 的
# 陈旧残留（评审指出）—— 那个残留不该存在，且它的内容无从验证。
for stale in /tmp/aiops-sync-*.tar.gz; do
  [ -e "\$stale" ] || continue
  [ "\$stale" = "$REMOTE_TARBALL" ] && continue
  rm -f "\$stale"
done
# 取 /health 的 version 字段。用 python3 解析而不是 grep/sed 搜子串 —— 那种做法会
# 命中响应里任意位置，而判据要的是「version 字段恰好等于期望值」。41 上有 python3。
# 定义放在最前面，因为部署前的基线记录与部署后的自检**必须用同一把尺**：两处解析方式
# 不同时，一次响应格式变化就会把成功的恢复误报成失败。
health_version() {
  curl -s --max-time 6 $HEALTH \
    | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("version",""))
except Exception:
    print("")' 2>/dev/null
}
# 部署前记录当前 /health 报的版本 —— 回滚成功的判据要与它相等，而不是只看 active。
PRE_VERSION=\$(health_version)
[ -n "\$PRE_VERSION" ] || PRE_VERSION='(部署前未能读到版本)'
echo "pre-version=\$PRE_VERSION"

# =====================================================================
# 变更阶段：**所有写操作累积状态，绝不提前退出。**
#
# 远端块带 set -e。若让 rsync/mkdir/cp/chown 里任何一个直接失败退出，控制流就到不了
# 下面的自检与回滚 —— 生产停在「源已换、服务未重启」的半部署状态，本机只看到「状态
# 未知」。这个坑我按实例修过四次（首次 restart、回滚 restart、恢复动作、变更阶段），
# 每次都是同一形状。所以这里改成结构性的：一个阶段累积一个状态，任何非零都汇入同一
# 条恢复路径，不再逐个 if 包。
# =====================================================================
mutate_rc=0
step_failed() { echo "mutate-failed=\$1"; mutate_rc=1; }

rm -rf /tmp/sync-check && mkdir -p /tmp/sync-check || step_failed "prepare-sync-dir"
tar xzf $REMOTE_TARBALL -C /tmp/sync-check || step_failed "extract"
rsync -a --delete /tmp/sync-check/aiops_diagnostics/ $REMOTE_SRC/aiops_diagnostics/ || step_failed "rsync-src"
# 参考资料：与源码分开搬，因为它们的落点是 /opt/aiops-41 而不是 src/。
# reference_root() 解析到 /opt/aiops-41（该目录有 pyproject.toml + src/），
# 诊断每次运行都从这里读这几份文件 —— 不同步就会「报新 commit、用旧 SOP」。
for ref in $REFERENCE_FILES; do
  if [ -f "/tmp/sync-check/$ref" ]; then
    mkdir -p "/opt/aiops-41/$(dirname "$ref")" "\$B/refs/$(dirname "$ref")" \
      || { step_failed "refs-mkdir:$ref"; continue; }
    # 备份后覆盖。参考资料的**存在性**已由本脚本的前置检查（step "参考资料前置检查"）
    # 保证 —— 因此这里不需要再判断 present/absent，回滚也不必删文件、只需还原。
    # 之前那版按存在状态决定回滚动作，在真机上无法可靠复现（见该前置检查处的说明），
    # 已按保守做法改为「不存在就拒绝部署」。
    cp "/opt/aiops-41/$ref" "\$B/refs/$ref" || { step_failed "refs-backup:$ref"; continue; }
    cp "/tmp/sync-check/$ref" "/opt/aiops-41/$ref" || { step_failed "refs-copy:$ref"; continue; }
    echo "reference-synced=$ref"
  fi
done
chown -R aiops41:aiops41 $REMOTE_SRC || step_failed "chown"

# restart 也累积：systemctl restart 非零时必须继续走自检/回滚，而不是退出。
# 注意 restart 是异步的：服务起不来时它**常常仍返回 0**（fork 完成即返回），
# 所以「restart 返回 0」不能推出「服务起来了」；真正的判据是下面的 check_ok。
if systemctl restart $SERVICE; then :; else echo "deploy-restart-rc=nonzero"; fi
sleep 5
echo "backup=\$B"

# ---- 自检与自动回滚（都在主机上做）----
# 自检放主机侧，因为回滚只能用主机上的备份做。若自检在本机、回滚在主机，中间网络
# 断了就会分裂成「本机以为失败 / 主机其实已切换」，两边都不确定。判据只取本机也能
# 独立复核的两项：服务 active + /health 含本次 commit 版本。
EXPECT_VERSION="${STAMPED}"
check_ok() {
  # mutate_rc 必须为 0：变更阶段有任何一步失败，即使服务恰好起来了，也不算成功 ——
  # 那意味着源码与参考资料可能只同步了一部分。
  [ "\$mutate_rc" = "0" ] || return 1
  [ "\$(systemctl is-active $SERVICE)" = "active" ] || return 1
  [ "\$(health_version)" = "\$EXPECT_VERSION" ] || return 1
  return 0
}
if check_ok; then
  echo "selfcheck=passed"
  # 备份修剪是**非关键维护**，放在自检通过之后：放前面的话，一次磁盘/权限问题就会
  # 阻断恢复路径，而修剪失败本身不影响这次部署是否正确。
  #
  # **只删本脚本自己造的备份**：备份名是 backup-<14位时间戳>。人工或别的工具留下的
  # （例如 backup-20260915-pre-621490d 那种带标记的）不在修剪范围 —— 精简磁盘不是删除
  # 别人产物的理由，何况备份正是回滚时要用的。实测中曾把一个人工备份误删，故加此约束。
  LATEST_PATTERN='backup-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'
  own=\$(ls -1d /var/backups/aiops-41/\$LATEST_PATTERN 2>/dev/null | wc -l)
  others=\$(( \$(ls -1d /var/backups/aiops-41/backup-* 2>/dev/null | wc -l) - own ))
  if [ "\$own" -gt "${KEEP_BACKUPS}" ]; then
    ls -1d /var/backups/aiops-41/\$LATEST_PATTERN 2>/dev/null | sort \
      | head -n "\$((own - ${KEEP_BACKUPS}))" \
      | while read -r old; do rm -rf "\$old"; echo "pruned-backup=\${old##*/}"; done || true
  fi
  [ "\$others" -gt 0 ] && echo "note: \$others non-CD backup(s) present, not pruned"
else
  echo "selfcheck=failed" >&2
  echo "rolling back to \$B ..." >&2
  # 回滚两样东西：源码 + 参考资料。两者都在本次部署里被改过，只回源码会留下
  # 「旧代码 + 被拒 commit 的 SOP」—— 服务能起来，但诊断读的是被拒版本的内容。
  # 恢复动作累积状态、不做提前退出：这些命令若失败而 set -e 直接终止，标记就发不出去，
  # 分类器把它当「状态未知」—— 而它其实是「回滚失败」，要人做的事完全不同。
  restore_rc=0
  rsync -a --delete "\$B/src/" $REMOTE_SRC/ || restore_rc=1
  for ref in $REFERENCE_FILES; do
    # 参考资料一定原本存在（前置检查保证），所以回滚就是「从备份还原」。
    # 唯一的不确定是备份本身是否完好 —— 那由 cp 的退出码如实反映，不猜。
    if [ -f "\$B/refs/$ref" ]; then
      mkdir -p "/opt/aiops-41/$(dirname "$ref")" || restore_rc=1
      cp "\$B/refs/$ref" "/opt/aiops-41/$ref" || restore_rc=1
    else
      echo "refs-backup-missing=$ref" >&2
      restore_rc=1
    fi
  done
  chown -R aiops41:aiops41 $REMOTE_SRC || restore_rc=1
  [ "\$restore_rc" = "0" ] || echo "rollback-restore-rc=nonzero"

  # 判定回滚是否成功必须用与部署前**同一把尺**：服务 active **且** /health 报的
  # 版本等于部署前记录的那个。只看 active 不够 —— systemd 可以是 active 而 HTTP
  # 监听起不来（初始化卡住），那时报 healthy 就是谎报。
  #
  # 所有标记都走 stdout：本机用 $( ) 只捕获 stdout，写 stderr 的标记到不了分类器。
  # restart 用 if 包住：远端块带 set -e，非零的 restart 会直接终止，连标记都发不出。
  if systemctl restart $SERVICE; then :; else echo "rollback-restart-rc=nonzero"; fi
  sleep 5
  # 三个条件缺一不可：恢复动作全部成功 **且** 服务 active **且** /health 版本等于部署前。
  # 少了 restore_rc 这条，就会把「参考资料只回了一半、但版本恰好还是旧的」报成
  # 完整回滚 —— 服务看起来正常，而诊断读的是半新半旧的输入。
  if [ "\$restore_rc" = "0" ] \
     && [ "\$(systemctl is-active $SERVICE)" = "active" ] \
     && [ "\$(health_version)" = "\$PRE_VERSION" ]; then
    echo "rollback=restored version=\$PRE_VERSION"
  else
    echo "rollback=also-failed"
    # is-active 用 || true 兜住：服务 inactive 时它非零，set -e 会在这里终止，
    # 下面的标记与清理就都发不出去 —— 而这正是最需要它们被发出去的情形。
    echo "rollback-health=\$(curl -s --max-time 6 $HEALTH || echo '(no response)')"
    echo "rollback-active=\$(systemctl is-active $SERVICE || true)"
  fi
  rm -rf /tmp/sync-check $REMOTE_TARBALL || true
  exit 1
fi

systemctl is-active $SERVICE
curl -s --max-time 6 $HEALTH
rm -rf /tmp/sync-check $REMOTE_TARBALL
EOF

# ---- 4. 干跑出口 -----------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  step "dry run：以下是将要执行的远端操作，未连接 41"
  cat <<EOF
dev-host cp $HOST_TARGET <tarball> --dest $REMOTE_TARBALL --artifact-sha256 $ARTIFACT_SHA

dev-host exec $HOST_TARGET --allow-service-exec -- '<下面这段>'
$REMOTE_CMD
EOF
  info "dry run 结束：未上传、未写入、未重启"
  exit 0
fi

# ---- 5. 上传（产物身份在此强制）--------------------------------------------
step "上传产物"
# --artifact-sha256 让策略门在**这一步**生效：dev-host 会核对载荷 sha，不符即拒
# （退出 77）。不是靠本脚本自觉声明。
dev-host cp "$HOST_TARGET" "$TARBALL" --dest "$REMOTE_TARBALL" --artifact-sha256 "$ARTIFACT_SHA"
info "已上传 $REMOTE_TARBALL（产物身份 $ARTIFACT_SHA 已核对）"

# ---- 6. 远端部署 -----------------------------------------------------------
step "远端部署"
# 超时要够。**超时设短不是更保守，而是更危险**：命令被 kill 在中途时，主机上的
# 自检与回滚块根本没机会跑，生产会停在「源已换、服务未重启」的分裂状态
# （2026-09-23 钻演实测踩到，见 validation.md）。
REMOTE_TIMEOUT=${REMOTE_TIMEOUT:-900}
case $REMOTE_TIMEOUT in
  ''|*[!0-9]*) die "REMOTE_TIMEOUT 必须是正整数秒数，得到：$REMOTE_TIMEOUT" ;;
  0) die "REMOTE_TIMEOUT=0 会让远端命令立即被 kill —— 回滚块没有机会执行" ;;
esac
set +e
REMOTE_OUT=$(dev-host exec "$HOST_TARGET" --allow-service-exec --timeout "$REMOTE_TIMEOUT" -- "$REMOTE_CMD")
REMOTE_RC=$?
set -e
printf '%s\n' "$REMOTE_OUT" | sed 's/^/   /'
BACKUP=$(printf '%s\n' "$REMOTE_OUT" | sed -n 's/^backup=//p' | head -1)
info "备份=$BACKUP"

if [ "$REMOTE_RC" -ne 0 ]; then
  # 分类逻辑抽在 deploy/classify-remote-result.sh，因为它有三个容易错的判断
  # （远端 set -e、标记写 stderr、只看 active 不看 /health），而那些错法只在
  # 特定分支显现。抽出来才能对每个分支做测试（见 test-rollback-classifier.sh）。
  printf '%s' "$REMOTE_OUT" | REMOTE_RC="$REMOTE_RC" \
    "$REPO_ROOT/deploy/classify-remote-result.sh" "$HOST_TARGET" "$SERVICE" >&2
  exit $?
fi

step "部署后验证"

ACTIVE=$(dev-host exec "$HOST_TARGET" --allow-service-exec -- "systemctl is-active $SERVICE" | tail -1)
[ "$ACTIVE" = "active" ] || die "服务不是 active（$ACTIVE）"
info "服务 active ✓"

LIVE_HEALTH=$(dev-host exec "$HOST_TARGET" --allow-service-exec -- "curl -s --max-time 6 $HEALTH" | tail -1)
printf '%s\n' "$LIVE_HEALTH" | grep -q '"ok":true' || die "/health 未返回 ok:true：$LIVE_HEALTH"
info "/health ok ✓"

# 核心断言：/health 报出的 version 必须含本次 short sha —— 这才叫「证明部署了什么」。
printf '%s' "$LIVE_HEALTH" | grep -q "$SHORT_SHA" \
  || die "/health 的 version 不含本次 commit（$SHORT_SHA）。实际：$LIVE_HEALTH"
info "/health version 含 $SHORT_SHA ✓"

REMOTE_FILES=$(dev-host exec "$HOST_TARGET" --allow-service-exec -- \
  "find $REMOTE_SRC/aiops_diagnostics -type f -not -path '*__pycache__*' | wc -l" | tail -1 | tr -d ' ')
[ "$REMOTE_FILES" = "$EXPECTED_FILES" ] \
  || die "文件数不一致：远端 $REMOTE_FILES vs 预期 $EXPECTED_FILES（疑似 sync-check 残留污染）"
info "文件数 $REMOTE_FILES == 预期 ✓"

LOCAL_SHAS=$(cd "$WORK" && find aiops_diagnostics -type f -not -path '*__pycache__*' | sort | xargs sha256sum | awk '{print $1}' | sort)
REMOTE_SHAS=$(dev-host exec "$HOST_TARGET" --allow-service-exec -- \
  "cd $REMOTE_SRC && find aiops_diagnostics -type f -not -path '*__pycache__*' | sort | xargs sha256sum | awk '{print \$1}' | sort" | sort)
if [ "$LOCAL_SHAS" = "$REMOTE_SHAS" ]; then
  info "逐文件 sha 一致 ✓"
else
  die "逐文件 sha 不一致 —— 远端内容与产物不同"
fi

# 参考资料同样核对：它们不在 src/ 下，文件数与 sha 树都覆盖不到。
for ref in $REFERENCE_FILES; do
  want=$(sha256sum "$WORK/$ref" 2>/dev/null | cut -d' ' -f1 || true)
  [ -n "$want" ] || continue
  got=$(dev-host exec "$HOST_TARGET" --allow-service-exec -- \
    "sha256sum /opt/aiops-41/$ref 2>/dev/null | cut -d' ' -f1" | tail -1 | tr -d '[:space:]')
  [ "$want" = "$got" ] || die "参考资料 $ref 未同步到 41（本地 $want / 41 $got）"
  info "参考资料 $ref 一致 ✓"
done

step "完成"
cat <<EOF
   commit   $FULL_SHA ($SHORT_SHA)
   version  $STAMPED
   备份     $BACKUP
   产物     sha256 $ARTIFACT_SHA

   41 已跑本 commit 的 src/。**这不等于业务验收**：§5 的真实端到端仍需业务方
   签发的 thirdSession，按 runbook §5 由人执行。可报告状态：merged_waiting_deploy。
EOF
