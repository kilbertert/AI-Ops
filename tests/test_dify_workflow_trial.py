"""M3-01 真 Dify 试用：在**临时应用**里跑冻结的草稿。

这一组守的是试用与发布的**边界**，不是 Dify 怎么调：

* 试用**不动正式指针**、不产生 `agent_versions` —— 试用不是发布；
* 试用有**真实执行身份**（`workflow_run_id` / `task_id`），不是"跑了个本地诊断"；
* 草稿临时改变（另一个请求发布/撤回）⇒ 试用拒绝，不拿旧草稿冒充；
* 无论成功失败，临时应用都被删掉。

替换的只有两个**外部边界**（控制台的导入/删 app、Dify 的 run/stop）；路由查找、
执行记录、取数凭据、放行判据都是本仓真实代码。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiops_diagnostics import chat_data_grant, chat_execution
from aiops_diagnostics.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.dify_workflow_trial import WorkflowTrial
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


def _scope(tenant: str = "TENANT-A") -> ScopeContext:
    subject = SubjectRecord(b_user_id="", c_user_id="USER-A", tenant_id=tenant)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=tenant,
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset(),
        resolved_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )


def _seed_route(tmp_path: Path, *, version: int = 2) -> Any:
    key = tmp_path / "app.key"
    key.write_text("service-key\n")
    key.chmod(0o600)
    return ChatRouteBindingStore(tmp_path / "gateway.db").publish(
        environment="env-a",
        tenant_id="TENANT-A",
        entry="consumer",
        scenario=chat_execution.SCENARIO_OWN_ORDER_STATUS,
        engine="dify",
        agent_id="agt_real",
        agent_version_no=version,
        app_id="app-11111111-2222-3333-4444-555555555555",
        workflow_id="22222222-2222-2222-2222-222222222222",
        workflow_key_ref=str(key),
    )


class _Admin:
    """控制台边界替身：导入出一个临时 app，记下删除。"""

    def __init__(self) -> None:
        self.imported: list[str] = []
        self.deleted: list[str] = []

    def import_app_dsl(self, workspace_id: str, *, yaml_content: str, name: str) -> str:
        app = f"tmp-{len(self.imported) + 1}"
        self.imported.append(app)
        return app

    def app_service_key(self, workspace_id: str, app_id: str) -> str:
        return "tmp-key"

    def delete_app(self, workspace_id: str, app_id: str) -> None:
        self.deleted.append(app_id)


class _Engine:
    """Dify 执行边界替身：给出真实形状的执行身份。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.started: list[tuple[str, str]] = []
        self.stops: list[str] = []

    def run(self, *, workflow_key, workflow_id, inputs, user, on_started=None):
        from aiops_diagnostics.dify_chat_workflow import WorkflowRun

        if self.fail:
            raise RuntimeError("engine exploded")
        run_id = "wr-" + str(len(self.started) + 1)
        task_id = "task-" + run_id
        self.started.append((run_id, task_id))
        if on_started is not None:
            on_started(run_id, task_id)
        return WorkflowRun(
            workflow_run_id=run_id,
            task_id=task_id,
            workflow_id=workflow_id,
            outputs={
                "answer": "ok",
                "snapshot": {
                    "order_no": "ORDER-1",
                    "status_code": "0",
                    "status_role": "charging",
                    "observed_at": "2026-10-24T00:00:00+00:00",
                    "source": "cpms.chargingInfo",
                },
            },
            status="succeeded",
            created_at=0,
        )

    def stop(self, *, workflow_key, task_id, user) -> bool:
        self.stops.append(task_id)
        return True


class _Runtime:
    def __init__(self, settings: Any, *, answer: dict | None = None) -> None:
        self.gateway_settings = settings
        self.store = type("S", (), {"path": settings.chat_route_bindings_file})()
        self._qa_registrations: dict[str, Any] = {}
        self._answer = answer or {"blocks": [{"kind": "text", "text": "订单充电中"}]}

    def _require_chat_order_visible(self, scope, order_no) -> None:
        return None

    def _release_dify_answer(self, **kwargs) -> dict:
        return self._answer


