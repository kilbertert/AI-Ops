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
# JVM truststore 的口令不是秘密（存储里只有公开 CA），但也不用空口令：
# 空口令在部分 JDK 上会被当成「未提供完整性检查」。
TRUSTSTORE_PASSWORD=m2live-trust

# 复用条件不只是「主机名匹配」：证书必须仍然**有效**（未过期、尚未到期），
# 否则「看起来能启动、握手全失败」。`-checkend` 检查剩余有效期。
reuse_ok() {
  [ -f "$TLS/platform.crt" ] && [ -f "$TLS/platform.p12" ] && [ -f "$TLS/password" ] \
    && [ -f "$TLS/ca.crt" ] && [ -f "$TLS/ca.key" ] || return 1
  openssl x509 -in "$TLS/platform.crt" -noout -checkhost platform >/dev/null
openssl x509 -in "$TLS/model-proxy.crt" -noout -checkhost model-proxy >/dev/null 2>&1 || return 1
  openssl x509 -in "$TLS/platform.crt" -noout -checkend 604800 >/dev/null 2>&1 || return 1
  # 证书必须由当前 CA 签发；CA 换过一次的话，旧证书已经不该被信任。
  openssl verify -CAfile "$TLS/ca.crt" "$TLS/platform.crt" >/dev/null 2>&1 || return 1
  openssl x509 -in "$TLS/ca.crt" -noout -checkend 604800 >/dev/null 2>&1 || return 1
  return 0
}

# 同一把 CA 下的叶子证书：已经有效就不动（重签会让运行中的对端不再信任），
# 缺失或即将到期才补签。补签只影响这一张叶子。
ensure_leaf() {  # ensure_leaf <name> <san>
  local name=$1 san=$2
  if [ -f "$TLS/$name.crt" ] && [ -f "$TLS/$name.key" ] \
     && openssl x509 -in "$TLS/$name.crt" -noout -checkhost "$name" >/dev/null 2>&1 \
     && openssl x509 -in "$TLS/$name.crt" -noout -checkend 604800 >/dev/null 2>&1 \
     && openssl verify -CAfile "$TLS/ca.crt" "$TLS/$name.crt" >/dev/null 2>&1; then
    return 0
  fi
  echo "tls: 补签 $name 证书"
  openssl req -newkey rsa:2048 -sha256 -nodes \
    -keyout "$TLS/$name.key" -out "$TLS/$name.csr" -subj "/CN=$name" >/dev/null 2>&1
  cat > "$TLS/$name.ext" <<EOF
[ext]
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$san
EOF
  openssl x509 -req -in "$TLS/$name.csr" -CA "$TLS/ca.crt" -CAkey "$TLS/ca.key" \
    -CAcreateserial -days 825 -sha256 -extfile "$TLS/$name.ext" -extensions ext \
    -out "$TLS/$name.crt" >/dev/null 2>&1
  rm -f "$TLS/$name.csr" "$TLS/$name.ext"
  chmod 644 "$TLS/$name.crt"; chmod 600 "$TLS/$name.key"
}

# 只核对「存在」会让过期的旧副本继续被容器使用（根材料换了、视图还是旧的）。
# 逐个比对内容：证书按 DER 比，keystore 与口令按字节比。
# 节点是 Java：它不认 SSL_CERT_FILE，要的是 truststore。宿主不一定装了 JDK，
# 所以优先用宿主 keytool，没有就用栈自己的 JVM 镜像跑一次 —— 两者产出的都是同一份
# 只含公开 CA 的 PKCS12。
write_truststore() {
  local store="$TLS/public/truststore.p12"
  rm -f "$store"
  if command -v keytool >/dev/null 2>&1; then
    keytool -importcert -noprompt -alias m2live-ca -file "$TLS/ca.crt" \
      -keystore "$store" -storetype PKCS12 -storepass "$TRUSTSTORE_PASSWORD" >/dev/null 2>&1
  else
    # 用当前 UID 跑容器：否则 keytool 产出的 truststore 属主是 root，宿主改不动它。
    docker run --rm --user "$(id -u):$(id -g)" -v "$TLS:/tls" --entrypoint keytool \
      "${M2LIVE_JAVA_IMAGE:-eclipse-temurin:21-jdk}" \
      -importcert -noprompt -alias m2live-ca -file /tls/ca.crt \
      -keystore /tls/public/truststore.p12 -storetype PKCS12 \
      -storepass "$TRUSTSTORE_PASSWORD" >/dev/null 2>&1
  fi
  [ -f "$store" ] || { echo "tls: 生成 truststore 失败" >&2; exit 2; }
  chmod 644 "$store"
}

