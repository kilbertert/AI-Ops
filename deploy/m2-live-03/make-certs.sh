#!/usr/bin/env bash
# 平台侧的 TLS 材料：一张本地 CA + 一张给 `platform` 用的服务端证书。
#
# 为什么要它：`canonical_gateway_url` 只允许**回环**使用明文 HTTP，其余远端必须 HTTPS。
# 隔离 Gateway 与平台在同一个 bridge 网络里，平台默认只讲明文；此前用一个回环中继
# 把「地址是回环」凑出来，那是绕开校验。这里的做法是让平台**真的**讲 HTTPS：
# 网关用挂载进来的 CA 校验 `https://platform:8900`，校验没有被放宽。
#
# 产物（全部 0600/0700，`$M2LIVE_CONFIG/tls`）：
#   ca.key / ca.crt                 本地 CA（只在建栈时用）
#   platform.key / platform.crt     SAN=platform,localhost,127.0.0.1
#   platform.p12                    Spring Boot 用的 PKCS12（口令在同目录的 password 文件）
#   password                        keystore 口令
#
# 幂等：材料已存在且自检通过就不重新生成（重建证书会让已加载的网关信任失效）。
set -euo pipefail

CONFIG=${M2LIVE_CONFIG:-/etc/aiops-m2-live}
TLS="$CONFIG/tls"

# 复用条件不只是「主机名匹配」：证书必须仍然**有效**（未过期、尚未到期），
# 否则「看起来能启动、握手全失败」。`-checkend` 检查剩余有效期。
reuse_ok() {
  [ -f "$TLS/platform.crt" ] && [ -f "$TLS/platform.p12" ] && [ -f "$TLS/password" ] \
    && [ -f "$TLS/ca.crt" ] || return 1
  openssl x509 -in "$TLS/platform.crt" -noout -checkhost platform >/dev/null 2>&1 || return 1
  openssl x509 -in "$TLS/platform.crt" -noout -checkend 604800 >/dev/null 2>&1 || return 1
  # 证书必须由当前 CA 签发；CA 换过一次的话，旧证书已经不该被信任。
  openssl verify -CAfile "$TLS/ca.crt" "$TLS/platform.crt" >/dev/null 2>&1 || return 1
  openssl x509 -in "$TLS/ca.crt" -noout -checkend 604800 >/dev/null 2>&1 || return 1
  return 0
}

# 只核对「存在」会让过期的旧副本继续被容器使用（根材料换了、视图还是旧的）。
# 逐个比对内容：证书按 DER 比，keystore 与口令按字节比。
views_ok() {
  [ -f "$TLS/public/ca.crt" ] && [ -f "$TLS/public/platform.crt" ] \
    && [ -f "$TLS/platform/platform.p12" ] && [ -f "$TLS/platform/password" ] || return 1
  openssl x509 -in "$TLS/public/ca.crt" -noout -fingerprint -sha256 2>/dev/null \
    | grep -qx "$(openssl x509 -in "$TLS/ca.crt" -noout -fingerprint -sha256 2>/dev/null)" || return 1
  openssl x509 -in "$TLS/public/platform.crt" -noout -fingerprint -sha256 2>/dev/null \
    | grep -qx "$(openssl x509 -in "$TLS/platform.crt" -noout -fingerprint -sha256 2>/dev/null)" || return 1
  cmp -s "$TLS/platform/platform.p12" "$TLS/platform.p12" || return 1
  cmp -s "$TLS/platform/password" "$TLS/password" || return 1
  return 0
}

# 平台配置里的 keystore 口令必须与本次 p12 一致。重新签发后忘了同步，表现是
# 平台起不来（"keystore password was incorrect"），而文件看起来全对。
sync_platform_password() {
  local props="$CONFIG/platform/application.properties"
  [ -f "$props" ] || return 0
  # 口令已经一致且**没有重复项**时才跳过：Java properties 里同名属性后者覆盖前者，
  # 「某一行匹配」不等于「生效值匹配」。重复项必须交给同步脚本清掉。
  # 跳过写入的理由是权限：preflight 允许 0400，而 0400 的文件打不开写。
  local want matches
  want=$(cat "$TLS/password")
  matches=$(grep -c "^server\.ssl\.key-store-password=" "$props" 2>/dev/null || true)
  if [ "${matches:-0}" = "1" ] \
     && grep -qx "server\.ssl\.key-store-password=${want}" "$props" 2>/dev/null; then
    return 0
  fi
  python3 "$(dirname -- "${BASH_SOURCE[0]}")/sync-keystore-password.py" "$props" "$TLS/password"
}

