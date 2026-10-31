"""发布门的模型白名单必须包含**已批准的客户端模型**。

现役两端本来就用不同模型（`docs/agents/operator-cpms/self-service.md`：客户端一个、
管家一个，不能互相覆盖），而平台 provider 注册表（`AIOPS_PROVIDERS`）只写管家那个。
2026-10-31 在 41 实测：页面开通客户端成功、草稿可见，点发布报 `model is not allowlisted`
—— profile 里明明已批准了 `consumer_models`。
"""

import json

from aiops_diagnostics.gateway import gateway_api
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings


def _settings_with_profile(tmp_path):
    profiles = tmp_path / "profiles.json"
    profiles.write_text(
        json.dumps(
            {
                "schema": "operator-profiles/1",
                "profiles": [
                    {
                        "id": "operator-41",
                        "name": "标准管家",
                        "model": {
                            "plugin": "model@x",
                            "provider": "langgenius/openai_api_compatible/openai_api_compatible",
                            "name": "operator-model",
                            "credential_file": str(tmp_path / "model.json"),
                        },
                        "consumer_models": [
                            {
                                "plugin": "model@x",
                                "provider": "langgenius/openai_api_compatible/openai_api_compatible",
                                "name": "consumer-model",
                                "credential_file": str(tmp_path / "model.json"),
                            }
                        ],
                    }
                ],
            }
        )
    )
    profiles.chmod(0o600)
    settings = GatewayServerSettings(
        data_home=tmp_path, database_file=tmp_path / "gateway.db", operator_profiles_file=str(profiles)
    )
    return settings


def test_consumer_models_of_the_profile_reach_the_publish_allowlist(tmp_path):
    settings = _settings_with_profile(tmp_path)
    # 直接测 create_gateway_app 内部用的那一段推导。
    allowed = gateway_api._publish_allowlist(("operator-model",), settings)
    assert allowed == ("operator-model", "consumer-model"), (
        "已批准的客户端模型没有进发布白名单：客户端草稿发布会报 model is not allowlisted"
    )


def test_without_profiles_the_allowlist_is_unchanged(tmp_path):
    settings = GatewayServerSettings(
        data_home=tmp_path, database_file=tmp_path / "gateway.db", operator_profiles_file=""
    )
    assert gateway_api._publish_allowlist(("aiops-api",), settings) == ("aiops-api",)
