#!/usr/bin/env bash
# 公司 GitLab 只读探查（#414 / #475 的唯一取数入口）。
#
# 用法：
#   deploy/company-gitlab-api.sh pin
#   deploy/company-gitlab-api.sh projects  --search <词>
#   deploy/company-gitlab-api.sh group     --id <组id>
#   deploy/company-gitlab-api.sh branches  --project <id> [--limit N]
#   deploy/company-gitlab-api.sh tree      --project <id> --ref <分支> [--count-java]
#   deploy/company-gitlab-api.sh blobs     --project <id> --ref <分支> --search <词>
#   deploy/company-gitlab-api.sh raw       --project <id> --ref <分支> --path <路径>
#
# 它只做只读的 GET。写方法（POST/PUT/DELETE）不在本脚本的能力范围内 ——
# 公司源码是外部治理资产，本仓对它**只读**。
#
# 🛑 `--ref` 是**强制**的，不是可选优化：默认分支可能是脚手架。
#    不传 `--ref` 会得到「这个仓库是空的 / 没有这段代码」这类**错误结论**。
#    实测反例见 docs/agents/company-code-recon-procedure.md §2：
#    `iot/cloud-charging-pile` 的 master 34 个 java、release 2012 个。
#
# 凭据不进 argv（经 curl 的 --config 从 stdin 读），不进输出，不进本仓任何文件。
# ⚠️ 仍然**不要用 `bash -x` 跑本脚本**：xtrace 会把带入参的那一行整个展开打印，
#    包括 curl 命令与参数。要调试就调试别的层，别把凭据 trace 进日志。
set -euo pipefail

URL=${AIOPS_GL_URL:-https://git.qushiyun.com:801}
CA=${AIOPS_GL_CA:-.scratch/company-repos/gitlab-qushiyun.pem}
#: 凭据位置**不可配置**，这是有意的：一个能被环境变量改掉路径的凭据读取器，
#: 就是一个能被改成读任意文件的原语。它只从这一处读，见规程 §3「凭据只写位置」。
CREDS=$HOME/.git-credentials

die() { printf '%s\n' "$*" >&2; exit 2; }

require_shape() {
  local name=$1 value=$2 pattern=$3
  # 换行先单独拒掉：`grep -E` 逐行匹配，一个「合法首行 + 换行 + 命令」的值会过正则那一关。
  case "$value" in
    *$'\n'* | *$'\r'*) die "环境变量 $name 含换行，拒绝。" ;;
  esac
  printf '%s' "$value" | grep -Eq "$pattern" || die "环境变量 $name 的形状不被接受：$value"
}
require_shape AIOPS_GL_URL "$URL" '^https://[A-Za-z0-9][A-Za-z0-9._-]*(:[0-9]{1,5})?$'
require_shape AIOPS_GL_CA "$CA" '^[A-Za-z0-9._/-]{1,200}$'
#: 凭据文件的**存在性不在这里检查**：`pin` 与 `help` 根本不需要它，在入口处拒绝
#: 会把「没配凭据」报成「你这条命令写错了」，而这两件事要分开（CI 上就是这么暴露的）。
#: 检查放在 `token()` 里 —— 只有真正要发请求的那几条路径才碰它。

#: 身份锚点。公司实例是自签证书，系统 CA 不认它 —— 这不是「临时 -k 跳过」的场景，
#: 而是一个**必须钉住**的东西：光用 `-k` 等于完全不做证书校验，任何人都能顶上。
#: 于是从留存的证书里算出 SPKI 指纹，用 `--pinnedpubkey` 钉住；证书一换就失败，
#: 由人核对指纹后更新锚点，而不是自动信任。
pin() {
  [ -f "$CA" ] || die "缺少身份锚点 $CA。
先取一次证书（指纹要由人核对，不要自动接受）：
  openssl s_client -connect <主机>:<端口> -servername <主机> </dev/null 2>/dev/null \\
    | openssl x509 -outform PEM > $CA
  openssl x509 -in $CA -noout -fingerprint -sha256"
  local spki
  # 失败要失败在**这一句**上：openssl 解不开文件时若放任它往下走，
  # 后面就会拿着一个空指纹去请求，而空指纹在 curl 里意味着「不钉」—— 那正是本节要防的事。
  spki=$(openssl x509 -in "$CA" -pubkey -noout 2>/dev/null \
    | openssl pkey -pubin -outform der 2>/dev/null \
    | openssl dgst -sha256 -binary | base64) \
    || die "$CA 不是一份可解析的证书。"
  [ -n "$spki" ] || die "$CA 不是一份可解析的证书。"
  printf 'sha256//%s' "$spki"
}