write_views() {
  mkdir -p "$TLS/public" "$TLS/platform"
  cp "$TLS/ca.crt" "$TLS/public/ca.crt"
  cp "$TLS/platform.crt" "$TLS/public/platform.crt"
  cp "$TLS/platform.p12" "$TLS/platform/platform.p12"
  cp "$TLS/password" "$TLS/platform/password"
  chmod 644 "$TLS/public"/*.crt
  chmod 600 "$TLS/platform"/*
  chmod 700 "$TLS/public" "$TLS/platform"
}

if reuse_ok; then
  # 根材料有效：只补齐缺失的拆分视图，**不重新签发**（重新签发会让运行中的
  # 平台与网关互相不信任）。
  views_ok || write_views
  sync_platform_password
  echo "tls: 已有可用材料（$TLS）"
  exit 0
fi

command -v openssl >/dev/null || { echo "tls: 需要 openssl" >&2; exit 2; }
mkdir -p "$TLS" && chmod 700 "$TLS"

password=$(openssl rand -hex 24)
# CA 必须带 keyUsage（keyCertSign, cRLSign）。缺它会得到一个能签发、但 OpenSSL 3
# 在**校验链**时拒绝的 CA（`CA cert does not include key usage extension`）——
# 证书看起来全都对，握手却永远失败。
cat > "$TLS/ca.cnf" <<'EOF'
[req]
distinguished_name=dn
x509_extensions=v3
prompt=no
[dn]
CN=aiops-m2-live-03 local CA
[v3]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
EOF
openssl req -x509 -newkey rsa:3072 -sha256 -days 825 -nodes \
  -keyout "$TLS/ca.key" -out "$TLS/ca.crt" -config "$TLS/ca.cnf" >/dev/null 2>&1
rm -f "$TLS/ca.cnf"

openssl req -newkey rsa:2048 -sha256 -nodes \
  -keyout "$TLS/platform.key" -out "$TLS/platform.csr" \
  -subj "/CN=platform" >/dev/null 2>&1

cat > "$TLS/openssl.cnf" <<'EOF'
[ext]
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:platform,DNS:localhost,IP:127.0.0.1
EOF

openssl x509 -req -in "$TLS/platform.csr" -CA "$TLS/ca.crt" -CAkey "$TLS/ca.key" \
  -CAcreateserial -days 825 -sha256 -extfile "$TLS/openssl.cnf" -extensions ext \
  -out "$TLS/platform.crt" >/dev/null 2>&1

openssl pkcs12 -export -name platform \
  -inkey "$TLS/platform.key" -in "$TLS/platform.crt" -certfile "$TLS/ca.crt" \
  -out "$TLS/platform.p12" -passout "pass:$password" >/dev/null 2>&1

printf '%s\n' "$password" > "$TLS/password"
rm -f "$TLS/platform.csr" "$TLS/openssl.cnf" "$TLS/ca.srl"
# 只对**文件**设权限：`chmod 600 "$TLS"/*` 会把已有的 public/、platform/ 目录
# 一起改成 0600（去掉执行位），随后写视图就再也进不去。目录单独给 0700。
chmod 700 "$TLS/public" "$TLS/platform" 2>/dev/null || true
chmod 600 "$TLS"/*.key "$TLS"/*.p12 "$TLS/password" 2>/dev/null || true
chmod 644 "$TLS/ca.crt" "$TLS/platform.crt"   # 公钥材料：容器内只需读

# 目录按**谁能看到什么**分开，而不是把整个 tls/ 挂给需要证书的每个容器：
#   public/   只有公开证书 —— 隔离 Gateway 挂它（它只需要验证平台，不需要签名能力）
#   platform/ 平台自己的私钥材料 —— 只有 platform 容器挂它
# 私钥留在 $TLS 根（宿主的 0600），任何容器都看不到。
write_views
# 重新签发必然换口令：这一步不能只在复用路径上做。
sync_platform_password

openssl x509 -in "$TLS/platform.crt" -noout -checkhost platform >/dev/null
echo "tls: 已生成本地 CA 与 platform 证书（$TLS；public/ 只含公开证书）"
