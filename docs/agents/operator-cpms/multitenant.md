# 同环境复制管家智能体

适用：已有可用 Java 管家，为同一公司环境中的另一个租户增加独立应用。
沿用 ADR-0059、0074、0085、0155；不新增执行器、接口或授权规则。
验收见 [场景](multitenant.feature) 与 [步骤](multitenant-qa.md)。

## 四种身份分别管理

| 对象 | 权威来源 | 复制时处理 |
|---|---|---|
| 公司租户 | 公司身份中的 tenant_id | 使用明确目标 ID；名称只用于展示 |
| Dify 工作区 | Gateway 的 `(tenant_id, environment)` 映射 | 新租户独立 workspace UUID |
| 应用与工具 | 该 workspace 的 app、provider | 导入得到新 app UUID，工具换成本工作区 provider UUID |
| 订单读取权限 | 本次正常公司登录、订单归属与站点范围 | 每次重新校验，不能从源应用复制 |

流程：核对源与目标 → 工作区开户 → 本工作区模型与工具 → 导入方法论 →
租户应用登记 → 节点目标 → 发布 → 正常登录验收 → 双向隔离检查。
`41` 是登录/工作区部署环境；现役节点逻辑环境是 `production-41`，沿用源目标的值。
同一 Java 节点可以处理两个租户，但每个 run 的订单、主体、工具、期限单独授权。

## 操作准备

读取 [当前状态](../current-delivery-state.md)、最新 validation、[41 手册](../env-41-runbook.md)
与 [退役操作](../runtime-retirement-operations.md)。访问只用项目声明的 `dev-host` 41/36。
以下 Python 片段在 41 服务目录、服务身份下运行：

```bash
dev-host exec 41 --allow-service-exec -- \
  "cd /opt/aiops-41 && runuser -u aiops41 -- bash -c 'set -a; . /etc/aiops-41/gateway.env; set +a; exec /opt/aiops-41/.venv/bin/python -'" <<'PY'
# 在此放入本节所需的 Python 片段。
PY
```

Python 以 `GatewayServerSettings.from_env()` 读进程配置；业务配置另用
`Settings.from_config(Path('/etc/aiops-41/production.env'))`。不打印配置对象或密钥。
初始化 `DifyConsoleAdmin(ConsoleApiSettings(settings.dify_console_base_url,
settings.dify_console_api_key))`，下称 `console`。
以显式 workspace 调用 `console._call(workspace, path, ...)`，不修改任何账号的 current。

复制前保存源 `GET /console/api/apps/{app}` 与 `.../{app}/export`、节点目标文件和现役路由；
目录0700、文件0600。运行结束回比源 model_config 与原路由。数据库备份使用 SQLite backup，
不能复制运行中的单个 SQLite 文件后忽略 WAL。回滚优先撤回本次新路由，不倒回整库。

## 开户与复制

1. 查询 `DifyWorkspaces(database).row(tenant, settings.environment)`；不存在才开户。
   命令必须同时加载 gateway.env 并传 `--config` 和 `--db`：
   `aiops-admin --config /etc/aiops-41/production.env ensure-workspace --tenant <目标租户> --company <展示名> --db /var/lib/aiops-41/gateway/gateway.db`。
   重跑同一命令应返回原 UUID；不手工创建第二个 workspace。
2. 从源应用确认模型 provider/name、输出预算、工具与 dataset 引用。本流程只复制无知识库的
   管家；存在 dataset 时停止，先按目标租户知识库开户和映射流程处理，不搬旧 dataset UUID。
3. 用 `installed_plugin_ids(source_workspace)` 找源模型的精确插件标识，在目标工作区
   `install_plugins(target_workspace, identifiers)`。只安装实际依赖的插件。
4. 新工作区模型凭据通过 Dify 原生模型 API 配置。源
   `GET /console/api/workspaces/current/model-providers/{provider}/models/credentials?model={name}&model_type=llm`
   只用于读取能力设置；GET 的 api_key 是掩码，不能复制。
   从受控模型出口凭据文件读取真实供应商 key，在目标工作区 `POST .../models/credentials`，
   body 为 `model/model_type/name/credentials`。本次授权复用现役模型渠道，业务公司令牌绝不复用。
   本轮凭据位置见 [节点运行配置](../../../deploy/node-runtime/README.md)；只在服务进程内读取。
   已有目标凭据先回读，不覆盖不明配置。核 `max_tokens_to_sample >= 应用 max_tokens`，
   工具能力为 `tool_call/supported`；[参数排错](../dify-model-parameters.md)给出验证方法。
   `ensure-ready` 的默认 deepseek 模板与本次 OpenAI-compatible 应用不同，不能盲跑默认模板。
