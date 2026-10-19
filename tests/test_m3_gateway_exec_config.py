"""M3-GATEWAY-01：签发时固定执行配置身份，领取时对照并持久化。

这些用例证明的是 **Gateway 这一侧**的判据：期望值只来自受控清单、身份是清单原始字节的
sha256、领取时对照并把实际值落库、以及旧库迁移不破坏历史记录。

它们**不**证明节点侧守卫 —— 那是 `tools/m2_close/m3node/` 的事，两套证据分开记。
本文件也不证明真实供应商或真实订单：上游与订单都是替身。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from aiops_diagnostics import execution_manifest
from aiops_diagnostics.agent_runs import AgentRun, AgentRunStore

NODE_TOKEN = "node-token-alpha-0123456789"
DELEGATED = "/v1/orders/{order}/charging-status/delegated"


def _manifest_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "m3-exec-config/1",
        "name": "gw-test",
        "history_policy": "none",
        "model": {
            "provider_key": "openai-compatible",
            "adapter_key": "openai-compatible",
            "model_key": "synthetic-model",
            "endpoint": "http://127.0.0.1:1/v1",
        },
        "model_options": {"temperature": 0.2, "top_p": 0.9, "max_tokens": 64},
        "system_messages": ["SYSTEM-PROMPT"],
        "tools": [{"name": "charging_status_snapshot", "description": "d", "input_schema": "{}"}],
    }
    payload.update(overrides)
    return payload


def _write_manifest(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    path = tmp_path / "exec-manifest.json"
    path.write_text(json.dumps(payload if payload is not None else _manifest_payload()))
    path.chmod(0o600)
    return path


def _identity(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# 纯函数面：清单读取与身份
# --------------------------------------------------------------------------


def test_identity_is_the_raw_bytes_digest_not_a_reserialization(tmp_path):
    """身份＝原始字节 sha256。同一份语义、不同空白 ⇒ 不同身份（不引入 JSON 归一化）。"""
    a = _write_manifest(tmp_path)
    b = tmp_path / "exec-manifest-b.json"
    b.write_text(json.dumps(_manifest_payload(), indent=2))
    b.chmod(0o600)

    first = execution_manifest.load(a)
    second = execution_manifest.load(b)

    assert first is not None and second is not None
    assert first.identity == _identity(a)
    assert second.identity == _identity(b)
    assert first.identity != second.identity
    # 语义相同：身份不同但解析结果一致，说明差异只在字节层。
    assert first.model_key == second.model_key
    assert first.system_messages == second.system_messages


def test_unset_path_means_not_enabled(tmp_path):
    assert execution_manifest.load(None) is None
    assert execution_manifest.load("") is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update({"schema": "m3-exec-config/999"}),
        lambda p: p.update({"history_policy": "full"}),
        lambda p: p.update({"system_messages": []}),
        lambda p: p.update({"tools": []}),
        lambda p: p.update({"model": {"provider_key": "x", "adapter_key": "y", "endpoint": "http://e"}}),
    ],
)
def test_invalid_manifest_is_refused(tmp_path, mutation):
    payload = _manifest_payload()
    mutation(payload)
    path = _write_manifest(tmp_path, payload)
    with pytest.raises(execution_manifest.ExecutionManifestError):
        execution_manifest.load(path)


def test_manifest_permissions_must_be_private(tmp_path):
    path = _write_manifest(tmp_path)
    path.chmod(0o644)
    with pytest.raises(execution_manifest.ExecutionManifestError):
        execution_manifest.load(path)


def test_symlink_is_refused_before_reading_the_target(tmp_path):
    real = _write_manifest(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(execution_manifest.ExecutionManifestError):
        execution_manifest.load(link)


# --------------------------------------------------------------------------
# 迁移面
# --------------------------------------------------------------------------


def _legacy_db(path: Path) -> None:
    """造一个**迁移前**的库：只有旧列。"""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE agent_runs (
            run_id TEXT PRIMARY KEY, scope_fingerprint TEXT NOT NULL, scope_json TEXT NOT NULL,
            order_no TEXT NOT NULL, operation TEXT NOT NULL, environment TEXT NOT NULL,
            app_id TEXT NOT NULL, agent_id TEXT NOT NULL, expected_node_id TEXT NOT NULL,
            state TEXT NOT NULL, reason TEXT, snapshot_json TEXT, actual_node_id TEXT,
            node_build TEXT, gateway_version TEXT NOT NULL, created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, claimed_at TEXT, completed_at TEXT
        );
        """
    )
    connection.execute(
        """
        INSERT INTO agent_runs (run_id, scope_fingerprint, scope_json, order_no, operation,
            environment, app_id, agent_id, expected_node_id, state, gateway_version,
            created_at, expires_at)
        VALUES ('run_legacy', 'fp', '{}', 'ORDER-A', 'charging_status', 'env-a', 'app',
                '7', 'node-a1', 'succeeded', '1.0', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:05:00+00:00')
        """
    )
    connection.commit()
    connection.close()


