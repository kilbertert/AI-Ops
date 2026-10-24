#!/usr/bin/env bash
# 构建**打过补丁的** dify-web 镜像。上游 web 镜像是预构建产物（没有源码），所以
# 想改控制台界面只有一条路：按固定 tag 取源码 → 打补丁 → 构建 → 得到新镜像。
#
# 与 patches/apply.sh 的关系：那个打的是 **api** 的补丁（文件挂载即可，因为镜像里有
# 可读的 .py）；这里打的是 **web** 的补丁，必须重新构建，因为要产出编译后的 Next.js
# 产物。两者同一套纪律：**版本钉死 + sha 核对**，镜像一换就拒绝执行，而不是把陈旧
# 的补丁树烧进镜像。
#
# 跑法（开发机、root、在 deploy/dify-36/ 目录）： ./web-patches/build-web-image.sh
#
# 产出：$DIFY_WEB_PATCHED_IMAGE（默认 aiops/dify-web-patched:1.17.1），可直接被
# docker-compose.yaml 的 web.image 覆盖。
set -euo pipefail
cd "$(dirname "$0")/.."          # deploy/dify-36/

VERSION=1.17.1
VERSION_DIR="web-patches/$VERSION"
OUT_IMAGE="${DIFY_WEB_PATCHED_IMAGE:-aiops/dify-web-patched:1.17.1}"
WORK="${DIFY_WEB_WORK:-$PWD/web-patches/work}"

# 上游源码的三个文件必须与 tag 一致。换版本时这些值要一起换 —— 这正是本脚本的用处。
PINNED="
web/app/components/datasets/external-knowledge-base/create/ExternalApiSelection.tsx
web/models/datasets.ts
web/service/datasets.ts
"

echo "== 1. 取上游源码（tag $VERSION）"
if [ ! -d "$WORK/.git" ]; then
  mkdir -p "$WORK"
  git clone --filter=blob:none --depth 1 --branch "$VERSION" https://github.com/langgenius/dify.git "$WORK"
else
  git -C "$WORK" fetch --depth 1 origin "refs/tags/$VERSION"
  git -C "$WORK" checkout -q FETCH_HEAD
fi
echo "ok: $(git -C "$WORK" rev-parse --short HEAD)"

echo "== 1b. 核对上游文件未被改动过（打补丁的前提）"
# 与 patches/apply.sh 同一条纪律：tag 一换，下面的 sha 就对不上，本脚本退出，而不是把
# 陈旧的补丁树烧进镜像。这三个 sha 对应 langgenius/dify@1.17.1。
check_sha() {
  local rel=$1 want=$2
  local got
  got=$(git -C "$WORK" show "HEAD:$rel" | sha256sum | cut -d' ' -f1)
  if [ "$got" != "$want" ]; then
    echo "FAIL: $rel 与 1.17.1 不一致" >&2
    echo "  期望 $want  实际 $got" >&2
    echo "  上游变了。补丁必须按新版本重做，不能把陈旧结果烧进镜像。" >&2
    exit 1
  fi
  echo "ok: $rel"
}
check_sha web/app/components/datasets/external-knowledge-base/create/ExternalApiSelection.tsx 1cfcac7b934c7ae183e1361c790e1063fc944e70f4acc32e4f40bdec6cdbea10
check_sha web/models/datasets.ts 7da7895349e90cc963c725b12826be71c4f45827d829b23cfe077a18fb492ac9
check_sha web/service/datasets.ts e5277febabf43ffc3d54b8fa6ff52305e9fd5ef18052c1ee404e78f958425a01

echo "== 2. 打补丁（--fuzz=0：上下文对不上就失败）"
# **先把工作树恢复干净。** 上一次构建留下的补丁产物会让 `patch` 停下来问
# "文件已存在，要覆盖吗" —— 非交互环境里它默认答"不"，于是那一条补丁被**静默跳过**
# （"1 out of 1 hunk ignored"），构建继续、产出却没有那个改动。
#
# 2026-10-24 实测：第二次构建时 `web/service/knowledge-sync.ts` 已存在，补丁被跳过。
# 恢复 + 删掉补丁新增的文件，两步都要 —— `git checkout --` 不管未跟踪文件。
git -C "$WORK" checkout -- .
# **清掉所有未跟踪文件，不只那一个。** 上一次构建留下的 `.rej`（补丁被拒时写的）
# 与补丁新增的文件都在这里 —— 只清一个名字不够：2026-10-24 实测，`.rej` 留着时
# `patch` 会认为"这个补丁已经试过了"，于是又跳过。
git -C "$WORK" clean -fdq
# `clean -fdq` 不删被 .gitignore 忽略的东西；补丁产物不在 ignore 里，所以够用。
# 保险起见再显式删一次那个已知的新增文件。
rm -f "$WORK/web/service/knowledge-sync.ts"

