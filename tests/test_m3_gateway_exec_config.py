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
from datetime import timedelta
from pathlib import Path

import pytest

from aiops_diagnostics import agent_runs, execution_manifest
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
        lambda p: p["tools"][0].update({"name": " \t "}),
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
    receipt = _run(
        expected_agent_version="abc123",
        observed_agent_version="abc123",
        exec_config_state="matched",
    ).receipt()
    assert receipt["agent_version"] == "abc123"
    assert receipt["agent_version_observed"] == "abc123"
    assert receipt["agent_version_gap"] is None


def test_fixed_expectation_without_confirmation_is_a_gap_not_a_version():
    """**已固定期望 ≠ 已核对成功**。存在 expected 就把版本缺口清空，是把未核实写成已核实。

    这条对应 Codex 复现的 failed receipt：一条被 `refuse` 的记录仍有 expected，
    回执却报出该摘要且 `gap=None`，读起来像「跑在这个版本上」。
    """
    for state in (None, "expected", "mismatched"):
        receipt = _run(
            state="failed",
            reason="exec_config_drift",
            snapshot=None,
            expected_agent_version="a" * 64,
            observed_agent_version=None,
            exec_config_state=state,
        ).receipt()
        assert receipt["agent_version"] is None, state
        assert receipt["agent_version_gap"] == "not_confirmed", state


def test_mismatched_observation_never_reports_a_version_even_if_a_value_is_stored():
    receipt = _run(
        state="failed",
        reason="exec_config_drift",
        snapshot=None,
        expected_agent_version="a" * 64,
        observed_agent_version="b" * 64,
        exec_config_state="mismatched",
    ).receipt()
    assert receipt["agent_version"] is None
    assert receipt["agent_version_gap"] == "not_confirmed"
    assert receipt["agent_version_observed"] == "b" * 64


def test_disabled_receipt_keeps_exactly_the_legacy_key_set():
    """未启用清单：回执字段集合与今天**逐字相同** —— 一个键都不多。

    比的是键集合相等，不是「看起来没变」。上一版在这里多出了
    `agent_version_observed`，于是「行为与今天逐字一致」是句没有判据的话。
    """
    legacy = {
        "run_id",
        "state",
        "reason",
        "operation",
        "scope_fingerprint",
        "expected_environment",
        "expected_app_id",
        "expected_agent_id",
        "expected_node_id",
        "actual_node_id",
        "node_build",
        "gateway_version",
        "agent_version",
        "agent_version_gap",
        "result_sha256",
        "created_at",
        "expires_at",
        "claimed_at",
        "completed_at",
    }
    assert set(_run().receipt()) == legacy


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
    """GW-02：不一致 ⇒ 拒绝、不返回快照，且记录**真正落终态**。

    `observed_agent_version` 在这里**要**落库：漂移时留下实际收到的摘要，是审计要求的
    「漂移时实际值怎么留证」。早先这里断言 `None`，等于要求「拒绝之后什么都不留」，
    与「按契约留证」相反。
    """
    manifest = _write_manifest(tmp_path)
    fixture = _fixture(tmp_path, monkeypatch, manifest=manifest)
    assert _delegate(fixture).status_code == 502
    run_id = _run_id(fixture)

    claimed = _claim(fixture, run_id, **{"X-AI-Ops-Exec-Config": "0" * 64})

    assert claimed.status_code == 403
    assert claimed.json()["error"]["code"] == "AGENT_RUN_EXEC_CONFIG_DRIFT"
    assert "order_no" not in claimed.json()
    row = _rows(fixture["store"])[0]
    assert row["expected_agent_version"] == _identity(manifest)
    assert row["observed_agent_version"] == "0" * 64
    assert row["exec_config_state"] == "mismatched"
    assert row["state"] == "failed"
    assert row["reason"] == "exec_config_drift"
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


# --------------------------------------------------------------------------
# GW-11：漂移必须落终态（三个非终态起点都要覆盖）
# --------------------------------------------------------------------------