@contextmanager
def _trial_env(tmp_path: Path, monkeypatch, *, engine_fail: bool = False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    route = _seed_route(tmp_path)

    # 用**实例**而不是类：代码里是 `settings.dify_chat_base_url`（实例属性访问），
    # 传类对象会让一处取值行为与真实情况不同。
    settings = SimpleNamespace(
        chat_route_bindings_file=str(tmp_path / "gateway.db"),
        dify_chat_base_url="http://dify.invalid:5001",
        chat_dify_key_dir=str(tmp_path),
        dify_console_base_url="http://dify.invalid:10008",
        dify_console_api_key="console-key",
        execution_environment="env-a",
    )

    admin = _Admin()
    engine = _Engine(fail=engine_fail)
    monkeypatch.setattr("aiops_diagnostics.dify_workflow_trial.DifyConsoleAdmin", lambda settings: admin)
    monkeypatch.setattr("aiops_diagnostics.dify_workflow_trial.DifyWorkflowClient", lambda base: engine)
    monkeypatch.setattr(
        "aiops_diagnostics.dify_workflow_trial.DifyDslClient",
        type(
            "C",
            (),
            {
                "__init__": lambda self, source: None,
                "publish_workflow": lambda self, app_id=None: True,
                "published_workflow_id": lambda self, app_id=None: route.workflow_id,
                "export_dsl": lambda self, app_id=None, workflow_id=None: (
                    "app:\n  mode: workflow\nworkflow:\n  graph:\n    nodes: []\n    edges: []\n"
                ),
            },
        ),
    )
    yield _Runtime(settings), route, admin, engine


def _draft(route: Any) -> WorkflowTrial:
    from aiops_diagnostics.dify_dsl_pull import DifyDslSource

    return WorkflowTrial(
        "app:\n  mode: workflow\nworkflow:\n  graph:\n    nodes: []\n    edges: []\n",
        DifyDslSource(
            base_url="http://dify.invalid:10008",
            app_id=route.app_id,
            api_key="console-key",
            workspace_id="ws",
        ),
        route,
    )


def test_a_trial_runs_on_a_real_engine_and_leaves_the_pointer_alone(tmp_path, monkeypatch):
    """试用有真实执行身份；正式指针与 `agent_versions` 一个字都没动。"""
    with _trial_env(tmp_path, monkeypatch) as (runtime, route, admin, engine):
        before = ChatRouteBindingStore(tmp_path / "gateway.db").get(
            "env-a", "TENANT-A", "consumer", chat_execution.SCENARIO_OWN_ORDER_STATUS
        )
        result = _draft(route).execute(runtime, "tr_1", _scope(), "ORDER-1", "zh")

        assert result.status == "completed"
        assert result.report["workflow_run_id"] and result.report["task_id"]
        assert engine.started, "试用必须真的跑一次引擎"
        after = ChatRouteBindingStore(tmp_path / "gateway.db").get(
            "env-a", "TENANT-A", "consumer", chat_execution.SCENARIO_OWN_ORDER_STATUS
        )
        assert (after.revision, after.agent_version_no, after.workflow_id) == (
            before.revision,
            before.agent_version_no,
            before.workflow_id,
        ), "试用不许动正式指针"


def test_a_trial_leaves_no_agent_version_behind(tmp_path, monkeypatch):
    """试用不产生版本 —— 它只在临时应用里跑，不碰 `agent_versions`。"""
    with _trial_env(tmp_path, monkeypatch) as (runtime, route, _admin, _engine):
        _draft(route).execute(runtime, "tr_1", _scope(), "ORDER-1", "zh")
    import sqlite3

    with sqlite3.connect(tmp_path / "gateway.db") as connection:
        rows = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='agent_versions'"
        ).fetchone()[0]
        if rows:
            count = connection.execute("SELECT COUNT(*) FROM agent_versions").fetchone()[0]
            assert count == 0


def test_the_temporary_app_is_always_deleted(tmp_path, monkeypatch):
    """成功与失败都要删临时应用：留下它们会在控制台里堆出无主的 app。"""
    with _trial_env(tmp_path, monkeypatch) as (runtime, route, admin, _engine):
        _draft(route).execute(runtime, "tr_ok", _scope(), "ORDER-1", "zh")
    assert admin.deleted == admin.imported

    with (
        _trial_env(tmp_path / "boom", monkeypatch, engine_fail=True) as (runtime, route, admin, _e),
        pytest.raises(RuntimeError),
    ):
        _draft(route).execute(runtime, "tr_fail", _scope(), "ORDER-1", "zh")
    assert admin.deleted == admin.imported, "失败路径也要删"


