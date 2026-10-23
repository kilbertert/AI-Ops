## 2026-10-23 · 依赖漂移门改比依赖声明

**目标/分支**：`fix/dependency-drift-ignores-comments`，基线 `efe066c`（#1029）。

### 一、CD 卡住的原因

CD 连续两次失败在依赖漂移检查。实测：两侧 `pyproject.toml` 字节不同，但 TOML 解析后
**所有依赖声明逐项相同**——差别只有注释与 `addopts`（`-n auto` → `-n 4`，#1023 的测试
并发修复）。`uv.lock` 两侧 sha256 完全一致。

判据按字节比，问的却是「restart 会不会缺包」。注释回答不了这个问题，而按字节比会让
**每一次注释修改都永久卡住 CD**，直到有人去生产机手动跑依赖更新。

### 二、改法

- `pyproject.toml`：比依赖声明（`dependencies` / `optional-dependencies` /
  `dependency-groups` / `requires-python`），两侧用 `tomllib` 解析后规范化比较
- `uv.lock`：**保持字节比较**（机读产物，差一字节就是不同解析结果）

### 三、实测

真机比对 41 与 main 的签名 → 相同，漂移门放行。反向：改一条版本约束后签名不同，仍拦下。

### 四、守卫

`tests/test_cd_deploy_scripts.py` 新增两条，罕见地两条都做了变异验证：
语义比较改成 `if false` → 第一条转红；签名忽略 `dependencies` → 第二条转红。
第二处变异是必要的：只测「注释不影响签名」的话，签名恒为空也能通过。

### 五、边界

这次改动**只放宽了 pyproject 的判据**，没有碰 uv.lock —— 后者是真正的环境指纹。若将来
出现「依赖声明相同但 restart 仍缺包」的情形（例如 editable 安装被换成实体目录），那不是
本门能覆盖的，属于 `docs/agents/env-41-dependency-update.md` 的守卫范围。