def _claim_with(fixture, run_id: str, observed: str | None):
    headers = {} if observed is None else {"X-AI-Ops-Exec-Config": observed}
    return _claim(fixture, run_id, **headers)


@pytest.mark.parametrize("start_state", ["issued", "running", "succeeded"])
def test_drift_is_terminal_from_every_live_state(tmp_path, monkeypatch, start_state):
    """**发的三种状态**漂移都必须钉成 ``failed``，而不是只有已验证成功的那种。

    早先这里只调 ``refuse()``，而它只认 ``succeeded``：于是 ``issued``/``running``
    的记录在拒绝之后仍然活着，换正确摘要再来一次就 200 —— 复现材料里的
    ``DRIFT_THEN_RETRY 403 after_drift=issued reason=None retry=200 final=succeeded``。

    同时证明**终态不可复活**：错摘要被拒后，再拿正确摘要重试仍 403，
    库里的 state/reason 不被覆盖，业务调用计数不变。
    """
    manifest = _write_manifest(tmp_path)
    identity = _identity(manifest)
    fixture = _fixture(tmp_path, monkeypatch, manifest=manifest)
    assert _delegate(fixture).status_code == 502
    run_id = _run_id(fixture)

    if start_state in {"running", "succeeded"}:
        assert fixture["store"].claim(run_id) is True
    if start_state == "succeeded":
        assert (
            fixture["store"].complete(
                run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b"
            )
            is True
        )
    calls_before = len(fixture["orders"].calls)

    drifted = _claim_with(fixture, run_id, "0" * 64)
    assert drifted.status_code == 403
    assert drifted.json()["error"]["code"] == "AGENT_RUN_EXEC_CONFIG_DRIFT"
    assert "order_no" not in drifted.json()

    row = _rows(fixture["store"])[0]
    assert row["state"] == "failed", start_state
    assert row["reason"] == "exec_config_drift", start_state
    assert row["exec_config_state"] == "mismatched", start_state
    assert row["observed_agent_version"] == "0" * 64, start_state

    # 换正确摘要仍拒绝：终态不复活。
    retried = _claim_with(fixture, run_id, identity)
    assert retried.status_code == 403, start_state
    assert retried.json()["error"]["code"] == "AGENT_RUN_NOT_ACTIVE", start_state
    after = _rows(fixture["store"])[0]
    assert after == row, start_state
    assert len(fixture["orders"].calls) == calls_before, start_state


def test_drift_does_not_resurrect_an_already_terminal_record(tmp_path, monkeypatch):
    """已有 failed/revoked 不被漂移路径改写原因，也不复活。"""
    manifest = _write_manifest(tmp_path)
    identity = _identity(manifest)
    for terminal, reason in (("failed", "upstream_failed"), ("revoked", "revoked_by_caller")):
        case_dir = tmp_path / terminal
        case_dir.mkdir()
        fixture = _fixture(case_dir, monkeypatch, manifest=manifest)
        assert _delegate(fixture).status_code == 502
        run_id = _run_id(fixture)
        assert fixture["store"].claim(run_id) is True
        if terminal == "failed":
            assert fixture["store"].fail(run_id, reason) is True
        else:
            assert fixture["store"].revoke(run_id, reason) is True
        before = _rows(fixture["store"])[0]

        drifted = _claim_with(fixture, run_id, "0" * 64)
        assert drifted.status_code == 403
        # 正确摘要也不行 —— 终态就是终态。
        assert _claim_with(fixture, run_id, identity).status_code == 403
        assert _rows(fixture["store"])[0] == before, terminal


def _prepare_claimed_with_snapshot(fixture):
    """**启用清单**下「已取数、未完成」的一条记录：``running`` + 快照。

    名字如实反映状态。早先它叫 `_prepare_succeeded`，但在本片之后，
    启用清单时 `complete()` 只写快照、**保持 running** —— 叫 succeeded 会让读者以为
    这条路径覆盖的是「成功记录的重试」，而实际覆盖的是「running+快照的放行」。
    """
    run_id = _run_id(fixture)
    assert fixture["store"].claim(run_id) is True
    assert (
        fixture["store"].complete(
            run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b"
        )
        is True
    )
    return run_id


