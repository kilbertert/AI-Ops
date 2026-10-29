import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "recover_tenant", Path(__file__).parents[2] / "deploy/kb-service/recover_tenant_login.py"
)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def test_recovery_backs_up_before_changing_and_keeps_identity(tmp_path):
    user = SimpleNamespace(
        id="original", password="old-hash", access_token="existing-session", is_superuser=False
    )
    backup = tmp_path / "backup"

    def update(user_id, password):
        assert json.loads(backup.read_text())["password"] == "old-hash"
        # RAGFlow 登录比对的是 base64(明文)，不是明文本身；存明文散列会让登录永远失败。
        assert user_id == "original" and password == base64.b64encode(b"new-secret" * 3).decode()
        user.password = "new-hash"

    service = SimpleNamespace(query_user_by_email=lambda _: [user], update_user_password=update)
    model = SimpleNamespace(get_by_id=lambda _: user)
    result = recovery.recover(
        user_service=service, user_model=model, tenant_id="TENANT", password="new-secret" * 3, backup=backup
    )
    assert result["user_id"] == "original" and "password" not in result
    with pytest.raises(FileExistsError):
        recovery.recover(
            user_service=service,
            user_model=model,
            tenant_id="TENANT",
            password="new-secret" * 3,
            backup=backup,
        )


def test_recovery_refuses_when_written_hash_cannot_pass_login(tmp_path):
    user = SimpleNamespace(id="u", password="old", access_token="s", is_superuser=False)
    service = SimpleNamespace(query_user_by_email=lambda _: [user], update_user_password=lambda *_: None)
    model = SimpleNamespace(get_by_id=lambda _: user)
    with pytest.raises(ValueError, match="does not satisfy the login check"):
        recovery.recover(
            user_service=service,
            user_model=model,
            tenant_id="TENANT",
            password="new-secret" * 3,
            backup=tmp_path / "backup",
            verify=lambda *_: False,
        )


def test_recovery_refuses_platform_account(tmp_path):
    service = SimpleNamespace(query_user_by_email=lambda _: [SimpleNamespace(is_superuser=True)])
    with pytest.raises(ValueError, match="non-platform"):
        recovery.recover(
            user_service=service,
            user_model=None,
            tenant_id="TENANT",
            password="new-secret" * 3,
            backup=tmp_path / "backup",
        )
    assert not (tmp_path / "backup").exists()
