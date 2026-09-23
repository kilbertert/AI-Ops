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

处理方式（需独立决定，不在本脚本范围）：
  1. 在 41 上更新依赖环境（该机无 uv，需先确定用哪种方式），并记录变更；或
  2. 若本次改动确实不需要新依赖，把 $spec 在 41 上对齐到目标 commit 的版本。
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
mkdir -p "\$B"
cp -a $REMOTE_SRC "\$B"/
rm -rf /tmp/sync-check && mkdir -p /tmp/sync-check
tar xzf $REMOTE_TARBALL -C /tmp/sync-check
rsync -a --delete /tmp/sync-check/aiops_diagnostics/ $REMOTE_SRC/aiops_diagnostics/
# 参考资料：与源码分开搬，因为它们的落点是 /opt/aiops-41 而不是 src/。
# reference_root() 解析到 /opt/aiops-41（该目录有 pyproject.toml + src/），
# 诊断每次运行都从这里读这几份文件 —— 不同步就会「报新 commit、用旧 SOP」。
for ref in $REFERENCE_FILES; do
  if [ -f "/tmp/sync-check/$ref" ]; then
    mkdir -p "/opt/aiops-41/$(dirname "$ref")"
    cp "/tmp/sync-check/$ref" "/opt/aiops-41/$ref"
    echo "reference-synced=$ref"
  fi
done
chown -R aiops41:aiops41 $REMOTE_SRC
# 备份保留：每次部署留一份全量 src 备份（约 8.5M）。CD 会把份数持续推上去，
# 所以按份数修剪、只保留最近 KEEP_BACKUPS 份。
#
# **只删本脚本自己造的备份**：本脚本的备份名是 backup-<14位时间戳>。人工或别的
# 工具留下的备份（例如 backup-20260915-pre-621490d 那种带标记的）不在修剪范围内
# —— 精简磁盘不是删除别人产物的理由，何况备份正是回滚时要用的东西。实测中曾把
# 一个人工备份误删，故加这个约束。
# 若匹配到的份数仍超限，说明有非本脚本的备份占位，报告出来而不是继续删。
LATEST_PATTERN='backup-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'
own=\$(ls -1d /var/backups/aiops-41/\$LATEST_PATTERN 2>/dev/null | wc -l)
others=\$(( \$(ls -1d /var/backups/aiops-41/backup-* 2>/dev/null | wc -l) - own ))
if [ "\$own" -gt "${KEEP_BACKUPS}" ]; then
  ls -1d /var/backups/aiops-41/\$LATEST_PATTERN 2>/dev/null | sort \
    | head -n "\$((own - ${KEEP_BACKUPS}))" \
    | while read -r old; do rm -rf "\$old"; echo "pruned-backup=\${old##*/}"; done
fi
if [ "\$others" -gt 0 ]; then
  echo "note: \$others non-CD backup(s) present, not pruned"
fi
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