#: 凭据只按「哪个文件、哪个主机键、哪个字段」取；值不进 argv、不落盘、不打印。
#: 约定：`.git-credentials` 里该主机的**口令字段**就是 GitLab 的 personal access token；
#: 用户字段是 `oauth2`，它只是「这一行是令牌」的标记，**不是**令牌本身。
token() {
  local host pair
  [ -f "$CREDS" ] || die "缺少凭据文件 $CREDS。"
  host=$(printf '%s' "$URL" | sed -E 's#^https://##; s#:[0-9]+$##')
  # 端口在 .git-credentials 里可能写成 %3a，两种键的形状都接受。
  # 一次匹配里同时取「用户字段」与「口令」两个捕获组：口令**必须**是第 2 组，
  # 否则交替分支 `(%3a|:)?` 会变成被打印的那一组（那会把 token 打印成 `%3a`）。
  pair=$(sed -nE "s#^https://([^:@]+):([^@]+)@${host}(%3a|:)?[0-9]*\$#\1 \2#p" "$CREDS" | head -1)
  [ -n "$pair" ] || die "$CREDS 里没有 $host 的条目。"
  printf '%s' "${pair#* }"
}

TMPD=$(mktemp -d)
HDR=$TMPD/hdr
BODY=$TMPD/body
trap 'rm -rf "$TMPD"' EXIT

#: 一次只读 GET。**令牌经 curl 的 `--config /dev/stdin` 传入，不出现在 argv 里**
#: （实测：`/proc/<pid>/cmdline` 里看不到它；放进 `-H` 则任何本机进程都能读到）。
#: `--proto '=https'` 让「万一将来加了 -L」也只能停在 https 上，不会把自定义头带去 http。
#: HTTP 状态**显式检查**：`curl` 默认不会因 4xx/5xx 失败，不检查就会把错误正文当源码交付。
fetch() {
  local path=$1 rc=0 status secret
  # 凭据**先单独取**，取不到就在这里停下。
  # 不能写成 `printf … "$(token)" | curl …`：那样子进程里 `token` 失败只让子进程退出，
  # 外层 `printf` 照样成功、管道照样跑 —— 于是会**拿着空令牌发出一个匿名请求**。
  # 公开项目会匿名返回 200，看起来像「认证成功」，比直接失败更糟。
  secret=$(token) || die "读不到凭据（原因见上），已停止，未发出请求。"
  # `|| rc=$?` 而不是「先跑再读 $?」：`set -e` 会在 curl 失败时**当场退出函数**，
  # 后面那句 `rc=$?` 根本不会执行，stderr 又已被重定向进文件 ——
  # 于是连接失败与公钥不匹配都只剩一个没有原因的非零退出码。`||` 让这条命令变成
  # 「被测试的」，set -e 不再拦它。
  printf 'header = "PRIVATE-TOKEN: %s"\n' "$secret" \
    | curl -sSk -m 90 -D "$HDR" -o "$BODY" -w '%{http_code}' \
        --proto '=https' --pinnedpubkey "$(pin)" \
        --config /dev/stdin "$URL/api/v4$path" >"$TMPD/status" 2>"$TMPD/err" || rc=$?
  if [ "$rc" != 0 ]; then
    die "请求失败（curl 退出码 $rc）：$path
$(cat "$TMPD/err")"
  fi
  status=$(cat "$TMPD/status")
  case "$status" in
    2??) : ;;
    *) die "HTTP $status：$path
$(head -c 300 "$BODY")" ;;
  esac
  cat "$BODY"
}

next_page() {
  tr -d '\r' < "$HDR" | sed -nE 's/^[Xx]-[Nn]ext-[Pp]age:[[:space:]]*([0-9]+).*/\1/p'
}

#: 分页取全。GitLab 默认每页 20 条、最多 100 —— 不分页就会**静默漏项**，
#: 而漏掉的项在结果里看不出任何痕迹（仓库地图与代码搜索尤其致命）。
#: 第 1 个参数必须已经带 `?`，本函数往后接 `&per_page=…&page=…`。
fetch_all() {
  local path=$1 filter=$2 page=1
  while [ -n "$page" ]; do
    fetch "$path&per_page=100&page=$page" | jq -r "$filter"
    page=$(next_page)
  done
}

#: 路径段与查询值都要编码。不编码时，含 `+` / `#` / `&` 的分支名或搜索词会被
#: 当成 URL 语法解析，请求打到别的分支上 —— 而结果看起来仍然像一个正常答案。
#: 必须用 `printf` 而不是 `<<<` 喂给 jq：here-string 会**带上一个换行**，
#: 于是每个值都被编码成 `…%0A`，GitLab 那边会把它当成值的一部分或直接报 400。
enc() { printf '%s' "$1" | jq -sRr @uri; }

need() { [ -n "${2:-}" ] || die "$1 是必填。"; }

cmd=${1:-help}; shift || true
project=""; ref=""; search=""; id=""; path=""; count_java=0; limit=""
while [ $# -gt 0 ]; do
  case "$1" in
    --project) project=$2; shift 2;;
    --ref)     ref=$2;     shift 2;;
    --search)  search=$2;  shift 2;;
    --id)      id=$2;      shift 2;;
    --path)    path=$2;    shift 2;;
    --count-java) count_java=1; shift;;
    --limit)   limit=$2;   shift 2;;
    *) die "未知参数 $1";;
  esac
