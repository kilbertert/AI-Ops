# 41 依赖环境更新流程

> **这是一份人工流程，不是 CD 的一部分。** CD（`.github/workflows/cd.yml`）只同步
> `src/` 与运行时参考资料，**不同步依赖**；依赖清单不一致时它**拒绝部署**并报出差异。
> 本文是那时候该做的事。
>
> **2026-10-09 补记（#609）**：本轮按本文走了一遍（#605 引入 `pyyaml` 造成的漂移），
> 有三处措辞与现场实测不符，已在下文逐条改正并标注 —— 尤其第 3 步的 editable 判据，
> 按原文执行会在**完全正常**的状态下误报。对照记录见 `docs/validation.md`。

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
# uv 不在 PATH，但在 venv 里（实测 2026-10-09）。用**绝对路径**调它，别指望 PATH：
# 下面那句 `command -v uv` 在 41 上就是失败的。
UV=/opt/aiops-41/.venv/bin/uv
[ -x "$UV" ] || { echo "找不到 $UV —— 见本文「边界与已知缺口」" >&2; exit 1; }
# --frozen：不重新解析 lockfile，清单就是目标 commit 的那份。
# --active：必须装进既有 venv，不能新建。
VIRTUAL_ENV=/opt/aiops-41/.venv PATH=/opt/aiops-41/.venv/bin:$PATH "$UV" sync --active --frozen --no-progress
echo "sync-done"'
```

> **`--no-install-package aiops-diagnostics` 这条看似更保险，但它会让 editable 直接消失。**
> 实测（2026-10-09）：带上它，`uv` 会把本包**卸载**，`.pth` 一并删掉，
> `import aiops_diagnostics` 变成 `ModuleNotFoundError` —— 比"实体目录"更坏。
> **照上面原样跑**：`uv sync` 会重新生成 `.pth`（时间戳更新）并额外落一份静态资产目录，
> 而实测确认解释器仍解析到 `/opt/aiops-41/src`（`sys.path` 里 `site-packages` 在前，
> 但那里没有 `__init__.py`，所以常规包照样赢 —— 见第 3 步）。

### 3. **必做**：确认 editable 没被换掉

用守卫脚本，别手敲：

```bash
deploy/check-41-editable.sh            # 只读；退出码 0 = 通过
deploy/check-41-editable.sh --self-check   # 无网络，只测判据本身
```

> **⚠️ 本步骤原写的是「`site-packages/aiops_diagnostics` 是目录即失败」。那条判据是错的。**
> 本包的 `pyproject.toml` 用 hatch 的 force-include 把 `.env.example` / SOP /
> `faq_catalog.json` 等静态资产装进了包目录，所以**那个目录在正常情况下就该存在** ——
> 刚跑完 `uv sync` 时它必然出现（2026-10-09 实测：`uv sync` 之后它是 12 个**静态资产
> 文件**，一个 `.py` 都没有）。按原措辞执行会在完全正常的 post-sync 状态**误报**，
> 然后把一次成功的依赖更新回滚掉。
>
> **真正的判据是 Python 的导入规则**：那个目录里若出现 `__init__.py`，它就成了**常规
> 包**，而常规包赢过 editable 的 `.pth`（`.pth` 追加在 `site-packages` **之后**，常规包
> 在更早的 `site-packages` 里就被找到）—— 此时 `import aiops_diagnostics` 解析到
> site-packages 那份，src 同步静默失效。没有 `__init__.py` 时它只是命名空间片段，
> `sys.path` 上更靠后的 `/opt/aiops-41/src` 照样赢。
>
> 守卫脚本实现的正是这条规则（四条观察）：① `import` 真的解析到哪、② 子模块解析到哪、
> ③ `__init__.py` 在不在、④ `files(pkg)` 读的包内数据来自哪。前两条是判据，
> 后两条解释"为什么会这样"。

**若守卫报失败**：立刻回滚（第 5 步）—— 这种状态下 CD 会「成功」但实际没换代码。

### 4. 重启并校验

> **`curl` 要打网关实际监听的地址，不是 `127.0.0.1`。** 41 上服务绑的是
> `172.18.0.1:8788`（实测 2026-10-09；`127.0.0.1:8788` 直接 connection refused，
> 那不是故障）。先看 `ss -ltnp | grep 8788` 确认地址，再 curl 它。

```bash
ssh aiops-41 '
systemctl restart aiops-gateway-41.service && sleep 6
systemctl is-active aiops-gateway-41.service
curl -s --max-time 8 http://172.18.0.1:8788/health'
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

### 7. 重跑部署

依赖环境就位后**重跑一次部署**（workflow 的 `workflow_dispatch`，或本地
`deploy/deploy-41.sh --commit <sha>`）：源码要跟着换，否则服务跑的还是旧代码。
重跑后核对 `/health` 的 `version` 等于目标 commit 短 sha，并**再跑一次第 3 步的守卫**
—— 部署本身不碰依赖，但这一步让"换完代码之后 editable 仍生效"成为一条被观测到的
事实，而不是从"部署前是对的"推出来的。

## 边界与已知缺口

- **不在 41 上装 `uv` 之外的工具链**。该机是公司生产宿主，只动 `/opt/aiops-41/`。
- **`uv` 在 venv 里，不在 PATH**（实测 2026-10-09：`command -v uv` 失败，
  `/opt/aiops-41/.venv/bin/uv --version` → 0.11.14）。上文的绝对路径调用是必须的。
- **Python 版本不随本流程变**。41 是 3.12.3；若某次改动抬高 `requires-python`，
  这条流程解决不了，需要独立决定（换解释器/换宿主）。
- **首次部署**（41 上还没有任何备份）时无回滚目标，失败只能人工介入。
- **判据本身有离线回归，流程执行没有。** `deploy/check-41-editable.sh` 的判据由
  `tests/test_env_41_editable_guard.py` 覆盖（导入规则用真解释器跑，真机分支用桩
  `ssh` 驱动），`--self-check` 在 CI 里可跑。但**在 41 上按下第 1–7 步执行**这件事
  仍然靠人，并留记录 —— 这是有意的：它改的是运行环境，出错代价高于源码部署。
