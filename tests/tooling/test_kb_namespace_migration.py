"""显式迁移不能复制账户、覆盖他人或恢复已轮换凭据。"""

import ast
import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from aiops_diagnostics.knowledge.tenant_scope import knowledge_tenant

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "kb_namespace_migration", ROOT / "deploy/kb-service/migrate_tenant_namespace.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def asset(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "tenancy.db"
    with closing(sqlite3.connect(path)) as c:
        c.execute(
            "CREATE TABLE tenants (biz_tenant_id TEXT PRIMARY KEY, ragflow_email TEXT UNIQUE, "
            "ragflow_token TEXT, created_at INTEGER)"
        )
        c.execute("INSERT INTO tenants VALUES ('tenant', 'owner@kb.internal', 'original-secret', 1)")
        c.commit()
    path.chmod(0o600)
    return path, knowledge_tenant("tenant", "env-a"), tmp_path / "backup.json"


def row(path):
    with closing(sqlite3.connect(path)) as c:
        return c.execute("SELECT * FROM tenants").fetchall()


def test_readonly_migration_and_cas_restore_preserve_owner_and_token(asset):
    path, target, backup = asset
    before = row(path)
    assert module.migrate(path, "tenant", target, "owner@kb.internal") == {"state": "ready", "changed": False}
    assert row(path) == before and not backup.exists()
    assert module.migrate(path, "tenant", target, "owner@kb.internal", backup=backup)["state"] == "migrated"
    assert row(path) == [(target, *before[0][1:])]
    assert backup.stat().st_mode & 0o077 == 0
    assert (
        module.migrate(path, "tenant", target, "owner@kb.internal", backup=backup, restore=True)["state"]
        == "restored"
    )
    assert row(path) == before


def test_conflicting_target_and_backup_refuse_before_writing(asset):
    path, target, backup = asset
    with closing(sqlite3.connect(path)) as c:
        c.execute("INSERT INTO tenants VALUES (?, 'foreign@kb.internal', 'foreign-secret', 2)", (target,))
        c.commit()
    before = row(path)
    with pytest.raises(ValueError, match="目标命名空间"):
        module.migrate(path, "tenant", target, "owner@kb.internal", backup=backup)
    assert row(path) == before and not backup.exists()


def test_restore_never_revives_rotated_secret(asset):
    path, target, backup = asset
    module.migrate(path, "tenant", target, "owner@kb.internal", backup=backup)
    with closing(sqlite3.connect(path)) as c:
        c.execute("UPDATE tenants SET ragflow_token='rotated-secret'")
        c.commit()
    with pytest.raises(ValueError, match="漂移"):
        module.migrate(path, "tenant", target, "owner@kb.internal", backup=backup, restore=True)
    assert row(path)[0][0] == target and row(path)[0][2] == "rotated-secret"


def test_lookup_patch_returns_only_exact_registered_owner(asset):
    # 从交付补丁提取新函数，驱动函数本身而非只检查字符串。
    patch = (ROOT / "deploy/kb-service/scoped-account.patch").read_text()
    text = "\n".join(
        line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    tree = ast.parse(text)
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    func.decorator_list = []
    func.args.defaults = []
    func.args.args[0].annotation = None

    class Refused(Exception):
        pass

    from types import SimpleNamespace

    path, _, _ = asset
    namespace = {"tenancy": SimpleNamespace(DB_PATH=str(path)), "HTTPException": Refused}
    exec(compile(ast.Module(body=[func], type_ignores=[]), "lookup-patch", "exec"), namespace)
    before = row(path)
    assert namespace[func.name]("tenant") == {
        "data": {"biz_tenant_id": "tenant", "ragflow_email": "owner@kb.internal"}
    }
    with pytest.raises(Refused):
        namespace[func.name]("other")
    assert row(path) == before
    namespace["tenancy"].DB_PATH = str(path.parent / "missing.db")
    with pytest.raises(Refused):
        namespace[func.name]("tenant")
    assert not Path(namespace["tenancy"].DB_PATH).exists()


def test_same_tenant_in_other_environment_is_not_an_implicit_alias(asset):
    path, _, backup = asset
    with pytest.raises(ValueError, match="该租户"):
        module.migrate(path, "tenant", knowledge_tenant("other", "env-a"), "owner@kb.internal", backup=backup)
    assert row(path)[0][0] == "tenant" and not backup.exists()