5. 调用已部署 `ops_provider.ensure_node_provider(console, target_workspace, gateway_https_url,
   json.loads(Path(source_target.manifest_file).read_text()))` 创建本工作区 cpms-node。
   按工具目录取其真实 UUID；认证必须为 none，schema 为两项无参数工具。
6. 读取 export 返回的 `data`，用 YAML 解析，仅修改 app 名称和两个工具的 provider_id；
   不复制会话、发布版本、用户成员、订单或共享业务凭据。按名称核重（命中多份即停止），
   调用 `console.import_app_dsl(target_workspace, yaml_content=..., name=...)`。
   保存新 app UUID；回读核对提示词逐字相同、工具均指向目标 provider、dataset 为空。
   用 `node_publication.candidate(DifyDslSource(...), target)` 核对真实导出能够生成节点清单，
   之后 `DifyWorkspaces(database).mark_ready(tenant, settings.environment)`。

## 登记与发布

先把新租户 `[[agents]]` 与 `[[dify_apps]]` 加到仓库 `ops/environments/env-41.toml`，
经 PR/CI 合并后再传摘要制品；CD 不自动更新现场 ops 清单。以下只处理新租户，
禁止为了新增一行对所有 agent 跑整份 reconcile。

**若该客户改用 Dify 页面“复制并开通”，则反向操作**：开通后该 `(租户, operator)` 进入
`operator_apps`、成为页面管理的受管应用，清单**必须删掉它的 `[[dify_apps]]`**（乃至 `[[agents]]`）。
两份声明同时存在时，`reconcile_dify_registry` 会按“页面管理的管家不能由环境清单覆盖”
**在写入前中止整份清单**——不是只跳过这一个租户。删掉是交还管理权，不是撤回：
库里的 agent 与 `agent_versions` 保留，`--prune` 也跳过页面管理的行。
2026-10-29 优链充即此情形（清单已对齐，见同期验收回执）。

1. `node_publication.targets(settings.chat_node_targets_file)` 找源管家目标；
   `dataclasses.replace(source_target, app_id=new_app, tenant_id=new_tenant)` 产生新目标。
   复用现役 config_file、manifest_file、CPMS 地址及回调，不新开端口、不更换平台模型。
   验证候选后向目标 JSON 只追加一行，原行逐字保留；已存在同一 app/tenant 时必须相等才跳过。
   写私有同目录临时文件、校验 `targets(temp_file)`，比对旧文件摘要未变化后原子替换。
2. 用已有 `AgentManager`、`AgentStore`、`admin_context(new_tenant)` 创建目标 agent。
   初始 `AgentConfig` 的 prompt/model 取本次 Dify 导出并通过当前模型白名单，
   `agent_type=operations/output_contract=diagnosis-v1/knowledge_base_ids=()/allowed_tools=()`。
   初始版本只是让已有节点发布器获得 agent 身份，不开放旧工具；实际节点模型仍由模板决定。按
   `allowed_models_from_settings(Settings.from_config(...))` 保留模型发布门。
   创建后调用 `manager.publish(admin_context(new_tenant), agent.agent_id,
   expected_revision=agent.revision)`，回读 published 状态和版本号；已有 published agent 直接复用。
   同名草稿仅在配置完全符合本次初始配置时继续发布，否则停止。之后节点发布将 prompt/两工具冻结为 node-sop。
3. 从本次合并的完整清单调用 `reconcile_dify_registry(registry, manifest, dry_run=True)`，
   预期只有目标 operator 行新增。确认后 `dry_run=False`，再跑必须全 unchanged。
4. 通过既有 `POST /v1/dify/publish` 发布 `{"app_id": new_app}`，
   Authorization 使用 `settings.dify_publish_api_key`，不猜配置所在文件。
   回读目标 `operator_order_diagnosis` 路由为 node/active，app、租户、agent 版本准确匹配。
   重跑发布应 unchanged，不产生新版本。原租户所有路由与备份一致。

新路由准备好后才通知测试人。正常公司登录会按租户加入工作区；平台运维确需编辑时按
[成员开通](../env-41-runbook.md)定点授权，不能用成员关系扩大业务数据权限。
若公司登录没有 aiops:agent:edit，应按公司授权流程处理，不能伪造账号或给全部管理员开通。

### 初始版本与登记的可执行片段

节点目标追加可直接使用以下片段（在前述服务身份入口中）。目标文件必须属本项目服务身份，
先 `apply=False` 看候选；确认源目标和新 app/tenant 后才改 True。只追加新行，不替换原行。

