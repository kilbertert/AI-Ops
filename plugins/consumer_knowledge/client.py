"""插件仅访问固定网关路径；目录凭据与本轮取数凭据分开使用。"""

import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def call(credentials, path, *, grant=None, body=None):
    base = credentials["gateway_url"].rstrip("/")
    parsed = urlsplit(base)
    if (
        (
            parsed.scheme != "https"
            and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"})
        )
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
    ):
        raise ValueError("网关地址必须为HTTPS根地址")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None

    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + (grant if grant is not None else credentials["catalogue_token"]),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            data = response.read(131073)
            if len(data) > 131072:
                raise ValueError("知识响应过大")
            return json.loads(data)
    except (OSError, ValueError) as exc:
        raise ValueError("知识目录或检索暂不可用，请核对工作区授权和发布状态") from exc