views_ok() {
  [ -f "$TLS/public/ca.crt" ] && [ -f "$TLS/public/platform.crt" ] \
    && [ -f "$TLS/public/model-proxy.crt" ] \
    && [ -f "$TLS/proxy/model-proxy.key" ] \
    && [ -f "$TLS/platform/platform.p12" ] && [ -f "$TLS/platform/password" ] || return 1
  openssl x509 -in "$TLS/public/ca.crt" -noout -fingerprint -sha256 2>/dev/null \
    | grep -qx "$(openssl x509 -in "$TLS/ca.crt" -noout -fingerprint -sha256 2>/dev/null)" || return 1
  openssl x509 -in "$TLS/public/platform.crt" -noout -fingerprint -sha256 2>/dev/null \
    | grep -qx "$(openssl x509 -in "$TLS/platform.crt" -noout -fingerprint -sha256 2>/dev/null)" || return 1
  # 视图里的叶子也必须与根材料同源，否则容器会用旧证书。
  openssl x509 -in "$TLS/public/model-proxy.crt" -noout -fingerprint -sha256 2>/dev/null \
    | grep -qx "$(openssl x509 -in "$TLS/model-proxy.crt" -noout -fingerprint -sha256 2>/dev/null)" || return 1
  cmp -s "$TLS/proxy/model-proxy.key" "$TLS/model-proxy.key" || return 1
  [ -f "$TLS/public/truststore.p12" ] || return 1
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
  mkdir -p "$TLS/public" "$TLS/platform" "$TLS/proxy"
  cp "$TLS/ca.crt" "$TLS/public/ca.crt"
  cp "$TLS/platform.crt" "$TLS/public/platform.crt"
  cp "$TLS/model-proxy.crt" "$TLS/public/model-proxy.crt"
  cp "$TLS/platform.p12" "$TLS/platform/platform.p12"
  cp "$TLS/password" "$TLS/platform/password"
  cp "$TLS/model-proxy.key" "$TLS/proxy/model-proxy.key"
  cp "$TLS/model-proxy.crt" "$TLS/proxy/model-proxy.crt"
  # 节点是 Java：它校验受控出口要用 truststore（JVM 不认 SSL_CERT_FILE）。
  write_truststore
  normalize_views
}

# 视图的属主与模式：两条路径（复用 / 重建）都必须落到同一个状态。
# 容器里跑 keytool 时产物属主是 root —— 先复制成自己的文件再设权限。
normalize_views() {
  local store="$TLS/public/truststore.p12"
  if [ -f "$store" ] && [ "$(stat -c '%u' "$store" 2>/dev/null)" != "$(id -u)" ]; then
    cp "$store" "$TLS/public/truststore.copy"
    mv -f "$TLS/public/truststore.copy" "$store"
  fi
  local f
  for f in "$TLS/public"/*.crt "$store"; do
    chmod 644 "$f" 2>/dev/null || true
  done
  chmod 600 "$TLS/platform"/* "$TLS/proxy/model-proxy.key" 2>/dev/null || true
  chmod 644 "$TLS/proxy/model-proxy.crt" 2>/dev/null || true
  chmod 700 "$TLS/public" "$TLS/platform" "$TLS/proxy" 2>/dev/null || true
}

if reuse_ok; then
  # 根材料有效：只补齐缺失的拆分视图，**不重新签发**（重新签发会让运行中的
  # 平台与网关互相不信任）。
  # 复用路径也可能缺一张叶子（例如新增受控出口）：补齐叶子后重建视图，
  # 不动已经有效的平台证书 —— 否则运行中的平台与网关会互相不信任。
  ensure_leaf model-proxy "DNS:model-proxy,DNS:localhost,IP:127.0.0.1"
  if views_ok; then
    normalize_views
  else
    write_views
  fi
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

# 两张叶子证书，同一把 CA：platform（平台 HTTPS 面）与 model-proxy（受控模型出口）。
# 节点只信任 CA，所以它既能校验平台，也能校验模型出口。
issue_leaf() {  # issue_leaf <name> <subject-cn> <san>
  local name=$1 cn=$2 san=$3
  openssl req -newkey rsa:2048 -sha256 -nodes \
    -keyout "$TLS/$name.key" -out "$TLS/$name.csr" -subj "/CN=$cn" >/dev/null 2>&1
  cat > "$TLS/$name.ext" <<EOF
[ext]
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$san
EOF
  openssl x509 -req -in "$TLS/$name.csr" -CA "$TLS/ca.crt" -CAkey "$TLS/ca.key" \
    -CAcreateserial -days 825 -sha256 -extfile "$TLS/$name.ext" -extensions ext \
    -out "$TLS/$name.crt" >/dev/null 2>&1
  rm -f "$TLS/$name.csr" "$TLS/$name.ext"
}

issue_leaf platform platform "DNS:platform,DNS:localhost,IP:127.0.0.1"
issue_leaf model-proxy model-proxy "DNS:model-proxy,DNS:localhost,IP:127.0.0.1"

openssl pkcs12 -export -name platform \
  -inkey "$TLS/platform.key" -in "$TLS/platform.crt" -certfile "$TLS/ca.crt" \
  -out "$TLS/platform.p12" -passout "pass:$password" >/dev/null 2>&1

printf '%s\n' "$password" > "$TLS/password"
rm -f "$TLS/ca.srl"
# 只对**文件**设权限：`chmod 600 "$TLS"/*` 会把已有的 public/、platform/ 目录
# 一起改成 0600（去掉执行位），随后写视图就再也进不去。目录单独给 0700。
chmod 700 "$TLS/public" "$TLS/platform" 2>/dev/null || true
chmod 600 "$TLS"/*.key "$TLS"/*.p12 "$TLS/password" 2>/dev/null || true
chmod 644 "$TLS/ca.crt" "$TLS/platform.crt" "$TLS/model-proxy.crt"   # 公钥材料：容器内只需读

# 目录按**谁能看到什么**分开，而不是把整个 tls/ 挂给需要证书的每个容器：
#   public/   只有公开证书 —— 隔离 Gateway 与节点都挂它（它们只验证，不签名）
#   platform/ 平台自己的私钥材料 —— 只有 platform 容器挂它
#   proxy/    模型出口的私钥 —— 只有 model-proxy 容器挂它
# 其余私钥留在 $TLS 根（宿主的 0600），任何容器都看不到。
write_views
# 重新签发必然换口令：这一步不能只在复用路径上做。
sync_platform_password

openssl x509 -in "$TLS/platform.crt" -noout -checkhost platform >/dev/null
openssl x509 -in "$TLS/model-proxy.crt" -noout -checkhost model-proxy >/dev/null
echo "tls: 已生成本地 CA 与 platform 证书（$TLS；public/ 只含公开证书）"
