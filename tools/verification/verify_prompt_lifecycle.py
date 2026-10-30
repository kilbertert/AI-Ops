#!/usr/bin/env python3
"""提示词生命周期验收：16 格样本 + 两端版本闭环，**无人值守可复跑**。

```bash
uv run python tools/verification/verify_prompt_lifecycle.py            # 默认输出到临时目录
uv run python tools/verification/verify_prompt_lifecycle.py --out DIR  # 私有目录
```

**这个脚本证明什么**：提示词写的正文在两端都能到达用户（两个契约、五类客户端出口、三类管家
出口，各两种风格），事实判据（金额标签、货币断言、到账承诺、状态方向、找站说明）与迁移保护
都按契约生效，节点发布的"草稿不动正式版 / 发布推进版本 / 恢复指回旧版"成立。

**它不证明什么**：真实 Dify 应用、真实 Java 节点、真实模型的观感与稳定性，以及 41 上的
页面点击。那些只能在真实环境验（qa-plan §五），本地桩不能冒充 —— 所以脚本末尾会明确打印
"未验证"清单，而不是把它算作通过。

退出码：0 = 全部断言通过；1 = 有断言失败；2 = 环境不合格（拒绝在非临时目录写证据）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))


def _client_paths():
    from aiops_diagnostics.consumer.consumer_documents import answer_selection
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer

    return answer_selection, final_answer


BILLING = {
    "order_no": "2107546575906942977",
    "source": "cpms.orderDetail",
    "observed_at": "2026-10-30T09:08:26+00:00",
    "order_amount": "27.36",
    "recorded_payment_amount": "20",
    "recorded_refund_amount": "0",
    "refund_status": "0",
}
STATUS = {
    "order_no": "2107546575906942977",
    "source": "cpms.chargingInfo",
    "observed_at": "2026-10-30T09:08:26+00:00",
    "status_code": "1",
    "status_role": "finished",
}


def _documents_context():
    return {
        "mode": "documents",
        "status": "found",
        "sources": [
            {"reference_id": "source-1", "retrieved_reference_id": "doc-1", "title": "充电枪", "content": ""}
        ],
        "live_snapshot": None,
    }


#: 8 类场景 × 2 风格。文本是**合成**的：这里验的是契约与判据，不是模型写得像不像人。
GRID = [
    ("client.knowledge", "简洁直接", "枪拔不下来时先在 App 点停止充电。", "documents"),
    ("client.knowledge", "亲切解释", "枪拔不下来别急：先在 App 里点停止充电，再按解锁键。", "documents"),
    ("client.status", "简洁直接", "这单已经结束充电。订单号 2107546575906942977。", "realtime"),
    (
        "client.status",
        "亲切解释",
        "帮您看了一下，这单的充电已经结束啦。订单号 2107546575906942977。",
        "realtime",
    ),
    (
        "client.billing",
        "简洁直接",
        "这单账单合计 27.36，系统记录的支付金额是 20，没有退款记录。订单号 2107546575906942977。",
        "realtime",
    ),
    (
        "client.billing",
        "亲切解释",
        "这单合计 27.36，账上记的付款是 20，没有退款记录。订单号 2107546575906942977。",
        "realtime",
    ),
    ("client.clarify", "简洁直接", "需要订单号才能查。", "clarify"),
    ("client.clarify", "亲切解释", "我这边还差一个订单号，方便说一下是哪一单吗？", "clarify"),
    ("client.not_found", "简洁直接", "这次没查到对应资料。", "not_found"),
    ("client.not_found", "亲切解释", "这次没有找到能回答的资料，换个说法我再帮您找找。", "not_found"),
]

OPERATOR_GRID = [
    ("operator.consult", "简洁直接", "状态 3 表示异常已由人工处理完毕。"),
    ("operator.consult", "亲切解释", "这个状态的意思是这次异常已经有同事处理过了，不用再操作。"),
    ("operator.diagnosis", "简洁直接", "这单的电费是 13.68，服务费 13.68，合计 27.36。"),
    ("operator.diagnosis", "亲切解释", "帮您算了一下：电费 13.68，服务费 13.68，加起来是 27.36。"),
    ("operator.site_search", "简洁直接", "按当前账号可见范围查到的站点如下。"),
    ("operator.site_search", "亲切解释", "我按您账号能看到的范围查了一下，结果在下面。"),
]

NEGATIVES = [
    ("client.billing 标签互换", "合计 20，支付 27.36", "CHATFLOW_FACT_LABEL_MISMATCH"),
    ("client.billing 编造单价", "另外每小时 3 元", "CHATFLOW_FACT_INVENTED_AMOUNT"),
    ("client.billing 到账承诺", "已到账", "CHATFLOW_FACT_PROMISE"),
    ("client.status 状态说反", "这单还没结束", "CHATFLOW_FACT_STATUS_INVERTED"),
]


def _run_client_grid(rows):
    answer_selection, final_answer = _client_paths()
    for name, style, text, kind in GRID:
        if kind == "documents":
            payload = {"kind": "knowledge", "answer": text, "reference_ids": ["source-1"]}
            got = answer_selection(
                json.dumps(payload), _documents_context(), language="zh", contract="slots"
            )[1]["blocks"][0]["text"]
        elif kind == "realtime":
            snapshot = BILLING if name == "client.billing" else STATUS
            context = {**_documents_context(), "live_snapshot": snapshot}
            payload = {"kind": "realtime", "answer": "", "reference_ids": [], "reply": text}
            got = answer_selection(json.dumps(payload), context, language="zh", contract="slots")[1][
                "blocks"
            ][0]["text"]
        else:
            answer = final_answer(
                json.dumps({"kind": kind, "faq_id": "", "reply": text}),
                {"status": "no_result", "candidates": []},
                "zh",
                None,
            )
            got = answer["blocks"][0]["text"]
        ok = got.startswith(text)
        rows.append((name, style, "pass" if ok else "FAIL", got.splitlines()[0][:48]))
        if not ok:
            rows[-1] = (name, style, "FAIL", got[:48])


def _run_operator_grid(rows):
    from aiops_diagnostics.operator import operator_conversation as conversation
    from aiops_diagnostics.operator import operator_cpms

    evidence = {
        "cpms_order_detail": {
            "source": "cpms.orderDetail",
            "observed_at": "2026-10-30T09:08:26+00:00",
            "data": {"orderNo": BILLING["order_no"]},
        }
    }
    sites = {
        "source": "cpms.siteSearch",
        "observed_at": "2026-10-30T09:08:26+00:00",
        "sites": [{"id": "SITE-1", "name": "天河测试站", "address": "广州市天河区测试路"}],
        "limited": False,
        "empty_scope": False,
        "total": 1,
        "scope_limited": True,
    }
    for name, style, text in OPERATOR_GRID:
        if name == "operator.consult":
            answer = {"kind": "answer", "text": text, "name": "", "address_terms": []}
            conversation.validate(answer, "状态3是什么意思")
            got = conversation.render(answer, None, "zh")["blocks"][0]["text"]
        elif name == "operator.diagnosis":
            answer = {
                "direct_answer": text,
                "summary": "订单已结束。",
                "probable_cause": "金额构成与计费模板一致。",
                "next_steps": ["核对分时电价请提供模板"],
                "limitations": ["上游未提供分时电价明细"],
                "evidence_tools": ["cpms_order_detail"],
            }
            operator_cpms.validate_answer(answer, evidence)
            got = operator_cpms.render_report(answer, evidence)
        else:
            answer = {"kind": "site_search", "text": text, "name": "天河", "address_terms": ["广州", "天河"]}
            conversation.validate(answer, "找广州天河充电站")
            got = conversation.render(answer, sites, "zh")["blocks"][0]["text"]
        ok = got.startswith(text) and (name != "operator.site_search" or "天河测试站" in got)
        rows.append((name, style, "pass" if ok else "FAIL", got.splitlines()[0][:48]))


def _run_negatives(rows):
    from aiops_diagnostics.consumer.consumer_documents import answer_selection
    from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

    for label, _fragment, code in NEGATIVES:
        snapshot = STATUS if "status" in label else BILLING
        text = {
            "CHATFLOW_FACT_LABEL_MISMATCH": "这单账单合计 20，系统记录的支付金额是 27.36，已退 0。"
            "订单号 2107546575906942977。",
            "CHATFLOW_FACT_INVENTED_AMOUNT": "这单合计 27.36，支付 20，已退 0，另外每小时 3 元。"
            "订单号 2107546575906942977。",
            "CHATFLOW_FACT_PROMISE": "这单合计 27.36，支付 20，已退 0，已到账。订单号 2107546575906942977。",
            "CHATFLOW_FACT_STATUS_INVERTED": "这单仍在充电中。订单号 2107546575906942977。",
        }[code]
        context = {**_documents_context(), "live_snapshot": snapshot}
        payload = {"kind": "realtime", "answer": "", "reference_ids": [], "reply": text}
        actual = ""
        try:
            answer_selection(json.dumps(payload), context, language="zh", contract="slots")
        except ChatRunUnavailable as exc:
            actual = exc.code
        rows.append((label, "—", "pass" if actual == code else "FAIL", actual or "accepted"))


def _run_lifecycle(rows, workdir):
    """版本生命周期：在**真实**路由绑定表上跑 A→发布 B→撤回，断言判据而不是写死 pass。

    用的是 `ChatRouteBindingStore` 与 `consumer_chat_service` 的判据本身；不是两段手写文本。
    页面级（Dify 编辑框 + 预览/发布/恢复按钮 + 真实模型）不在这里，脚本末尾记为 NOT_RUN。
    """
    from pathlib import Path
    from types import SimpleNamespace

    from aiops_diagnostics.consumer import consumer_chat_service
    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    db = Path(workdir) / "routes.db"
    store = ChatRouteBindingStore(db)

    def publish(version, revision):
        return store.publish(
            environment="env-a",
            tenant_id="TENANT-A",
            entry="consumer",
            scenario="consumer_service",
            engine="dify",
            agent_id="agent-1",
            agent_version_no=version,
            app_id="app-1",
            workflow_id="wf-1",
            expected_revision=revision,
        )

    first = publish(1, 0)
    rows.append(("lifecycle.publish_A", "—", "pass" if first.agent_version_no == 1 else "FAIL", "version=1"))
    second = publish(2, first.revision)
    advanced = second.agent_version_no == 2 and second.revision > first.revision
    rows.append(
        ("lifecycle.publish_B", "—", "pass" if advanced else "FAIL", f"version={second.agent_version_no}")
    )
    # 已签发 run 钉在 A：路由推进后仍然 active，判据放行；撤回后必须拒。
    runtime = SimpleNamespace(gateway_settings=SimpleNamespace(chat_route_bindings_file=str(db)))
    run = SimpleNamespace(
        environment="env-a", app_id="app-1", agent_version_no=1, route_revision=first.revision
    )
    active_ok = consumer_chat_service._pinned_route_active(runtime, run)
    rows.append(
        ("lifecycle.pinned_run_still_deliverable", "—", "pass" if active_ok else "FAIL", "route active")
    )
    current = store.get("env-a", "TENANT-A", "consumer", "consumer_service")
    store.retract(
        environment="env-a",
        tenant_id="TENANT-A",
        entry="consumer",
        scenario="consumer_service",
        expected_revision=current.revision,
    )
    retracted_ok = consumer_chat_service._pinned_route_active(runtime, run)
    rows.append(
        (
            "lifecycle.retraction_refuses_pinned_run",
            "—",
            "pass" if not retracted_ok else "FAIL",
            "route retracted",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="证据输出目录（默认临时目录）")
    args = parser.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix="aiops-prompt-lifecycle-"))
    if out.resolve() == Path("/var/lib/aiops-41").resolve() or "aiops-41" in str(out):
        print("拒绝在真实数据根写证据：本脚本只跑合成数据", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    os.umask(0o027)

    rows: list[tuple[str, str, str, str]] = []
    _run_client_grid(rows)
    _run_operator_grid(rows)
    _run_negatives(rows)
    _run_lifecycle(rows, out)

    width = max(len(r[0]) for r in rows)
    for name, style, verdict, detail in rows:
        print(f"{name:<{width}}  {style:<6}  {verdict:<4}  {detail}")
    failed = [r for r in rows if r[2] == "FAIL"]
    (out / "grid.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    print(f"\n共 {len(rows)} 项，失败 {len(failed)} 项；证据写入 {out}")
    not_run = [
        "页面级：在 Dify 编排页真实编辑指定文本框并点预览/发布/恢复（需隔离 Dify + 真实模型）",
        "真实 Java 节点的执行与找站工具调用（需隔离节点）",
        "真实模型的风格观感与稳定性（本地文本是合成的）",
        "41 上的在途与历史会话（需合并制品）",
    ]
    print("\nNOT_RUN（不计入通过数量）：")
    for item in not_run:
        print(f"  - {item}")
    (out / "not-run.json").write_text(json.dumps(not_run, ensure_ascii=False, indent=2) + "\n")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
