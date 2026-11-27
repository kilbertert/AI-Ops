#!/usr/bin/env bash
# 在 41 控制面写入受控 OCI Registry 配置（0600 私有 JSON，密码经 stdin 传入，不进命令行历史）。
set -euo pipefail
umask 027

usage() {
  cat >&2 <<'EOF'
用法:
  configure-registry.sh --repository <REGISTRY/NAMESPACE/IMAGE> \
    [--registry <HOST>] [--pull-username <USER>] [--no-pull-auth] [--central <TARGET>]

说明:
  - 若提供 --pull-username（不加 --no-pull-auth），会从 stdin（或交互提示）读取拉取密码。
  - 若客户机已预登录同一 ACR（如公司 ACR），传 --no-pull-auth 即可，不覆写客户机认证。
EOF
  exit 2
}

REPOSITORY=""
REGISTRY=""
PULL_USER=""
NO_PULL_AUTH=0
CENTRAL="41"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repository)
      [ "$#" -ge 2 ] || usage
      REPOSITORY="$2"
      shift 2
      ;;
    --registry)
      [ "$#" -ge 2 ] || usage
      REGISTRY="$2"
      shift 2
      ;;
    --pull-username)
      [ "$#" -ge 2 ] || usage
      PULL_USER="$2"
      shift 2
      ;;
    --no-pull-auth)
      NO_PULL_AUTH=1
      shift
      ;;
    --central)
      [ "$#" -ge 2 ] || usage
      CENTRAL="$2"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "未知参数: $1" >&2
      usage
      ;;
  esac
done

[ -n "$REPOSITORY" ] || usage
if [ -z "$REGISTRY" ]; then
  REGISTRY="${REPOSITORY%%/*}"
fi
[[ "$REGISTRY" =~ ^[A-Za-z0-9._-]+(:[0-9]{1,5})?$ ]] || {
  echo "非法 Registry 域名: $REGISTRY" >&2
  exit 2
}
[[ "$REPOSITORY" =~ ^[a-z0-9._:/-]{3,200}$ ]] || {
  echo "非法镜像仓库路径: $REPOSITORY" >&2
  exit 2
}

PULL_PASSWORD=""
if [ "$NO_PULL_AUTH" = "0" ] && [ -n "$PULL_USER" ]; then
  if [ -t 0 ]; then
    printf '请输入 ACR 只读拉取密码（%s @ %s）: ' "$PULL_USER" "$REGISTRY" >&2
    IFS= read -r -s PULL_PASSWORD
    printf '\n' >&2
  else
    IFS= read -r PULL_PASSWORD
  fi
  [ -n "$PULL_PASSWORD" ] || { echo "拉取密码为空，已取消。" >&2; exit 2; }
fi

payload=$(python3 - "$REGISTRY" "$REPOSITORY" "$PULL_USER" "$PULL_PASSWORD" <<'PY'
import json
import sys
from datetime import datetime, timezone

reg, repo, user, pw = sys.argv[1:5]
doc = {
    "schema": "aiops-container-registry/1",
    "registry": reg,
    "repository": repo,
    "pull_username": user,
    "pull_password": pw,
    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
print(json.dumps(doc, ensure_ascii=False, indent=2))
PY
)

remote_py=$(cat <<'PY'
import json, os, pwd, sys
from pathlib import Path

payload = sys.stdin.read()
doc = json.loads(payload)
assert doc.get("schema") == "aiops-container-registry/1"
target = Path("/var/lib/aiops-41/onboarding-assets/registry-config.json")
target.parent.mkdir(parents=True, exist_ok=True)
pw = pwd.getpwnam("aiops-41")
os.chown(target.parent, pw.pw_uid, pw.pw_gid)
os.chmod(target.parent, 0o700)
tmp = target.with_suffix(".tmp")
tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chown(tmp, pw.pw_uid, pw.pw_gid)
os.chmod(tmp, 0o600)
os.replace(tmp, target)
print(json.dumps({
    "ok": True,
    "registry": doc["registry"],
    "repository": doc["repository"],
    "has_pull_auth": bool(doc.get("pull_username") and doc.get("pull_password")),
}, ensure_ascii=False))
PY
)

printf '%s' "$payload" | dev-host exec "$CENTRAL" --allow-service-exec --timeout 60 -- "sudo -n python3 -c $(printf '%q' "$remote_py")"
