"""第二轮返工的生命周期用例（R2/R4—R9）：每条都是**真实链路**跑通，不是"探针不报错"。

独立验收的 10 个反例在这里变成正向断言 + 负向断言：

| 用例 | 反例 | 这里断言的是 |
| --- | --- | --- |
| 导入 id | `request_id` 带冒号被真实 Dify 拒绝 | 用补丁的 Pydantic 约束校验，且回读同一应用 |
| 草稿试用 | 草稿被 `row.active` 拒 | 完整构造一次试用，且正式指针表仍为空 |
| 第二份草稿 | 按租户取第一条目标 | 两份草稿各自可发布 |
| 草稿摘要 | 发布忽略 `expected_draft` | 过期摘要被拒、正确摘要放行 |
| 重试撤回 | 重试把撤回的版本重新激活 | 重试只对账，不覆盖撤回 |
| 凭据对账 | 跨请求又建一枚 key | 第二次尝试复用第一枚 |
| 首次发布失败 | 留下不可续跑的草稿 agent | 重试接着发布，不新建 agent |
"""

import json
import re
from pathlib import Path

import pytest

# `dual` 夹具依赖 operations 下的 `operator` 夹具；它必须在本模块命名空间里，
# 否则 pytest 解析 `dual` 的依赖时会报 "fixture 'operator' not found"。
from operations.test_operator_cpms import operator  # noqa: F401,E402
from pydantic import BaseModel, Field

from aiops_diagnostics.consumer.consumer_publication import workflow_of
from aiops_diagnostics.dify import self_service_prepare as prepare
from aiops_diagnostics.dify import service_keys
from aiops_diagnostics.dify.self_service_store import public_job
from dify.test_dual_entry import (  # noqa: F401,E402
    CONSUMER_APP,
    TARGET,
    dual,
    request,
    run,
    source_document,
)

ONLY = "consumer"


