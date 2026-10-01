#!/usr/bin/env bash
# 公司 GitLab 只读探查（#414 / #475 的唯一取数入口）。
#
# 用法：
#   deploy/company-gitlab-api.sh pin
#   deploy/company-gitlab-api.sh projects  --search <词>
#   deploy/company-gitlab-api.sh group     --id <组id>
#   deploy/company-gitlab-api.sh branches  --project <id>
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
# 凭据只从文件里读，不进命令行、不进输出、不进本仓任何文件。
# ⚠️ 因此**不要在本脚本外面套 `bash -x` 跑**：xtrace 会把 `-H 'PRIVATE-TOKEN: …'`
# 整条打印出来。要调试就调试别的层，别把令牌trace 进日志。
set -euo pipefail

URL=${AIOPS_GL_URL:-https://git.qushiyun.com:801}
CA=${AIOPS_GL_CA:-.scratch/company-repos/gitlab-qushiyun.pem}
#: 凭据位置**不可配置**，这是有意的：一个能被环境变量改掉路径的凭据读取器，
#: 就是一个能被改成读任意文件的原语。它只从这一处读，见 §3「凭据只写位置」。
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
[ -f "$CREDS" ] || die "缺少凭据文件 $CREDS。"

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

#: 凭据按「哪个主机、哪个用户字段、哪个 HTTP 头」取用；值不进命令行、不落盘、不打印。
#: 约定：`.git-credentials` 里该主机的用户字段就是 GitLab 的 personal access token。
token() {
  local host pair
  host=$(printf '%s' "$URL" | sed -E 's#^https://##; s#:[0-9]+$##')
  # 端口在 .git-credentials 里可能写成 %3a，两种键的形状都接受。
  # 一次匹配里同时取「用户字段」与「口令」两个捕获组：口令**必须**是第 2 组，
  # 否则交替分支 `(%3a|:)?` 会变成被打印的那一组（那会把 token 打印成 `%3a`）。
  pair=$(sed -nE "s#^https://([^:@]+):([^@]+)@${host}(%3a|:)?[0-9]*\$#\1 \2#p" "$CREDS" | head -1)
  [ -n "$pair" ] || die "$CREDS 里没有 $host 的条目。"
  printf '%s' "${pair#* }"
}

#: 带下发的 next-page 走一页。第 2 个参数是暂存响应头的文件。
fetch() {
  curl -sSk -m 90 -D "$2" --pinnedpubkey "$(pin)" \
    -H "PRIVATE-TOKEN: $(token)" "$URL/api/v4$1"
}
next_page() {
  tr -d '\r' < "$1" | sed -nE 's/^[Xx]-[Nn]ext-[Pp]age:[[:space:]]*([0-9]+).*/\1/p'
}

need() { [ -n "${2:-}" ] || die "$1 是必填。"; }

cmd=${1:-help}; shift || true
project=""; ref=""; search=""; id=""; path=""; count_java=0
while [ $# -gt 0 ]; do
  case "$1" in
    --project) project=$2; shift 2;;
    --ref)     ref=$2;     shift 2;;
    --search)  search=$2;  shift 2;;
    --id)      id=$2;      shift 2;;
    --path)    path=$2;    shift 2;;
    --count-java) count_java=1; shift;;
    *) die "未知参数 $1";;
  esac
done

HDR=$(mktemp); trap 'rm -f "$HDR"' EXIT

case "$cmd" in
  pin)
    pin; printf '\n'
    ;;
  projects)
    need --search "$search"
    fetch "/projects?search=$(printf '%s' "$search" | jq -sRr @uri)&per_page=100&simple=true&order_by=last_activity_at" "$HDR" \
      | jq -r '.[] | "\(.id)\t\(.path_with_namespace)\tdefault=\(.default_branch)\tactivity=\(.last_activity_at)"'
    ;;
  group)
    need --id "$id"
    fetch "/groups/$id/projects?include_subgroups=true&per_page=100&simple=true" "$HDR" \
      | jq -r '.[] | "\(.id)\t\(.path_with_namespace)\tdefault=\(.default_branch)\tactivity=\(.last_activity_at)"'
    ;;
  branches)
    need --project "$project"
    fetch "/projects/$project/repository/branches?per_page=100" "$HDR" \
      | jq -r '.[] | "\(.name)\t\(.commit.committed_date)\t\(.commit.short_id)"'
    ;;
  tree)
    need --project "$project"; need --ref "$ref"
    page=1; blobs=0; java=0
    while [ -n "$page" ]; do
      body=$(fetch "/projects/$project/repository/tree?ref=$ref&recursive=true&per_page=100&page=$page" "$HDR")
      counts=$(printf '%s' "$body" | jq -r '[.[]|select(.type=="blob")]|"\(length) \(map(select(.path|endswith(".java")))|length)"')
      blobs=$((blobs + ${counts% *})); java=$((java + ${counts#* }))
      if [ "$count_java" = 0 ]; then
        printf '%s' "$body" | jq -r '.[] | "\(.type)\t\(.path)"'
      fi
      page=$(next_page "$HDR")
    done
    # `[ … ] && printf` 在条件为假时整条语句的退出码是 1，而它是 case 分支的最后一句，
    # 会变成脚本的退出码 —— 一个成功的列目录会被读成失败。
    if [ "$count_java" = 1 ]; then printf 'ref=%s blobs=%s java=%s\n' "$ref" "$blobs" "$java"; fi
    ;;
  blobs)
    need --project "$project"; need --ref "$ref"; need --search "$search"
    fetch "/projects/$project/search?scope=blobs&ref=$ref&search=$(printf '%s' "$search" | jq -sRr @uri)&per_page=100" "$HDR" \
      | jq -r '.[] | "\(.path):\(.startline)"'
    ;;
  raw)
    need --project "$project"; need --ref "$ref"; need --path "$path"
    fetch "/projects/$project/repository/files/$(printf '%s' "$path" | jq -sRr @uri)/raw?ref=$ref" "$HDR"
    ;;
  *)
    sed -n '2,18p' "$0"
    ;;
esac
