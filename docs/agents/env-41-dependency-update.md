# 41 依赖环境更新流程

> **这是一份人工流程，不是 CD 的一部分。** CD（`.github/workflows/cd.yml`）只同步
> `src/` 与运行时参考资料，**不同步依赖**；依赖清单不一致时它**拒绝部署**并报出差异。
> 本文是那时候该做的事。

## 为什么依赖不走 CD

做过一版自动同步，钻演后撤掉。三条理由，第一条是硬的：

1. **`uv sync` 会破坏 editable 安装。** 41 上 `aiops_diagnostics` 是 editable 安装
   （`_editable_impl_aiops_diagnostics.pth` 指向 `/opt/aiops-41/src`），`src/` 同步
   生效**完全依赖**这一点。而 `uv sync` 倾向装成实体目录 —— 一旦 `site-packages/`
   里出现实体 `aiops_diagnostics/`，之后同步 `src/` 就**静默失效**：`import` 仍成功，
   但拿到的是实体目录里的旧代码，而 `/health` 还会报新 commit。
   （实测踩到过：2026-09-23 钻演在生产上留下实体目录，须人工清理才恢复。）
2. **它是「改运行环境」，与「传一次源码」是两件事。** 两者失败时的爆炸半径不同，
   混在一个流程里会让源码被依赖问题拖下水。
3. **失败时会把源码一起拖下水。** 依赖那步若在源码替换之后执行，坏掉就同时污染两边。

所以：依赖变更走本文的人工流程，源码走 CD。

## 判据：什么时候需要走这个流程

CD 会在**上传前**拒绝并报出两侧 sha 差异：

```
依赖漂移：目标 commit 的 pyproject.toml 与 41 上的不一致
  目标 commit (<sha>) <hash-a>
  41                 <hash-b>
```

日常核对（只读，随时可跑）：

```bash
cd <canonical checkout>
for f in pyproject.toml uv.lock; do
  printf '%-16s 本地=%s  41=%s\n' "$f" \
    "$(sha256sum "$f" | cut -c1-16)" \
    "$(ssh aiops-41 "sha256sum /opt/aiops-41/$f" | cut -c1-16)"
done
```

两者都相同 = 无需动作，CD 可正常部署。

## 更新流程

> 与源码部署一样：**备份 → 更新环境 → 重启 → 校验**。区别是这步在 41 上装包，
> 属于「改运行环境」，因此**必须留下记录**（写进 `docs/validation.md`）。

### 1. 备份当前环境

```bash
ssh aiops-41 '
set -e
TS=$(date +%Y%m%d-%H%M%S)
B=/var/backups/aiops-41/deps-$TS
mkdir -p "$B"
cp -a /opt/aiops-41/pyproject.toml /opt/aiops-41/uv.lock "$B/"
cp -a /opt/aiops-41/.venv "$B/venv"
echo "backup=$B"'
```

记下 `backup=` 路径 —— 回滚要用。

### 2. 取目标 commit 的清单并同步

```bash
# 本地：把目标 commit 的清单传到 41
cd <canonical checkout>
git show <target-sha>:pyproject.toml > /tmp/pyproject.toml
git show <target-sha>:uv.lock        > /tmp/uv.lock
scp -q /tmp/pyproject.toml /tmp/uv.lock aiops-41:/tmp/

ssh aiops-41 '
set -e
cp /tmp/pyproject.toml /tmp/uv.lock /opt/aiops-41/
cd /opt/aiops-41
# uv 随镜像/产物带过来过；没有就装一个静态二进制。
command -v uv >/dev/null 2>&1 || {
  echo "uv 不在 PATH —— 用产物里的那份，或按 https://docs.astral.sh/uv/ 装" >&2; exit 1; }
# --frozen：不重新解析 lockfile，清单就是目标 commit 的那份。
# --active 之外的路径不要用：必须装进既有 venv，不能新建。
VIRTUAL_ENV=/opt/aiops-41/.venv uv sync --active --frozen --no-progress
echo "sync-done"'
```

### 3. **必做**：确认 editable 没被换掉

```bash
ssh aiops-41 '
SP=/opt/aiops-41/.venv/lib/python3.12/site-packages
if [ -d "$SP/aiops_diagnostics" ]; then
  echo "失败：aiops_diagnostics 成了实体目录 —— src/ 同步将不再生效" >&2; exit 1
fi
[ -f "$SP/_editable_impl_aiops_diagnostics.pth" ] || {
  echo "失败：editable .pth 不见了" >&2; exit 1; }
cd /opt/aiops-41
/opt/aiops-41/.venv/bin/python -c "import aiops_diagnostics as a; print(\"import →\", a.__file__)"
'
```

`import` 必须指向 `/opt/aiops-41/src/aiops_diagnostics/__init__.py`。
**若不是，立刻回滚**（第 5 步）—— 这种状态下 CD 会「成功」但实际没换代码。

### 4. 重启并校验

```bash
ssh aiops-41 '
systemctl restart aiops-gateway-41.service && sleep 6
systemctl is-active aiops-gateway-41.service
curl -s --max-time 8 http://127.0.0.1:8788/health'
```

服务 `active` 且 `/health` 返回 `ok:true`、版本为当前已部署 commit。

**这一步通过之后**，CD 的漂移门才会放行该 commit。重跑一次部署 workflow（或本地
`deploy/deploy-41.sh --commit <sha>`）让源码同步到与之匹配的版本。

### 5. 回滚（环境装坏了）

```bash
ssh aiops-41 '
set -e
B=<第 1 步的 backup 路径>
cp -a "$B/pyproject.toml" "$B/uv.lock" /opt/aiops-41/
rm -rf /opt/aiops-41/.venv
cp -a "$B/venv" /opt/aiops-41/.venv
systemctl restart aiops-gateway-41.service && sleep 6
systemctl is-active aiops-gateway-41.service
curl -s --max-time 8 http://127.0.0.1:8788/health'
```

回滚后同样跑第 3 步的 editable 检查。

### 6. 记录（不得省略）

在 `docs/validation.md` 追加一节，写清：目标 commit、两份清单的 sha、`uv sync` 输出
摘要、editable 检查结果、`/health` 结果、备份路径。**没有这段记录，这次环境变更就
没有可追溯的证据** —— 下次出问题无法判断生产环境到底是什么状态。

## 边界与已知缺口

- **不在 41 上装 `uv` 之外的工具链**。该机是公司生产宿主，只动 `/opt/aiops-41/`。
- **Python 版本不随本流程变**。41 是 3.12.3；若某次改动抬高 `requires-python`，
  这条流程解决不了，需要独立决定（换解释器/换宿主）。
- **首次部署**（41 上还没有任何备份）时无回滚目标，失败只能人工介入。
- 本流程目前**没有被自动化测试覆盖** —— 它靠人按步骤执行并留记录。这是有意的：
  它改的是运行环境，出错代价高于源码部署，不适合无人值守。
