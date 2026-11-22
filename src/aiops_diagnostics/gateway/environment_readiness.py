"""正式发布前的目标事实检查；正常身份与当前安装，不接表单passed。"""

import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiops_diagnostics.consumer.candidate_trial import consumer_identity
from aiops_diagnostics.gateway import environment_business as business
from aiops_diagnostics.gateway import environment_trial
from aiops_diagnostics.gateway.environment_registry import load


def resource_sample(root):
    values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return {
        "available_memory_bytes": int(values["MemAvailable"].split()[0]) * 1024,
        "available_disk_bytes": shutil.disk_usage(root).free,
        "observed_at": time.time(),
    }


def check(context, spec):
    business.installation(context, spec)
    settings = context.settings
    cases = {cap: business.load_case(context, spec, cap) for cap in spec["capabilities"]}
    if set(cases) != {"consumer", "order", "offline"}:
        raise ValueError("all selected initial business evidence required")
    case = cases["consumer"][0]
    consumer = consumer_identity(context, case["consumer_session"])
    operator = environment_trial.identity(context, cases["order"][0]["operator_token"])
    if (
        consumer.effective_tenant_id != spec["tenant_id"]
        or operator.effective_tenant_id != spec["tenant_id"]
        or consumer.delegated
        or consumer.data_scope.type != "self"
        or not consumer.subject.c_user_id
    ):
        raise ValueError("current normal identities do not match target tenant")
    context.runtime._require_chat_order_visible(consumer, case["cases"]["consumer"]["order_no"])
    sample = resource_sample(settings.data_home)
    if min(sample["available_memory_bytes"], sample["available_disk_bytes"]) < 1024**3:
        raise ValueError("target resource headroom insufficient")
    directory = load(settings.environment_registry_file)
    row = next(r for r in directory["environments"] if r["id"] == spec["environment"])
    urls = [row["gateway_url"], row["company_gateway_url"], row["cpms_base_url"], settings.dify_chat_base_url]
    if any(urlsplit(url).scheme != "https" for url in urls):
        raise ValueError("formal endpoints require TLS")
    from aiops_diagnostics.consumer.consumer_knowledge_selection import catalogue_token

    root = Path(f"/etc/aiops-access-{settings.environment}/runtime")
    import json

    from aiops_diagnostics.execution.node_publication import business_prompt

    manifest = (root / "operator-manifest.json").read_text()
    return {
        "operator_manifest": manifest,
        "operator_prompt": business_prompt(json.loads(manifest)["system_messages"][0]),
        "environment": spec["environment"],
        "tenant_id": spec["tenant_id"],
        "installed": spec["installed"],
        "resources": sample,
        "registry_revision": directory["revision"],
        "operator_scope": operator.scope_fingerprint,
        "consumer_scope": consumer.scope_fingerprint,
        "catalogue_token": catalogue_token(
            settings.internal_token_secret, settings.environment, spec["consumer"]["workspace_id"]
        ),
        "gateway_url": row["gateway_url"],
        "dify_chat_base_url": settings.dify_chat_base_url,
    }
