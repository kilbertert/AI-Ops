#!/usr/bin/env bash
# 41 的 editable 安装守卫（#609）。
#
# 跑法：  deploy/check-41-editable.sh                （只读，随时可跑）
#        deploy/check-41-editable.sh --self-check   （无网络，只测本脚本的判据）
#
# 背景：41 上 `aiops_diagnostics` 是 **editable 安装**，CD 同步 `src/` 能让新代码
# 生效完全依赖这一点。`uv sync` 有一步是重新安装本包；一旦它把 editable 换成实体
# 目录，之后同步 `src/` 就**静默失效** —— import 仍成功，拿的却是旧代码，而
# `/health` 还报着新 commit（这正是 2026-09-23 在生产上踩到的那次）。
#
# ---------------------------------------------------------------------------
# 判据为什么不是「site-packages 里有没有 aiops_diagnostics 目录」
# ---------------------------------------------------------------------------
# 那个目录**本来就应该在**：本包的 `pyproject.toml` 用 hatch 的 force-include
# 把 `.env.example` / SOP / `faq_catalog.json` 等静态资产装进了包目录。刚跑完
# `uv sync` 时它必然出现（实测 2026-10-09：`uv sync` 之后它是 12 个**静态资产
# 文件**，一个 `.py` 都没有）。
#
# 所以「目录在」既不是异常也不是正常 —— 它对这个判断没有信息量。真正区分两种
# 世界的是下面这条，而且它是 **Python 的导入规则**，不是本仓的约定：
#
#   若那个目录里存在 `__init__.py`，它就是**常规包**，而常规包**赢过** editable
#   的 `.pth` 路径（`.pth` 追加在 `site-packages` 之后，而常规包在更早的
#   `site-packages` 里被找到）。此时 `import aiops_diagnostics` 会解析到
#   site-packages 里的那份 —— src 同步失效。
#   若没有 `__init__.py`，它只是**命名空间包**的一个片段；`sys.path` 上更靠后的
#   常规包（`/opt/aiops-41/src`，由 `.pth` 追加）照样赢。
#
# 这与运行手册 `docs/agents/env-41-dependency-update.md` 第 3 步原本的措辞
# （"`-d` 为真即失败"）**不一致**：那一版按字面执行会在完全正常的 post-sync 状态
# 下误报。本脚本把判据改成上面那条导入规则，并保留手册里另外三条可执行的检查。
#
# 幂等、只读：不改 41 上任何东西，只读文件与跑解释器。
set -euo pipefail

HOST="${AIOPS_41_HOST:-aiops-41}"
SP="/opt/aiops-41/.venv/lib/python3.12/site-packages"
PY="/opt/aiops-41/.venv/bin/python"
SRC="/opt/aiops-41/src"

failures=0
note() { printf '  %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; failures=$((failures + 1)); }
ok() { printf 'ok: %s\n' "$*"; }

# --------------------------------------------------------------------------
# --self-check：不需要 41 的纯逻辑自检。改坏判据必转红。
# --------------------------------------------------------------------------
if [ "${1:-}" = "--self-check" ]; then
  command -v python3 >/dev/null || { echo "need python3" >&2; exit 1; }
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  # 两种世界各建一个：一个 site-packages 片段带 __init__.py（会被赢），
  # 一个不带（常规包照样赢）。断言与上面那条导入规则逐字对应。
  mkdir -p "$tmp/sp/aiops_diagnostics" "$tmp/src/aiops_diagnostics"
  printf 'WHO="site-packages"\n' >"$tmp/sp/aiops_diagnostics/__init__.py"
  printf 'WHO="src"\n' >"$tmp/src/aiops_diagnostics/__init__.py"

  # site-packages 在前，src 在后 —— 与 41 上 .pth 追加的顺序一致。
  win() { PYTHONPATH="$tmp/sp:$tmp/src" python3 -c 'import aiops_diagnostics as a; print(a.__file__)'; }

  got="$(win)"
  case "$got" in
  "$tmp/sp/"*) ok "self-check: __init__.py 在场 ⇒ site-packages 赢（这就是要被拦住的世界）" ;;
  *) fail "self-check: 期望 site-packages 赢，实际 $got" ;;
  esac

  rm "$tmp/sp/aiops_diagnostics/__init__.py"
  got="$(win)"
  case "$got" in
  "$tmp/src/"*) ok "self-check: 无 __init__.py ⇒ src 的常规包赢（正常状态，不得报警）" ;;
  *) fail "self-check: 期望 src 赢，实际 $got" ;;
  esac

  if [ "$failures" -eq 0 ]; then
    echo "self-check passed"
    exit 0
  fi
  echo "self-check FAILED ($failures)" >&2
  exit 1
fi

echo "== editable 守卫（只读）host=$HOST =="