for p in "$VERSION_DIR"/*.patch; do
  # `--batch` 不是可选的：没有它，`patch` 遇到"文件已存在 / 看起来已应用过"会**停下来
  # 问**，而非交互环境里默认答"不" —— 于是那个补丁被**静默跳过**，构建继续，产出一个
  # 缺改动的镜像。2026-10-24 实测：3 个文件、7 个 hunk 全被忽略，脚本仍然退出 0。
  # `-N` = 不要反向应用。`--batch` 遇到"看起来已应用过"会自动选 `-R`，把补丁
  # **反向**打回去 —— 比跳过更坏：源看起来"有改动"，实际回到了原始状态。
  patch -p1 --fuzz=0 --batch -N -s -d "$WORK" <"$p"
  echo "ok: $(basename "$p")"
done
# 补丁里不含新文件（diff -u 只覆盖改动），新组件单独拷进去。
cp "$VERSION_DIR/ExternalKnowledgeIdPicker.tsx" \
   "$WORK/web/app/components/datasets/external-knowledge-base/create/ExternalKnowledgeIdPicker.tsx"

# **打完补丁要验它真的进去了。** 上面所有防护都是"让失败变明显"，但没有一条
# 证明改动真的落到了源里 —— 而这个脚本一旦产出镜像就会上生产。
# 判据是每个补丁里 `+++ b/` 指到的文件，都得带着一个只有我们才有的标记。
for marker in "knowledge-sync" "ExternalKnowledgeIdPicker" "useKnowledgeSync"; do
  if ! grep -rq "$marker" "$WORK/web/app" "$WORK/web/service" 2>/dev/null; then
    echo "FAIL: 补丁没打上（找不到 $marker）—— 拒绝构建" >&2
    exit 1
  fi
done
echo "ok: 补丁已落进源（两个标记都在）"

echo "== 3. 把构建命令换成 webpack"
# 关键：**用 webpack 而不是 Turbopack**。实测 Turbopack（上游默认）在本机会在
# "Creating an optimized production build ..." 处长时间零产物，三次尝试分别被
# OOM(137)/超时(124)/超时(124) 结束；webpack 在同一棵树上 3.8 分钟编译成功。
# 上游 Dockerfile 写的是 `pnpm build`（turbopack），所以这里改它。
#
# `pnpm build:vinext` 也去掉：那是上游的另一套运行时（EXPERIMENTAL_ENABLE_VINEXT），
# 本实例 compose 里 EXPERIMENTAL_ENABLE_VINEXT=false，不需要它，而且它会再跑一遍
# 前端构建。
python3 - "$WORK/web/Dockerfile" <<'PYEOF'
import sys
p = sys.argv[1]
s = open(p).read()
old = "RUN pnpm build && pnpm build:vinext"
new = "RUN rm -rf .next/cache && pnpm build --webpack"
if old not in s:
    raise SystemExit(f"FAIL: Dockerfile 里没有预期的构建行：{old!r}")
s = s.replace(old, new, 1)

# 去掉 vinext 的产物 COPY。`build:vinext` 不再跑 ⇒ `/app/web/dist/standalone`
# 根本不存在，而 production stage 仍有一条 `COPY … ./targets/vinext` —— 构建会停在
# "failed to calculate checksum … not found"。本实例 compose 里
# EXPERIMENTAL_ENABLE_VINEXT=false，这个 target 从来不被用到。
#
# 上一版脚本只改了构建命令、没删这条 COPY，2026-10-24 实测构建失败在 Dockerfile:80。
vinext_copy = "COPY --from=builder --chown=dify:dify /app/web/dist/standalone ./targets/vinext\n"
if vinext_copy not in s:
    raise SystemExit(f"FAIL: Dockerfile 里没有预期的 vinext COPY 行：{vinext_copy!r}")
s = s.replace(vinext_copy, "", 1)
open(p, "w").write(s)
print("ok: Dockerfile 构建行 -> webpack，并去掉 vinext 产物 COPY")
PYEOF

echo "== 4. 构建镜像"
# **`--no-cache-filter builder` 不是优化项，是正确性项。**
#
# 2026-10-24 实测：第三次构建时 `RUN pnpm build --webpack` 报 `CACHED` —— Docker 认为
# 上方 COPY 的层没变，于是**根本没重新编译**。镜像产出了，但里面是上一次的代码。
# 而源树里补丁确实应用了（`git status` 能看到 5 个文件被改）。
#
# 只对 builder 阶段关缓存：production 阶段的三条 COPY 是廉价的，缓存它们没问题。
docker build \
  --build-arg NODE_VERSION=24.20.0 \
  --no-cache-filter builder \
  -f "$WORK/web/Dockerfile" \
  -t "$OUT_IMAGE" \
  "$WORK"

echo
echo "ok: $OUT_IMAGE"
echo "下一步：在 deploy/.env 里写 DIFY_WEB_IMAGE=$OUT_IMAGE，然后"
echo "  docker compose up -d --force-recreate web   # 只 up 不会重读镜像，必须 force-recreate"