```python
import json
import os
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from aiops_diagnostics.dify.dify_dsl_pull import DifyDslSource
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.gateway_server import GatewayServerSettings
from aiops_diagnostics.execution.node_publication import candidate, targets

apply = False
settings = GatewayServerSettings.from_env()
path = Path(settings.chat_node_targets_file)
original = path.read_bytes()
source_target = next(
    t
    for t in targets(str(path))
    if t.app_id == "137cc417-f068-43c2-9d4a-43ebd254ea01" and t.entry == "operator"
)
target = replace(
    source_target, tenant_id="1941847786056323072", app_id="349a637c-8f86-41b9-b932-e9818fb39a97"
)
workspace = DifyWorkspaces(Path("/var/lib/aiops-41/gateway/gateway.db")).resolve(
    target.tenant_id, settings.environment
)
candidate(
    DifyDslSource(settings.dify_console_base_url, target.app_id, settings.dify_console_api_key, workspace),
    target,
)
body = json.loads(original)
existing = [
    r
    for r in body["targets"]
    if r["app_id"] == target.app_id
    or (r["tenant_id"] == target.tenant_id and r.get("entry", "consumer") == "operator")
]
assert not existing or existing == [asdict(target)]
if not existing:
    body["targets"].append(asdict(target))
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".node-targets-")
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(body, output, ensure_ascii=False)
        targets(temporary)
        if apply:
            assert path.read_bytes() == original, "目标已被其他操作修改，请重新核对"
            os.replace(temporary, path)
        print("目标校验通过", "已追加" if apply else "仅预演")
    finally:
        Path(temporary).unlink(missing_ok=True)
else:
    print("目标未变化")
```

下面放入前述服务身份 Python 入口。`manifest_path` 必须指向摘要已核对的合并清单；
只填写新应用对应的租户。先 `apply=False` 核输出，再改为 True；不跑全量 agent reconcile。

```python
from dataclasses import replace
from pathlib import Path
import yaml
from aiops_diagnostics.publication.agent_lifecycle import (
    AgentManager,
    AgentStore,
    allowed_models_from_settings,
)
from aiops_diagnostics.publication.agent_manifest import admin_context, load_manifest, reconcile_dify_registry
from aiops_diagnostics.config import Settings
from aiops_diagnostics.dify.dify_app_registry import DifyAppRegistry
from aiops_diagnostics.dify.dify_dsl_pull import DifyDslClient, DifyDslSource, _declared_dataset_ids
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.gateway_server import GatewayServerSettings

apply = False
tenant = "1941847786056323072"
manifest_path = Path("/opt/aiops-41/ops/environments/env-41.toml")
database = Path("/var/lib/aiops-41/gateway/gateway.db")
settings = GatewayServerSettings.from_env()
manifest = load_manifest(manifest_path)
declared = next(a for a in manifest.agents if a.tenant_id == tenant)
binding = next(b for b in manifest.dify_apps if b.tenant_id == tenant and b.business_entry == "operator")
workspace = DifyWorkspaces(database).resolve(tenant, settings.environment)
source = DifyDslSource(
    settings.dify_console_base_url, binding.app_id, settings.dify_console_api_key, workspace
)
doc = yaml.safe_load(DifyDslClient(source).export_dsl())
assert not _declared_dataset_ids(doc["model_config"])
assert doc["model_config"]["model"]["name"] == declared.config.model
config = replace(declared.config, prompt=doc["model_config"]["pre_prompt"], allowed_tools=())
manager = AgentManager(
    AgentStore(database),
    allowed_models=allowed_models_from_settings(Settings.from_config(Path("/etc/aiops-41/production.env"))),
)
assert config.model in manager.allowed_models
context = admin_context(tenant)
matches = [a for a in manager.list(context) if a.name == binding.agent_name]
assert len(matches) <= 1
registry = DifyAppRegistry(database)
reports = reconcile_dify_registry(registry, manifest, dry_run=True)
assert all(
    r.action == "unchanged"
    or (r.tenant_id == tenant and r.business_entry == "operator" and r.action == "registered")
    for r in reports
)
print([(r.tenant_id, r.business_entry, r.action) for r in reports])
if apply:
    agent = (
        matches[0]
        if matches
        else manager.create(context, name=binding.agent_name, description=declared.description, config=config)
    )
    if agent.status == "draft":
        assert agent.config == config
        manager.publish(context, agent.agent_id, expected_revision=agent.revision)
    agent = manager.get(context, agent.agent_id)
    assert agent.status == "published" and agent.published_version is not None
    reconcile_dify_registry(registry, manifest, dry_run=False)
    assert all(r.action == "unchanged" for r in reconcile_dify_registry(registry, manifest, dry_run=True))
    print("初始版本与登记完成", agent.agent_id, agent.published_version)
```

最终发布使用同一服务身份、同一配置读取器，可直接执行：

