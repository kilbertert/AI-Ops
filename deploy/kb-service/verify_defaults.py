"""对指定 kb_adapter.py 执行开户契约自检，不联网，不创建租户。"""

import importlib.util
import sys
from unittest.mock import patch


def verify(path):
    spec = importlib.util.spec_from_file_location("kb_adapter_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = module.RagflowClient("fixture")
    calls = []
    models = [
        {
            "name": name,
            "model_type": [kind],
            "model_id": kind,
            "provider_name": "OpenAI",
            "instance_name": "baoyun",
        }
        for name, kind in [
            ("text-embedding-3-large", "embedding"),
            ("deepseek-v4-flash", "chat"),
            ("gpt-4o-mini", "vision"),
        ]
    ]
    models.append({**models[-1], "model_id": "wrong-instance", "instance_name": "unrelated"})

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        if path.endswith("/instances"):
            return [{"id": "own-instance", "instance_name": name} for name in ("baoyun", "maas")]
        if path == "/models":
            return models
        return None

    client._req = request
    with patch.object(module.requests, "post", side_effect=AssertionError("自检禁止联网")):
        report = client.setup_default_models(api_key="fixture", base_url="https://provider.invalid/v1")
    assert report["factory"] == "OpenAI" and report["instance"] == "baoyun"
    assert report["defaults"] == {k: k for k in ("embedding", "chat", "vision")}
    body = next(body for method, path, body in calls if path.endswith("own-instance"))
    assert any(
        m["model_name"] == "gpt-4o-mini" and m["model_type"] == ["image2text"] for m in body["model_info"]
    )
    client.create_kb("fixture")
    assert calls[-1][2]["embedding_model"] == "text-embedding-3-large@baoyun@OpenAI"
    models[:] = models[:2]
    try:
        client.setup_default_models(api_key="fixture", base_url="https://provider.invalid/v1")
    except RuntimeError as error:
        assert "incomplete" in str(error)
    else:
        raise AssertionError("缺少视觉模型时不能报告开户配置完整")
    print("开户默认值、实例选择、缺模型拒绝：通过")


if __name__ == "__main__":
    verify(sys.argv[1])
