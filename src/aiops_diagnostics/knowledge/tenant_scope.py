"""共享知识服务的租户命名空间；公司租户身份保持不变。"""

import json
from base64 import urlsafe_b64encode

from aiops_diagnostics.gateway.deployment_environment import canonical_environment


def knowledge_tenant(tenant_id: str, environment: str = "") -> str:
    if not environment:
        return tenant_id
    value = json.dumps(
        [canonical_environment(environment), tenant_id], ensure_ascii=False, separators=(",", ":")
    )
    return "env_" + urlsafe_b64encode(value.encode()).decode().rstrip("=")


def namespace_for(settings) -> str:
    return (
        canonical_environment(settings.environment)
        if getattr(settings, "environment_registry_file", "")
        else ""
    )