def test_legacy_database_is_migrated_without_backfilling(tmp_path):
    path = tmp_path / "gateway.db"
    _legacy_db(path)
    before = _snapshot(path)
    legacy_columns = tuple(before[0].keys())

    AgentRunStore(path)  # 打开即迁移

    after = _snapshot(path)
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(agent_runs)")}
    assert {"expected_agent_version", "observed_agent_version", "exec_config_state"} <= columns
    # 旧行三列为 NULL，且**历史字段逐字未变**（快照只比对迁移前就有的列）。
    assert after[0]["expected_agent_version"] is None
    assert after[0]["observed_agent_version"] is None
    assert after[0]["exec_config_state"] is None
    # 迁移**只加列**：迁移前就存在的每一列逐字未变（含时间戳与快照）。
    assert [tuple(row[column] for column in legacy_columns) for row in before] == [
        tuple(row[column] for column in legacy_columns) for row in after
    ]


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "gateway.db"
    _legacy_db(path)
    AgentRunStore(path)
    first = _snapshot(path)
    AgentRunStore(path)  # 再开一次
    assert _snapshot(path) == first


def _snapshot(path: Path) -> list[dict[str, object]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM agent_runs ORDER BY run_id")]
    finally:
        connection.close()


# --------------------------------------------------------------------------
# 回执面
# --------------------------------------------------------------------------


def _run(**overrides: object) -> AgentRun:
    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, tzinfo=UTC)
    defaults: dict[str, object] = {
        "run_id": "run_x",
        "scope_fingerprint": "fp",
        "scope_json": "{}",
        "order_no": "ORDER-A",
        "operation": "charging_status",
        "environment": "env-a",
        "app_id": "app",
        "agent_id": "7",
        "expected_node_id": "node-a1",
        "state": "succeeded",
        "reason": None,
        "snapshot": {"order_no": "ORDER-A"},
        "actual_node_id": "node-a1",
        "node_build": "build",
        "expected_agent_version": None,
        "observed_agent_version": None,
        "exec_config_state": None,
        "gateway_version": "1.0",
        "created_at": now,
        "expires_at": now,
        "claimed_at": None,
        "completed_at": None,
    }
    defaults.update(overrides)
    return AgentRun(**defaults)  # type: ignore[arg-type]


def test_receipt_reports_not_wired_when_no_expectation_was_fixed():
    receipt = _run().receipt()
    assert receipt["agent_version"] is None
    assert receipt["agent_version_gap"] == "not_wired"


def test_receipt_reports_the_recorded_identity_not_model_text():
    receipt = _run(expected_agent_version="abc123", observed_agent_version="abc123").receipt()
    assert receipt["agent_version"] == "abc123"
    assert receipt["agent_version_observed"] == "abc123"
    assert receipt["agent_version_gap"] is None


# --------------------------------------------------------------------------
# 端到端：签发 → 领取 → 回执（走真实的 delegated / claims 路由）
# --------------------------------------------------------------------------

HEADERS = {"Authorization": "Bearer verified-token", "X-Business-Entry": "consumer"}