def consumer_only(dual):  # noqa: F811
    return {"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}}


# ── R2：导入 id 必须满足真实补丁的约束 ──────────────────────────────────


def test_consumer_import_id_satisfies_the_published_patch_and_replays_the_same_app(dual, monkeypatch):  # noqa: F811
    """用**生产补丁里的那条正则**校验我们发出的 `request_id`。

    独立验收的反例：`job_id + ":consumer"` 带冒号，真实 Dify 的
    `AppImportPayload.request_id` 是 `^[a-f0-9-]{36}$`，会 `string_pattern_mismatch`。
    替身不校验，所以它一直是绿的。
    """
    patches = Path(__file__).parents[2] / "deploy/dify-36/patches/1.17.1"
    patch = next(patches.glob("*import*")).read_text()
    match = re.search(r'request_id:.*pattern=r"([^"]+)"', patch)
    assert match is not None, "补丁里没有 request_id 的约束 —— 那条约束本身变了"
    pattern = match[1]

    class PublishedImportSchema(BaseModel):
        request_id: str = Field(pattern=pattern)

    sent = []
    real = dual.console._call
    lose = [True]

    def validating(workspace, path, **kwargs):
        if path == "/console/api/apps/imports" and kwargs.get("method") == "POST":
            sent.append(kwargs["body"]["request_id"])
            PublishedImportSchema.model_validate(kwargs["body"])
            result = real(workspace, path, **kwargs)
            if lose[0]:
                # 导入**成功**但响应丢失：这正是幂等导入要处理的那一种
                lose[0] = False
                raise TimeoutError("synthetic lost import response")
            return result
        return real(workspace, path, **kwargs)

    monkeypatch.setattr(dual.console, "_call", validating)
    job = dual.service.create(
        dual.caller, request(dual, scope=ONLY, sources=consumer_only(dual), key="rework-import-001")
    )
    # 第一次：导入成功但响应丢失
    with pytest.raises(TimeoutError):
        prepare.copy_consumer_app(
            dual.console, None, "workspace-tenant-b", job["id"], {"name": "x"}, source_document()
        )
    # 第二次：同一个 request_id 派生同一个应用 id（回读而不是再建一个）
    first = prepare.consumer_app_id("workspace-tenant-b", job["id"])
    replay = prepare.copy_consumer_app(
        dual.console, None, "workspace-tenant-b", job["id"], {"name": "x"}, source_document()
    )
    assert replay == first
    assert sent and sent[0] == prepare.consumer_request_id(job["id"])
    assert sent[1] == sent[0], "重试换了 request_id —— 那就回读不到同一个应用"
    assert len([a for a in dual.console.apps if a == first]) == 1


# ── R4：未发布草稿可以试用 ──────────────────────────────────────────────


def test_unpublished_consumer_draft_can_be_previewed_through_the_real_builder(dual):  # noqa: F811
    only = consumer_only(dual)
    first = run(dual, request(dual, scope=ONLY, sources=only, key="rework-trial-001"))
    assert first["entries"]["consumer"]["published"]
    # 第二份草稿（不推进指针）
    second = run(dual, request(dual, scope=ONLY, sources=only, key="rework-trial-002"))
    draft_app = second["entries"]["consumer"]["app_id"]
    assert second["entries"]["consumer"]["published"] is False

    from types import SimpleNamespace

    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
    from aiops_diagnostics.gateway import gateway_api

    before = ChatRouteBindingStore(dual.settings.chat_route_bindings_file).get(
        "env-a", TARGET, "consumer", "consumer_service"
    )
    trial = gateway_api._workflow_trial_draft(  # noqa: SLF001 - 真实 builder
        dual.context,
        draft_app,
        SimpleNamespace(effective_tenant_id=TARGET),  # type: ignore[arg-type]
    )
    assert getattr(trial, "draft", False) is True
    # 试用**不写**正式指针：那一行仍然是第一份应用的 active 记录
    after = ChatRouteBindingStore(dual.settings.chat_route_bindings_file).get(
        "env-a", TARGET, "consumer", "consumer_service"
    )
    assert after == before


# ── R5：第二份草稿能被发布 ──────────────────────────────────────────────


def test_second_consumer_draft_targets_its_own_app_and_can_be_published(dual):  # noqa: F811
    only = consumer_only(dual)
    first = run(dual, request(dual, scope=ONLY, sources=only, key="rework-second-001"))
    second = run(dual, request(dual, scope=ONLY, sources=only, key="rework-second-002"))
    assert first["entries"]["consumer"]["published"]
    candidate = second["entries"]["consumer"]
    assert candidate["draft_only"]
    route = dual.service.route_for(TARGET, "consumer")
    dual.service.publish(dual.caller, candidate["app_id"], route.revision)
    assert dual.service.route_for(TARGET, "consumer").app_id == candidate["app_id"]


# ── R6：发布必须核对操作者确认的草稿摘要 ────────────────────────────────


def test_stale_draft_digest_is_refused_and_the_current_one_is_accepted(dual):  # noqa: F811
    only = consumer_only(dual)
    result = run(dual, request(dual, scope=ONLY, sources=only, key="rework-digest-001"))
    app = result["entries"]["consumer"]["app_id"]
    route = dual.service.route_for(TARGET, "consumer")
    status = dual.service.status(dual.caller, app)
    assert re.fullmatch(r"[0-9a-f]{64}", status["draft_sha256"] or "")
    # 过期的摘要（另一个标签页的内容）⇒ 拒绝
    with pytest.raises(Exception, match="DRAFT_CHANGED"):
        dual.service.publish(dual.caller, app, route.revision, "0" * 64)
    # 页面读到的那一份 ⇒ 放行
    published = dual.service.publish(dual.caller, app, route.revision, status["draft_sha256"])
    assert published["app_id"] == app
    assert dual.service.route_for(TARGET, "consumer").active


# ── R7：重试不复活已撤回的版本 ──────────────────────────────────────────


def test_retry_after_lost_receipt_does_not_reactivate_a_retracted_version(dual, monkeypatch):  # noqa: F811
    original = dual.service._publish_consumer  # noqa: SLF001
    calls = [0]

    def lost(*args, **kwargs):
        outcome = original(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 1:
            raise TimeoutError("synthetic lost publish receipt")
        return outcome

    monkeypatch.setattr(dual.service, "_publish_consumer", lost)
    only = consumer_only(dual)
    job = dual.service.create(dual.caller, request(dual, scope=ONLY, sources=only, key="rework-retry-001"))
    dual.service.run(job["id"], "session")
    route = dual.service.route_for(TARGET, "consumer")
    assert route.active
    dual.service.change_version(dual.caller, route.app_id, route.revision)
    assert not dual.service.route_for(TARGET, "consumer").active
    dual.service.run(job["id"], "session")
    after = dual.service.route_for(TARGET, "consumer")
    assert not after.active, "重试把客户主动撤回的版本又打开了"


# ── R8：凭据对账跨请求/重启仍然幂等 ─────────────────────────────────────


def test_key_reconciliation_survives_a_lost_list_and_a_new_attempt(dual, monkeypatch, tmp_path):  # noqa: F811
    app = "88888888-1111-4111-8111-111111111111"
    real = dual.console._call
    after_create = [False]

    def flaky(workspace, path, **kwargs):
        if path.endswith("/api-keys") and kwargs.get("method", "GET") == "GET" and after_create[0]:
            raise TimeoutError("synthetic list outage")
        if path.endswith("/api-keys") and kwargs.get("method", "GET") == "POST":
            real(workspace, path, **kwargs)
            after_create[0] = True
            raise TimeoutError("synthetic lost key response")
        return real(workspace, path, **kwargs)

    monkeypatch.setattr(dual.console, "_call", flaky)
    database = tmp_path / "gateway.db"
    with pytest.raises((TimeoutError, ValueError)):
        service_keys.ensure(
            dual.console, tmp_path / "restart-keys", "workspace-target", app, database=database
        )
    assert dual.console.key_posts == 1
    # 下一次（相当于另一个进程/重启之后）只靠库里的创建前快照对账，不再建第二枚
    monkeypatch.setattr(dual.console, "_call", real)
    path = service_keys.ensure(
        dual.console, tmp_path / "restart-keys", "workspace-target", app, database=database
    )
    assert dual.console.key_posts == 1, "重试又建了一枚 key"
    assert Path(path).read_text().strip() == "app-key-1"


# ── R9：首次发布失败后可以续跑 ──────────────────────────────────────────


def test_failed_first_consumer_publish_resumes_instead_of_creating_another_agent(dual, monkeypatch):  # noqa: F811
    from dify.test_dual_entry import Dsl

    original = Dsl.publish_workflow
    calls = [0]

    def fail_once(self, app_id=None):
        calls[0] += 1
        if calls[0] == 1:
            raise TimeoutError("synthetic publisher unavailable")
        return original(self, app_id)

    monkeypatch.setattr(Dsl, "publish_workflow", fail_once)
    only = consumer_only(dual)
    job = dual.service.create(
        dual.caller, request(dual, scope=ONLY, sources=only, key="rework-bootstrap-001")
    )
    dual.service.run(job["id"], "session")
    assert dual.service.store.get(job["id"])["state"] == "failed"
    agents_before = [a.name for a in dual.context.agent_manager.store.list(TARGET)]
    dual.service.run(job["id"], "session")
    result = public_job(dual.service.store.get(job["id"]))
    assert result["state"] == "completed", result
    agents_after = [a.name for a in dual.context.agent_manager.store.list(TARGET)]
    assert agents_before == agents_after, "重试新建了一个 agent 而不是续跑原来的草稿"


# ── 工作区目录锚点：来源摘要与客户端发布冻结同一份内容 ──────────────────


def test_consumer_publish_freezes_the_digest_the_page_saw(dual):  # noqa: F811
    only = consumer_only(dual)
    result = run(dual, request(dual, scope=ONLY, sources=only, key="rework-freeze-001"))
    app = result["entries"]["consumer"]["app_id"]
    status = dual.service.status(dual.caller, app)
    dual.service.publish(dual.caller, app, status["route_revision"], status["draft_sha256"])
    route = dual.service.route_for(TARGET, "consumer")
    snapshot = dual.context.agent_manager.store.version(
        route.agent_id, TARGET, route.agent_version_no
    ).snapshot
    assert workflow_of(snapshot["workflow"]) == status["draft_sha256"]
    assert json.dumps(snapshot["consumer_service"], sort_keys=True)
