"""外部知识API连接属于Dify工作区；仅平台端点和密钥可以作为创建模板。"""

import hmac

from .dify_console_admin import ConsoleApiError

PATH = "/console/api/datasets/external-knowledge-api"


def ensure(console, workspace_id, settings, *, pending=None, save=None, name=""):
    endpoint = settings.public_base_url.rstrip("/") + "/v1/dify"
    name = name or "aiops-" + settings.environment + "-knowledge"
    if settings.dify_external_knowledge_api_id:
        try:
            row = console._call(workspace_id, PATH + "/" + settings.dify_external_knowledge_api_id)
        except ConsoleApiError as exc:
            if exc.status not in {400, 404}:
                raise
        else:
            if (
                row.get("tenant_id") != workspace_id
                or (row.get("settings") or {}).get("endpoint") != endpoint
            ):
                raise ValueError("knowledge connection identity mismatch")
            if pending is not None and not hmac.compare_digest(
                str(row.get("settings", {}).get("api_key", "")), settings.dify_knowledge_api_key
            ):
                raise ValueError("knowledge connection credential mismatch")
            return row["id"]
    rows = []
    for page in range(1, 51):
        body = console._call(workspace_id, f"{PATH}?page={page}&limit=100")
        rows.extend(body["data"])
        if not body.get("has_more"):
            break
    else:
        raise ValueError("knowledge connection listing limit reached")
    matching = [r for r in rows if r["id"] == settings.dify_external_knowledge_api_id or r["name"] == name]
    if len(matching) > 1:
        raise ValueError("ambiguous managed knowledge connection")
    if matching:
        row = matching[0]
        if pending is not None:
            row = console._call(workspace_id, PATH + "/" + row["id"])
        if row.get("tenant_id") != workspace_id or (row.get("settings") or {}).get("endpoint") != endpoint:
            raise ValueError("knowledge connection identity mismatch")
        if pending is not None and not hmac.compare_digest(
            str(row.get("settings", {}).get("api_key", "")), settings.dify_knowledge_api_key
        ):
            raise ValueError("knowledge connection credential mismatch")
        return row["id"]
    if not settings.dify_knowledge_api_key:
        raise ValueError("knowledge connection credential missing")
    if pending is not None:
        if pending.get("attempted"):
            raise ConsoleApiError("DIFY_KNOWLEDGE_CONNECTION_UNCONFIRMED", "连接创建未确认，继续对账")
        pending["attempted"] = True
        save()
    result = console._call(
        workspace_id,
        PATH,
        method="POST",
        body={
            "name": name,
            "settings": {"endpoint": endpoint, "api_key": settings.dify_knowledge_api_key},
        },
    )
    if result.get("tenant_id") != workspace_id or not result.get("id"):
        raise ConsoleApiError("DIFY_KNOWLEDGE_CONNECTION_INVALID", "创建知识连接未返回当前工作区身份")
    return result["id"]