def _prepare_succeeded(fixture):
    """**真正的** succeeded 记录：领取 → 快照 → 整轮确认。

    用于覆盖「成功记录的重试放行」这条路径本身。未启用清单时 `complete()` 即 succeeded，
    启用清单时必须再走一次 `complete_loop()`。
    """
    run_id = _prepare_claimed_with_snapshot(fixture)
    identity = _rows(fixture["store"])[0]["expected_agent_version"]
    if identity is not None:
        verdict, settled = fixture["store"].complete_loop(run_id, observed_agent_version=identity)
        assert verdict == "completed", verdict
    assert _rows(fixture["store"])[0]["state"] == "succeeded"
    return run_id


def _drive_to_release_path(fixture, release_path: str) -> str:
    """把记录推到某条**放行路径**的起点，并返回目标 run_id。

    ``race_release`` 的关键是：记录仍是 ``issued``、本请求的 ``claim()`` **真的**输给
    另一个领取者，另一个领取者随后把这条记录跑成功。早先这里先把记录置成 ``succeeded``，
    于是路由直接走「成功快照重试」分支，``claim()`` 一次都没被调用 —— 「覆盖了三条路径」
    只是参数名在宣称，``monkeypatch.setattr(store, "claim", lambda _: False)`` 也从未生效。
    """
    run_id = _run_id(fixture)
    if release_path == "first_claim":
        return run_id
    if release_path == "retry_release":
        # 这条路径的名字说的是「成功快照的重试」，所以起点必须是**真的** succeeded。
        return _prepare_succeeded(fixture)
    # race_release：记录**必须仍是 issued**。竞争发生在 claim() 那一瞬间（见下面的
    # `lose_claim_to_other_request`）：本请求读到的还是 issued，要 claim 时另一个领取者
    # 已经领走并跑成功。事先把它置成 succeeded 会让路由直接走「成功快照重试」，
    # claim() 一次都不会被调用 —— 那正是上一版用例宣称覆盖、实际没覆盖的路径。
    return run_id


def _install_final_gate_probe(fixture, monkeypatch, *, event, calls, before=None):
    """在**真正的最终核对点**（第二次 `confirm_exec_config`）注入一次事件。

    为什么是「第二次」：领取路径上有两次核对 —— 领取前的预检与放行前的终检。
    只挂「下一次」会落在预检里，测到的不是最终放行。

    ``before`` 会被填上**最终事务执行前**那一刻的 `exec_config_state`：之后要用它证明
    终态没有被最终事务改写。在请求之外采这一份是不对的 —— 那时预检还没发生。
    """
    store = fixture["app"].state.gateway.agent_run_store
    original = store.confirm_exec_config
    expires = fixture["store"].get(_run_id(fixture)).expires_at

    def at_final_gate(run_id_, observed, **kwargs):
        calls.append(run_id_)
        if len(calls) == 2:
            if before is not None:
                before.append(_rows(fixture["store"])[0]["exec_config_state"])
            if event == "refuse":
                # 另一个已认证请求把记录拒绝成终态。
                if not fixture["store"].fail(run_id_, "exec_config_drift"):
                    fixture["store"].refuse(run_id_, "exec_config_drift")
            else:
                # 让时间在最终事务之前越过到期点。
                monkeypatch.setattr(agent_runs, "_now", lambda: expires + timedelta(seconds=1))
        return original(run_id_, observed, **kwargs)

    monkeypatch.setattr(store, "confirm_exec_config", at_final_gate)
    return expires