done

#: `--limit` 是数字。这条检查必须在 `case` **之前**：放进分支里用
#: `[ "$limit" -gt 0 ]` 判空/非数字时，那个测试本身会失败并被 `set -e` 吞掉，
#: 结果是**静默地不按 limit 走**（实测就是那样，一个带分号的注入值也能过）。
if [ -n "$limit" ]; then
  case "$limit" in
    ''|*[!0-9]*) die "--limit 只接受数字：$limit" ;;
  esac
  [ "${#limit}" -le 3 ] || die "--limit 最多 3 位：$limit"
fi

case "$cmd" in
  pin)
    pin; printf '\n'
    ;;
  projects)
    # `--search` 不带 ⇒ 列**全部可见仓**（L1 仓库地图要的就是全集）。
    # 带 ⇒ 按**仓库名**过滤，注意它是名字匹配、不是路径匹配：同一个名字可能在
    # 多个命名空间下各有一份（实测 `cloud-charging-pile` 有 3 个），
    # 所以「搜到了唯一一个」这个前提不成立，别据此下结论。
    if [ -n "$search" ]; then
      fetch_all "/projects?search=$(enc "$search")&simple=true&order_by=last_activity_at&sort=desc" \
        '.[] | "\(.id)\t\(.path_with_namespace)\tdefault=\(.default_branch)\tactivity=\(.last_activity_at)"'
    else
      fetch_all "/projects?simple=true&order_by=last_activity_at&sort=desc" \
        '.[] | "\(.id)\t\(.path_with_namespace)\tdefault=\(.default_branch)\tactivity=\(.last_activity_at)"'
    fi
    ;;
  group)
    need --id "$id"
    fetch_all "/groups/$id/projects?include_subgroups=true&simple=true" \
      '.[] | "\(.id)\t\(.path_with_namespace)\tdefault=\(.default_branch)\tactivity=\(.last_activity_at)"'
    ;;
  branches)
    need --project "$project"
    # `--limit N` 只报**最近提交的 N 个分支**，按提交时间倒序。
    # 为什么需要它：仓库有几十个分支时（商城 60+），一份按名字排序的完整列表
    # **读不出「真分支是哪一个」**——而判据正是「谁的提交最新」。
    # `fetch_all` 已经取全，这里只是排序后截取，不再多发请求。
    if [ -n "$limit" ]; then
      # 用 awk 而不是 `| head -n N`：分支多时 head 读完就关掉读端，上游 sort 收到
      # SIGPIPE(141)，而 `set -o pipefail` 会把整条管道判成失败 —— 结果已经印出来了，
      # 调用方却看到非零退出码。awk 会读完全部输入，只在打印上截断。
      fetch_all "/projects/$project/repository/branches?" \
        '.[] | "\(.commit.committed_date)\t\(.name)"' \
        | sort -r | awk -v n="$limit" 'NR <= n'
      exit 0
    fi
    fetch_all "/projects/$project/repository/branches?" \
      '.[] | "\(.name)\t\(.commit.committed_date)\t\(.commit.short_id)"'
    ;;
  tree)
    need --project "$project"; need --ref "$ref"
    page=1; blobs=0; java=0
    while [ -n "$page" ]; do
      body=$(fetch "/projects/$project/repository/tree?ref=$(enc "$ref")&recursive=true&per_page=100&page=$page")
      counts=$(printf '%s' "$body" | jq -r '[.[]|select(.type=="blob")]|"\(length) \(map(select(.path|endswith(".java")))|length)"')
      blobs=$((blobs + ${counts% *})); java=$((java + ${counts#* }))
      if [ "$count_java" = 0 ]; then
        printf '%s' "$body" | jq -r '.[] | "\(.type)\t\(.path)"'
      fi
      page=$(next_page)
    done
    # `[ … ] && printf` 在条件为假时整条语句退出码是 1，而它是分支的最后一句，
    # 会变成脚本的退出码 —— 一个成功的列目录会被读成失败。
    if [ "$count_java" = 1 ]; then printf 'ref=%s blobs=%s java=%s\n' "$ref" "$blobs" "$java"; fi
    ;;
  blobs)
    need --project "$project"; need --ref "$ref"; need --search "$search"
    fetch_all "/projects/$project/search?scope=blobs&ref=$(enc "$ref")&search=$(enc "$search")" \
      '.[] | "\(.path):\(.startline)"'
    ;;
  raw)
    need --project "$project"; need --ref "$ref"; need --path "$path"
    fetch "/projects/$project/repository/files/$(enc "$path")/raw?ref=$(enc "$ref")"
    ;;
  *)
    sed -n '2,18p' "$0"
    ;;
esac