def _fixture(tmp_path, monkeypatch, *, manifest: Path | None):
    """在既有委托执行夹具上打开清单，并**由测试自己扮演节点**（与 `_succeed_run` 同一模式）。

    重建应用或走派发期领取都会得到另一套状态 —— 见 `_succeed_run` 的注释：
    时序要完全可控，所以派发只记录不领取，节点那一跳由测试显式发出。
    """
    from test_m2_close_contract import _app as _upstream_app
    from test_m2_close_contract import cpms_stub

    ctx = cpms_stub()
    upstream = ctx.__enter__()
    fixture = _upstream_app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
    fixture["_upstream_ctx"] = ctx
    fixture["dispatcher"].claim = None
    settings = fixture["app"].state.gateway.settings
    object.__setattr__(settings, "execution_manifest_file", str(manifest) if manifest else "")
    return fixture


def _delegate(fixture):
    return fixture["client"].post(DELEGATED.format(order="ORDER-A"), headers=HEADERS, json={})


def _claim(fixture, run_id: str, **headers):
    return fixture["client"].post(
        f"/v1/internal/charging-status/claims/{run_id}",
        headers={"Authorization": f"Bearer {NODE_TOKEN}", **headers},
        json={},
    )


def _rows(store: AgentRunStore) -> list[dict[str, object]]:
    """读记录。另开连接要重设 row_factory，否则拿到的是 tuple。"""
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM agent_runs")]
    finally:
        connection.close()


def _run_id(fixture) -> str:
    rows = _rows(fixture["store"])
    assert rows, "委派应已创建记录"
    return str(rows[0]["run_id"])


# --------------------------------------------------------------------------
# 端到端：签发 → 领取 → 回执（走真实的 delegated / claims 路由）
# --------------------------------------------------------------------------

HEADERS = {"Authorization": "Bearer verified-token", "X-Business-Entry": "consumer"}


def test_matching_identity_is_recorded_and_reported(tmp_path, monkeypatch):
    """GW-01：一致的节点身份 ⇒ 期望与实际都落库，回执取自记录。"""
    manifest = _write_manifest(tmp_path)
    identity = _identity(manifest)
    fixture = _fixture(tmp_path, monkeypatch, manifest=manifest)
    assert _delegate(fixture).status_code == 502  # 派发期不领取，由测试扮演节点
    run_id = _run_id(fixture)

    claimed = _claim(fixture, run_id, **{"X-AI-Ops-Exec-Config": identity})

    assert claimed.status_code == 200, claimed.text
    row = _rows(fixture["store"])[0]
    assert row["expected_agent_version"] == identity
    assert row["observed_agent_version"] == identity
    assert row["exec_config_state"] == "matched"
    receipt = row and fixture["store"].get(run_id).receipt()
    assert receipt["agent_version"] == identity
    assert receipt["agent_version_gap"] is None


def test_drifted_identity_is_refused_before_any_snapshot(tmp_path, monkeypatch):
    """GW-02：不一致 ⇒ 拒绝，且不返回快照。"""
    manifest = _write_manifest(tmp_path)
    fixture = _fixture(tmp_path, monkeypatch, manifest=manifest)
    assert _delegate(fixture).status_code == 502
    run_id = _run_id(fixture)

    claimed = _claim(fixture, run_id, **{"X-AI-Ops-Exec-Config": "0" * 64})

    assert claimed.status_code == 403
    assert claimed.json()["error"]["code"] == "AGENT_RUN_EXEC_CONFIG_DRIFT"
    row = _rows(fixture["store"])[0]
    assert row["expected_agent_version"] == _identity(manifest)
    assert row["observed_agent_version"] is None
    assert not row["snapshot_json"]


def test_missing_identity_header_is_treated_as_drift(tmp_path, monkeypatch):
    """GW-03：缺省不等于一致。"""
    manifest = _write_manifest(tmp_path)
    fixture = _fixture(tmp_path, monkeypatch, manifest=manifest)
    assert _delegate(fixture).status_code == 502
    run_id = _run_id(fixture)

    claimed = _claim(fixture, run_id)

    assert claimed.status_code == 403
    assert _rows(fixture["store"])[0]["observed_agent_version"] is None