@pytest.mark.parametrize("release_path", ["first_claim", "retry_release", "race_release"])
@pytest.mark.parametrize("event", ["refuse", "expires"])
def test_final_gate_blocks_refused_or_expired_records(tmp_path, monkeypatch, release_path, event):
    """**本请求的最终核对之前**，别的请求拒绝、或时间越过到期点 ⇒ 不返回快照。

    复现材料里这两件事都返回了 ``200 + ORDER-A``，而记录已经是 ``failed``/``succeeded``。
    三条放行路径各跑一遍，并断言：
    - 核对确实发生了**两次**（预检 + 终检），且我们注入的是第二次；
    - ``race_release`` 的 ``claim()`` 确实被调用并输掉（真实竞争，不是参数名）。
    """
    manifest = _write_manifest(tmp_path)
    identity = _identity(manifest)
    fixture = _fixture(tmp_path, monkeypatch, manifest=manifest)
    assert _delegate(fixture).status_code == 502
    run_id = _drive_to_release_path(fixture, release_path)

    store = fixture["app"].state.gateway.agent_run_store
    calls: list[str] = []
    competitions: list[str] = []
    if release_path == "race_release":
        # 真实竞争：本请求的 claim() 里，另一个领取者先赢并跑成功，然后本请求输掉。
        # 记录在这之前仍是 issued，所以路由确实走进 claim 竞争分支。
        original_claim = store.claim

        def lose_claim_to_other_request(rid):
            competitions.append(rid)
            assert original_claim(rid) is True  # 另一个领取者赢
            assert (
                fixture["store"].complete(
                    rid,
                    snapshot={"order_no": "ORDER-A"},
                    actual_node_id="node-a1",
                    node_build="other",
                )
                is True
            )
            return False  # 本请求输掉

        monkeypatch.setattr(store, "claim", lose_claim_to_other_request)

    before: list[object] = []
    _install_final_gate_probe(fixture, monkeypatch, event=event, calls=calls, before=before)

    response = _claim_with(fixture, run_id, identity)

    assert len(calls) == 2, f"{release_path}/{event}: 必须走到最终核对（预检+终检）"
    assert len(competitions) == (1 if release_path == "race_release" else 0)
    assert response.status_code == 403, response.text
    assert "order_no" not in response.json(), "拒绝时绝不能带业务正文"
    row = _rows(fixture["store"])[0]
    # 「终态之后不得写 matched」是**顺序**要求：预检那次核对写下的 matched 是当时的事实，
    # 不能因为之后进入终态就反过来要求它消失，更不能在终态**之后**再盖一次。
    # 所以比较的是「最终事务之前」与「最终事务之后」的值。
    assert before, "探针必须真的在最终事务处采到状态"
    assert row["exec_config_state"] == before[0], "最终事务不得在终态之后改写身份核对状态"
    # 库里的快照可以留着（`refuse()` 从不删已算出的结果，那是审计材料）；
    # 判据是它**没有被交出去** —— 上一行的 `order_no not in response` 就是这件事。
    if event == "refuse":
        assert row["state"] == "failed"
        assert row["reason"] == "exec_config_drift"
    else:
        # 过期沿用既有语义：与「读的时候发现过期」同一个状态与原因。
        assert row["state"] == "expired"
        assert row["reason"] == "ttl_elapsed"


@pytest.mark.parametrize("release_path", ["first_claim", "retry_release", "race_release"])
def test_live_record_releases_the_snapshot_at_the_final_gate(tmp_path, monkeypatch, release_path):
    """**正对照**：同样的三条路径，不注入任何事件时必须在最终核对后交出快照。

    没有这一条，「三条路径都返回 403」也可能只是三条路径都坏了。
    """
    manifest = _write_manifest(tmp_path)
    identity = _identity(manifest)
    fixture = _fixture(tmp_path, monkeypatch, manifest=manifest)
    assert _delegate(fixture).status_code == 502
    run_id = _drive_to_release_path(fixture, release_path)

    store = fixture["app"].state.gateway.agent_run_store
    calls: list[str] = []

    def observe(run_id_, observed, **kwargs):
        calls.append(run_id_)
        return original(run_id_, observed, **kwargs)

    original = store.confirm_exec_config
    monkeypatch.setattr(store, "confirm_exec_config", observe)

    response = _claim_with(fixture, run_id, identity)

    assert len(calls) == 2
    assert response.status_code == 200, response.text
    assert response.json()["order_no"] == "ORDER-A"
    assert _rows(fixture["store"])[0]["exec_config_state"] == "matched"