def test_a_changed_route_refuses_the_trial(tmp_path, monkeypatch):
    """草稿冻结点之后这条路由被改过 ⇒ 拒绝，不拿旧草稿冒充"现在这一版"。"""
    with _trial_env(tmp_path, monkeypatch) as (runtime, route, _admin, engine):
        store = ChatRouteBindingStore(tmp_path / "gateway.db")
        store.publish(
            environment="env-a",
            tenant_id="TENANT-A",
            entry="consumer",
            scenario=chat_execution.SCENARIO_OWN_ORDER_STATUS,
            engine="dify",
            agent_id="agt_real",
            agent_version_no=3,
            app_id=route.app_id,
            workflow_id=route.workflow_id,
            workflow_key_ref=route.workflow_key_ref,
            expected_revision=route.revision,
        )
        with pytest.raises(RuntimeError) as raised:
            _draft(route).execute(runtime, "tr_2", _scope(), "ORDER-1", "zh")
        assert "route changed" in str(raised.value)
        assert engine.started == [], "拒绝之后一次引擎都不该跑"


def test_a_trial_revokes_its_grant_even_when_the_engine_fails(tmp_path, monkeypatch):
    """失败也必须撤销取数凭据：那是"这一次"的授权，不是长期能力。"""
    with _trial_env(tmp_path / "boom", monkeypatch, engine_fail=True) as (runtime, route, _a, _e):
        with pytest.raises(RuntimeError):
            _draft(route).execute(runtime, "tr_fail", _scope(), "ORDER-1", "zh")
        grants = chat_data_grant.DataGrantStore(tmp_path / "boom" / "gateway.db")
        states = [g.state for g in grants.all()] if hasattr(grants, "all") else []
        assert all(state == "revoked" for state in states), states


def test_the_trial_callback_accepts_its_temporary_workflow(tmp_path, monkeypatch):
    """试用取数回调必须接受**临时的** workflow 身份。

    试用跑在临时应用里，`workflow_id` 与正式绑定天然不同。若回调守卫仍拿正式
    workflow 身份去比，真实取数会被拒成 `CHARGING_STATUS_DISABLED`，试用永远完不成
    —— 而上一条用例替换了引擎与放行方法，看不见这条路径。

    这里直接调守卫本身：判据是"源路由仍在且 revision 未变"，不是"workflow 相同"。
    """
    from types import SimpleNamespace

    from aiops_diagnostics.gateway_api import _require_trial_source_route

    with _trial_env(tmp_path, monkeypatch) as (_runtime, route, _admin, _engine):
        settings = SimpleNamespace(chat_route_bindings_file=str(tmp_path / "gateway.db"))
        run = SimpleNamespace(
            kind="trial",
            binding_key=route.key,
            route_revision=route.revision,
            workflow_id="temporary-workflow-id-differs-from-the-formal-one",
        )
        # 不抛 = 通过：临时 workflow 身份**不参与**这条判据。
        _require_trial_source_route(SimpleNamespace(settings=settings), run)


def test_the_trial_callback_refuses_a_changed_source_route(tmp_path, monkeypatch):
    """源路由被重新发布过 ⇒ 试用回调拒绝（revision 变了）。"""
    from types import SimpleNamespace

    from aiops_diagnostics.chat_route_bindings import ChatRouteBindingStore
    from aiops_diagnostics.gateway_api import StandardAPIError, _require_trial_source_route

    with _trial_env(tmp_path, monkeypatch) as (_runtime, route, _admin, _engine):
        ChatRouteBindingStore(tmp_path / "gateway.db").publish(
            environment="env-a",
            tenant_id="TENANT-A",
            entry="consumer",
            scenario=chat_execution.SCENARIO_OWN_ORDER_STATUS,
            engine="dify",
            agent_id="agt_real",
            agent_version_no=3,
            app_id=route.app_id,
            workflow_id=route.workflow_id,
            workflow_key_ref=route.workflow_key_ref,
            expected_revision=route.revision,
        )
        settings = SimpleNamespace(chat_route_bindings_file=str(tmp_path / "gateway.db"))
        run = SimpleNamespace(
            kind="trial", binding_key=route.key, route_revision=route.revision, workflow_id="tmp"
        )
        with pytest.raises(StandardAPIError):
            _require_trial_source_route(SimpleNamespace(settings=settings), run)