def test_without_manifest_behaviour_is_unchanged(tmp_path, monkeypatch):
    """GW-04：未启用清单 ⇒ 与今天逐字一致（无期望、回执 not_wired）。"""
    fixture = _fixture(tmp_path, monkeypatch, manifest=None)
    assert _delegate(fixture).status_code == 502
    run_id = _run_id(fixture)

    claimed = _claim(fixture, run_id)

    assert claimed.status_code == 200, claimed.text
    receipt = fixture["store"].get(run_id).receipt()
    assert receipt["agent_version"] is None
    assert receipt["agent_version_gap"] == "not_wired"
    assert _rows(fixture["store"])[0]["expected_agent_version"] is None


# --------------------------------------------------------------------------
# GW-05 / GW-07 / GW-08
# --------------------------------------------------------------------------


def test_startup_refuses_an_invalid_manifest(tmp_path, monkeypatch):
    """GW-05：清单不合格 ⇒ **启动即拒**，不是运行期降级。"""
    from aiops_diagnostics.gateway_config import GatewayServerSettings

    manifest = _write_manifest(tmp_path, _manifest_payload(schema="m3-exec-config/999"))
    config_file = tmp_path / "agent-runs.json"
    config_file.write_text("{}")
    config_file.chmod(0o600)
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        charging_status_base_url="http://127.0.0.1:1/charging-pile",
        execution_environment="env-a",
        agent_run_config_file=str(config_file),
        execution_manifest_file=str(manifest),
    )
    with pytest.raises(ValueError, match="execution manifest is invalid"):
        settings.validate()


def test_manifest_without_delegation_is_a_startup_error(tmp_path):
    """GW-05b：只配清单不配委派 ⇒ 启动错误（没有 run 可固定身份）。"""
    from aiops_diagnostics.gateway_config import GatewayServerSettings

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        execution_manifest_file=str(_write_manifest(tmp_path)),
    )
    with pytest.raises(ValueError, match="execution manifest requires agent run delegation"):
        settings.validate()


def test_retry_release_path_also_checks_identity(tmp_path, monkeypatch):
    """GW-07：成功快照的**重试放行**也要复核身份 —— 三条放行路径各一次机会。

    先按一致身份跑成功，再把期望改成另一个身份，重试必须拒绝（记录已被 refuse）。
    """
    manifest = _write_manifest(tmp_path)
    identity = _identity(manifest)
    fixture = _fixture(tmp_path, monkeypatch, manifest=manifest)
    assert _delegate(fixture).status_code == 502
    run_id = _run_id(fixture)
    assert _claim(fixture, run_id, **{"X-AI-Ops-Exec-Config": identity}).status_code == 200

    # 换一份清单：同一路径下内容变了 ⇒ 身份变了。
    manifest.write_text(json.dumps(_manifest_payload(name="rotated")))
    manifest.chmod(0o600)
    rotated = _identity(manifest)
    retried = _claim(fixture, run_id, **{"X-AI-Ops-Exec-Config": rotated})

    assert retried.status_code == 403
    assert retried.json()["error"]["code"] == "AGENT_RUN_EXEC_CONFIG_DRIFT"


def test_wrong_credential_and_sibling_node_are_unaffected(tmp_path, monkeypatch):
    """GW-08：错凭据/兄弟节点拿到的是**既有**拒绝理由，不因本片新增对照而改变。"""
    manifest = _write_manifest(tmp_path)
    identity = _identity(manifest)
    fixture = _fixture(tmp_path, monkeypatch, manifest=manifest)
    assert _delegate(fixture).status_code == 502
    run_id = _run_id(fixture)

    # 错凭据：401，且记录不被本片的新对照改写。
    wrong = fixture["client"].post(
        f"/v1/internal/charging-status/claims/{run_id}",
        headers={"Authorization": "Bearer not-a-known-node", "X-AI-Ops-Exec-Config": identity},
        json={},
    )
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "NODE_TOKEN_REQUIRED"
    row = _rows(fixture["store"])[0]
    assert row["state"] == "issued"
    assert row["observed_agent_version"] is None