def test_expiry_boundary_uses_the_existing_semantics(tmp_path, monkeypatch):
    """到期判据是 ``expires_at <= now``，不是「已经过了一阵子」。

    三档：到期前一刻放行、**恰好到期**拒绝、明显过期拒绝。中间那一档是边界 ——
    用 ``<`` 代替 ``<=`` 会让一次恰好在到期瞬间到达的放行通过。
    """
    manifest = _write_manifest(tmp_path)
    identity = _identity(manifest)
    cases = []
    for label, offset in (("before", -1), ("exact", 0), ("after", 1)):
        case_dir = tmp_path / label
        case_dir.mkdir()
        fixture = _fixture(case_dir, monkeypatch, manifest=manifest)
        assert _delegate(fixture).status_code == 502
        run_id = _prepare_succeeded(fixture)
        expires = fixture["store"].get(run_id).expires_at
        monkeypatch.setattr(agent_runs, "_now", lambda e=expires, o=offset: e + timedelta(seconds=o))
        response = _claim_with(fixture, run_id, identity)
        cases.append((label, response.status_code, _rows(fixture["store"])[0]["state"]))
        monkeypatch.undo()

    assert cases[0][:2] == ("before", 200), cases
    assert cases[1] == ("exact", 403, "expired"), cases
    assert cases[2] == ("after", 403, "expired"), cases


# --------------------------------------------------------------------------
# 清单对照自身的判据（不需要 JDK：只验「独立期望」这一层）
# --------------------------------------------------------------------------


def test_every_manifest_sample_declares_an_independent_expectation():
    """对照表必须覆盖每份样本，且**至少一个正例** —— 否则对照会在两端全坏时仍「一致」。

    Codex 的复现：把所有样本以 0644 复制过去，两端连正常样例都拒绝，接受数量为 0，
    旧脚本照样 PASS。根因是判据只比较「两侧相同」，没有比较「各自应当是什么」。
    """
    import json

    root = Path(__file__).resolve().parents[1] / "tools/m2_close/m3node"
    samples = {path.name for path in (root / "manifest-samples").glob("*.json")}
    declared = json.loads((root / "manifest_samples_expected.json").read_text())
    expectations = {name: spec for name, spec in declared.items() if not name.startswith("_")}

    assert samples == set(expectations), f"未声明期望的样本：{samples - set(expectations)}"
    assert any(spec["accept"] for spec in expectations.values()), "必须至少声明一个正例"


def test_normal_sample_is_accepted_and_identity_is_the_raw_bytes_digest(tmp_path):
    """正例必须真的被接受；身份是原始字节 sha256。"""
    root = Path(__file__).resolve().parents[1] / "tools/m2_close/m3node"
    source = root / "manifest-samples/accept-normal.json"
    sample = tmp_path / "accept-normal.json"
    sample.write_bytes(source.read_bytes())
    sample.chmod(0o600)

    manifest = execution_manifest.load(sample)
    assert manifest is not None, "合规清单必须被接受（这是对照的正例）"
    assert manifest.identity == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "mode",
    [0o644, 0o640, 0o604],
)
def test_group_or_other_access_is_rejected_regardless_of_checkout_mode(tmp_path, mode):
    """权限负例**独立构造**，不依赖 Git 是否保存 0600。

    这同时解释了为什么对照脚本要把样本复制到一次性目录再显式设权限：
    Git 不保存 0600，检出通常就是 0644，直接读工作树会让「全部拒绝」变成常态。
    """
    root = Path(__file__).resolve().parents[1] / "tools/m2_close/m3node"
    sample = tmp_path / "sample.json"
    sample.write_bytes((root / "manifest-samples/accept-normal.json").read_bytes())
    sample.chmod(mode)

    with pytest.raises(execution_manifest.ExecutionManifestError):
        execution_manifest.load(sample)