# 一次 ssh 取回所有观察点。刻意**不用** `ssh host "… $VAR …"`：那种写法会在客户端
# 展开变量（shellcheck SC2029），远端路径就成了本地拼出来的字符串，读的是"我以为
# 它是什么"而不是"它实际是什么"。远端脚本用引号 heredoc 原样送过去，解释器里的值
# 只在**远端**求值。
probe="$(
  # shellcheck disable=SC2087  # 这里**就是**要在客户端展开 $SP/$PY：它们是远端路径
  # 常量，展开后才成为远端脚本的字面量；远端一侧用 \$ 转义，保证值只在远端求值。
  ssh "$HOST" bash -s <<REMOTE
set -u
SP="$SP"
PY="$PY"
printf 'PKG_FILE=%s\n' "\$("\$PY" -c 'import aiops_diagnostics as a; print(a.__file__)')"
printf 'API_FILE=%s\n' "\$("\$PY" -c 'import aiops_diagnostics.gateway_api as m; print(m.__file__)')"
printf 'HAS_INIT=%s\n' "\$([ -f "\$SP/aiops_diagnostics/__init__.py" ] && echo yes || echo no)"
printf 'HAS_PTH=%s\n' "\$([ -f "\$SP/_editable_impl_aiops_diagnostics.pth" ] && echo yes || echo no)"
printf 'PTH_TARGET=%s\n' "\$(cat "\$SP/_editable_impl_aiops_diagnostics.pth" 2>/dev/null | tr -d '\n')"
printf 'FAQ_DATA=%s\n' "\$("\$PY" -c 'from importlib.resources import files; import aiops_diagnostics as a; print(files(a).joinpath("faq_catalog.json"))')"
REMOTE
)"

value_of() { printf '%s\n' "$probe" | sed -n "s/^$1=//p" | tail -1; }

pkg_file="$(value_of PKG_FILE)"
api_file="$(value_of API_FILE)"
has_init="$(value_of HAS_INIT)"
has_pth="$(value_of HAS_PTH)"
pth_target="$(value_of PTH_TARGET)"
faq_data="$(value_of FAQ_DATA)"

# ---------------------------------------------------------------- 1. 导入解析
# 唯一真正的判据：解释器实际拿到哪份代码。放在第一位，因为它排在面象检查之前
# 就有意义 —— 后面几条解释的是"为什么会这样"。
note "import aiops_diagnostics → $pkg_file"
case "$pkg_file" in
"$SRC/aiops_diagnostics/__init__.py") ok "解析到 src（editable 生效）" ;;
*) fail "解析到 $pkg_file —— 不是 $SRC/aiops_diagnostics/__init__.py，src 同步不会生效" ;;
esac

note "import .gateway_api       → $api_file"
case "$api_file" in
"$SRC/aiops_diagnostics/"*) ok "子模块也来自 src" ;;
*) fail "子模块解析到 $api_file —— 包内解析已被劫持" ;;
esac

# ---------------------------------------------------------------- 2. 真包标志
# site-packages 里的那个目录**允许存在**（force-include 的静态资产）。它变成
# 常规包的唯一标志是 __init__.py —— 那会让它赢过 .pth（见头部注释）。
if [ "$has_init" = "yes" ]; then
  fail "$SP/aiops_diagnostics/__init__.py 存在 —— site-packages 里成了常规包，会赢过 editable .pth"
else
  ok "site-packages 里没有 __init__.py（不是常规包）"
fi

# ---------------------------------------------------------------- 3. .pth 仍在
if [ "$has_pth" = "yes" ]; then
  ok ".pth 在（$pth_target）"
else
  fail "$SP/_editable_impl_aiops_diagnostics.pth 不见了 —— editable 安装已被移除"
fi

# ---------------------------------------------------------------- 4. 包内数据
# faq.py 用 importlib.resources.files() 读 faq_catalog.json。resolved 到 src 时
# 它必须也落在 src（而不是被 site-packages 里那份同名的静态资产顶掉）。
note "files(pkg) faq_catalog.json → $faq_data"
case "$faq_data" in
"$SRC/aiops_diagnostics/"*) ok "包内数据来自 src" ;;
*) fail "包内数据解析到 $faq_data —— 与 src 不一致" ;;
esac

echo
if [ "$failures" -eq 0 ]; then
  echo "editable guard passed"
  exit 0
fi
cat >&2 <<'EOF'
editable guard FAILED —— 立刻回滚依赖环境，别继续部署：
  docs/agents/env-41-dependency-update.md 第 5 步（备份 → 恢复 venv → 重跑本守卫）。
这种状态下 CD 会「成功」但实际没换代码：import 仍成功，拿的是旧代码，
而 /health 还报新 commit。
EOF
exit 1
