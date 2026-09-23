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
CD_ALIAS=${CD_SSH_ALIAS:-aiops-41-cd}
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
info "commit=$FULL_SHA (${SHORT_SHA})"
info "subject=$(git log -1 --format=%s "$FULL_SHA" | cut -c1-70)"

WORK="$STAGE/src"

# 用 git archive 取该 commit 的 src/，而不是用工作树 —— 交付物必须是 commit 的
# 内容，不是某人 checkout 到哪。落地即：src/aiops_diagnostics/...
git archive "$FULL_SHA" src/aiops_diagnostics | tar -x -C "$STAGE"
[ -d "$WORK/aiops_diagnostics" ] || die "该 commit 没有 src/aiops_diagnostics"

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
tar -czf "$TARBALL" -C "$WORK" aiops_diagnostics
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

REMOTE_TARBALL="/tmp/aiops-sync-${SHORT_SHA}.tar.gz"

# 远端块的构造：把它写成一条已引用好的命令交给 dev-host exec（它按 ssh argv 语义
# 转发，需要 shell 语法时必须整体引用）。
#
# 关键：解包前 `rm -rf /tmp/sync-check`。rsync --delete 只删目标侧多出的文件，
# 不管源侧 —— 残留目录会被当成本次内容同步到生产。这是真实踩过的坑（#386）。
read -r -d '' REMOTE_CMD <<EOF || true
set -eu
TS=\$(date +%Y%m%d-%H%M%S)
B=/var/backups/aiops-41/backup-\$TS
mkdir -p "\$B"
cp -a $REMOTE_SRC "\$B"/
rm -rf /tmp/sync-check && mkdir -p /tmp/sync-check
tar xzf $REMOTE_TARBALL -C /tmp/sync-check
rsync -a --delete /tmp/sync-check/aiops_diagnostics/ $REMOTE_SRC/aiops_diagnostics/
chown -R aiops41:aiops41 $REMOTE_SRC
systemctl restart $SERVICE
sleep 5
echo "backup=\$B"
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
REMOTE_OUT=$(dev-host exec "$HOST_TARGET" --allow-service-exec --timeout 300 -- "$REMOTE_CMD")
printf '%s\n' "$REMOTE_OUT" | sed 's/^/   /'
BACKUP=$(printf '%s\n' "$REMOTE_OUT" | sed -n 's/^backup=//p' | head -1)
info "备份=$BACKUP"

# ---- 7. 部署后验证（失败即非零退出）----------------------------------------
# 只证技术健康。业务验收需业务方 thirdSession，§5/§6 明确禁止 CI 自证 ——
# 所以本节通过也**不**等于「已验收」，状态词止于 merged_waiting_deploy。
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

step "完成"
cat <<EOF
   commit   $FULL_SHA ($SHORT_SHA)
   version  $STAMPED
   备份     $BACKUP
   产物     sha256 $ARTIFACT_SHA

   41 已跑本 commit 的 src/。**这不等于业务验收**：§5 的真实端到端仍需业务方
   签发的 thirdSession，按 runbook §5 由人执行。可报告状态：merged_waiting_deploy。
EOF