```python
import json
from urllib.request import Request, urlopen
from aiops_diagnostics.gateway_server import GatewayServerSettings

settings = GatewayServerSettings.from_env()
request = Request(
    "http://172.18.0.1:8788/v1/dify/publish",
    data=json.dumps({"app_id": "349a637c-8f86-41b9-b932-e9818fb39a97"}).encode(),
    headers={"Authorization": "Bearer " + settings.dify_publish_api_key, "Content-Type": "application/json"},
)
with urlopen(request, timeout=60) as response:
    print(response.status, response.read().decode())
```

## 验证与恢复

- 用 workspace 定向管理 API 双向访问对方 app 应404；这是应用容器边界验证，不能冒充真人权限验收。
- 把源 provider UUID 放进目标 candidate 应拒绝；证明导入后不能暗连旧工具。
- 正常 B 公司登录、明确 B 订单，在原生预览观察完整报告，再按本次 QA/run 核租户、
  node-41、succeeded/matched、双 CPMS 证据。不能拿“最近成功”代替这次运行。
- 合法 A/B 会话做跨租户订单与 QA 回读负例；401无登录、410过期不算隔离通过。
- 失败时用既有 retract 撤回本次目标路由，保留新应用与审计便于修复；原租户不受影响。
  不删除工作区、不恢复整库、不套用旧自动撤回定时器。恢复后按原 publish 流程复验。

本次不扩展“上一单”查询，也不复制源租户知识库。真实订单成功不代表故障根因准确率验收。

## 管家 App 验收与账号前置

Dify 登录页当前是公司账号、密码、图形验证码；手机号短信码不能替代该登录方式。
管家 App 的“验证码登录”调用公司 `/upms/token/loginByPhone`（41附 `grant_type=sms_login`），
使用 App 现有 client-type 和租户配置。用户提供有效验证码时不额外发送短信。
浏览器无公网可达性时，隧道的现役实现见
[用户端浏览器驱动](../../../tools/consumer-status/status_probe.py)中的 `tunnel_argv`、
`_wait_port` 和 `_terminate_tree`；通过 `dev-host exec 41` 建443转发并按端口和进程精确清理。
管家复用该方法，只将浏览器目标换成管家域名：

```python
import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

path = Path("tools/consumer-status/status_probe.py")
spec = importlib.util.spec_from_file_location("consumer_browser", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
assert not module._wait_port(port, timeout=0.2)
tunnel = subprocess.Popen(
    module.tunnel_argv(target="41", port=port),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
    start_new_session=True,
)
pgid = os.getpgid(tunnel.pid)
try:
    assert module._wait_port(port, timeout=15)
    assert module._listening_pids(port) & module._descendants(tunnel.pid)
    with sync_playwright() as playwright:
        rule = (
            f"MAP ulinkmanage.h5.mall.qushiyun.com:443 127.0.0.1:{port},"
            f"MAP api.mall.qushiyun.com:443 127.0.0.1:{port}"
        )
        browser = playwright.chromium.launch(headless=True, args=[f"--host-resolver-rules={rule}"])
        try:
            page = browser.new_page()
            page.goto("https://ulinkmanage.h5.mall.qushiyun.com/", wait_until="domcontentloaded")
            # 在这里按下述步骤使用正常公司登录和原页面。
        finally:
            browser.close()
finally:
    module._terminate_tree(tunnel, pgid=pgid, port=port)
    assert not module._listening_pids(port)
```

只映射现役域名，保留HTTPS主机名与证书验证，不设置ignore_https_errors。

1. 正常登录后读取公司 `/upms/user/info`，核 tenantId；用 Gateway 的现役
   `_caller_resolver(settings).resolve(..., platform_entry='operator')` 核主体及站点范围。
   token通过stdin或私有浏览器storage传递，不出现在日志、命令参数或提交。
2. 租户主账号沿用既有all规则；普通/代理商账号的空站点范围不能用于成功取数正例。
   再查 `_operator_site_scope(Settings.from_config(...)).site_scope_by_b_user_id(...)`
   区分“权威目录明确空集合”和目录错误；不能把空集合改成all。
3. 原首页“智能检测”→聊天页“订单检测”→原订单选择器→发送，记录本次POST响应。
   成功需要202、QA completed、唯一Java run succeeded/matched、双工具及原页面完整报告。
   单纯模型冒烟、订单列表200或路由active不能替代这些条件。
4. 若列表有订单而诊断空范围403，分别记录公司列表、UPMS店铺集合和站点归属；
   不以列表可见反推账号获得了业务授权。账号换成已有站点权限的测试人后再跑正例。

2026-10-29首个测试账号复现上述差异：公司订单列表返回租户全部694条，UPMS店铺集合和
两种站点归属均0；原页面诊断403、节点run为0。公司release源码的StaffSiteScopeService
只对type=51加新范围约束，type=5仍沿用旧拦截逻辑；旧逻辑存在空集合不附加限制的分支。
本任务不改变公司业务权限或修补公司仓；替换测试账号，保留该拒绝案例，不能据其宣称整链通过。
