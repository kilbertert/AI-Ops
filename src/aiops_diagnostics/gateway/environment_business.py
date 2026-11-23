"""目标本机业务验证：案例只留本环境，实际模型取证不改变普通准入。"""

import fcntl
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

from aiops_diagnostics.common.private_files import (
    validate_private_directory,
    validate_private_file,
    write_private_text,
)
from aiops_diagnostics.common.scalar_rules import declared_timezone, parse_stored_time
from aiops_diagnostics.data.order_status import StatusRole, role_of
from aiops_diagnostics.execution.agent_runs import scope_from_json
from aiops_diagnostics.gateway import environment_registry
from aiops_diagnostics.identity.order_identity import is_order_identifier

CAPABILITIES = ("consumer", "order", "offline")


class ValidationRefused(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class CaseExpired(ValidationRefused):
    """案例窗口真实闭合的细分信号：对外error_code不变，复验方据此分终态。"""


def valid_spec(spec):
    if (
        not isinstance(spec, dict)
        or set(spec)
        != {
            "id",
            "environment",
            "tenant_id",
            "case_ref",
            "capabilities",
            "installed",
            "operator_app",
            "consumer",
        }
        or not re.fullmatch(r"task_[a-f0-9]{32}", str(spec["id"]))
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(spec["case_ref"]))
        or not isinstance(spec["capabilities"], list)
        or not spec["capabilities"]
        or any(not isinstance(c, str) for c in spec["capabilities"])
        or len(set(spec["capabilities"])) != len(spec["capabilities"])
        or not set(spec["capabilities"]) <= set(CAPABILITIES)
        or not isinstance(spec["consumer"], dict)
        or not isinstance(spec["installed"], dict)
    ):
        raise ValueError("invalid business validation request")


def case_file(context, reference):
    return Path(f"/etc/aiops-access-{context.settings.environment}/validation-cases") / (reference + ".json")


def load_case(context, spec, capability):
    valid_spec(spec)
    settings = context.settings
    if spec["environment"] != settings.environment or settings.execution_environment != settings.environment:
        raise ValueError("case environment mismatch")
    path = case_file(context, spec["case_ref"])
    directory = path.parent
    if not directory.exists():
        raise ValidationRefused("ENVIRONMENT_VALIDATION_CASE_REQUIRED")
    validate_private_directory(directory)
    if not path.exists():
        raise ValidationRefused("ENVIRONMENT_VALIDATION_CASE_REQUIRED")
    validate_private_file(path)
    if path.stat().st_nlink != 1 or path.stat().st_size > 16384 or any(p.is_symlink() for p in path.parents):
        raise ValueError("invalid private case")
    raw = path.read_bytes()
    case = json.loads(raw, object_pairs_hook=environment_registry._unique_object)
    if (
        not isinstance(case, dict)
        or set(case)
        != {"environment", "tenant_id", "expires_at", "operator_token", "consumer_session", "cases"}
        or (case["environment"], case["tenant_id"]) != (spec["environment"], spec["tenant_id"])
        or type(case["expires_at"]) not in (int, float)
        or not math.isfinite(case["expires_at"])
        or not isinstance(case["cases"], dict)
        or set(case["cases"]) - set(CAPABILITIES)
        or any(
            not isinstance(case[k], str) or len(case[k]) > 8192
            for k in ("operator_token", "consumer_session")
        )
    ):
        raise ValidationRefused("ENVIRONMENT_VALIDATION_CASE_EXPIRED_OR_INVALID")
    # 窗口闭合与内容无效分开：只有实际越过的期限是可判终态；超出允许上限的
    # 未来期限属无效输入（修正后可重核），与篡改/异形同按拒绝处理。
    now = time.time()
    if case["expires_at"] <= now:
        raise CaseExpired("ENVIRONMENT_VALIDATION_CASE_EXPIRED_OR_INVALID")
    if case["expires_at"] > now + 3600:
        raise ValidationRefused("ENVIRONMENT_VALIDATION_CASE_EXPIRED_OR_INVALID")
    item = case["cases"].get(capability)
    required = (
        {"question", "order_no", "reference_id"}
        if capability == "consumer"
        else {"question", "order_no"}
        if capability == "order"
        else {"question", "device_id", "start", "end"}
    )
    if (
        not isinstance(item, dict)
        or set(item) != required
        or any(not isinstance(v, str) or not v for v in item.values())
    ):
        raise ValidationRefused("ENVIRONMENT_VALIDATION_SAMPLE_REQUIRED")
    if len(item["question"]) > 4000:
        raise ValueError("case question too large")
    if capability in {"consumer", "order"} and (
        not is_order_identifier(item["order_no"]) or item["order_no"] not in item["question"]
    ):
        raise ValueError("explicit case order required")
    if capability == "consumer" and (not case["consumer_session"] or len(item["reference_id"]) > 128):
        raise ValidationRefused("ENVIRONMENT_VALIDATION_CONSUMER_REQUIRED")
    if capability in {"order", "offline"} and not case["operator_token"]:
        raise ValidationRefused("ENVIRONMENT_VALIDATION_OPERATOR_REQUIRED")
    if capability == "offline":
        if (
            not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", item["device_id"])
            or item["device_id"] not in item["question"]
        ):
            raise ValueError("explicit case device required")
        begin, end = (parse_stored_time(item[k]) for k in ("start", "end"))
        if (
            any(declared_timezone(item[k]) is None for k in ("start", "end"))
            or not 0 < (end - begin).total_seconds() <= 31 * 86400
        ):
            raise ValueError("invalid case window")
    return case, hashlib.sha256(raw).hexdigest()


def installation(context, spec):
    """运行代码不能读root账本；核受管现役manifest、配置、目录与runtime摘要。"""
    from aiops_diagnostics.gateway.environment_artifacts import runtime_files
    from aiops_diagnostics.gateway.environment_projection import validate_candidate

    settings = context.settings
    root = Path(f"/etc/aiops-access-{settings.environment}")
    app = Path(f"/opt/aiops-access-{settings.environment}")
    if str(settings.server_config_file) != str(root / "production.env") or (app / "pending.json").exists():
        raise ValueError("installed validation target required")
    build = json.loads((app / "current/manifest.json").read_text())
    expected = spec["installed"]
    if (
        build["version"] != __import__("aiops_diagnostics").__version__
        or build["revision"] != expected["source_revision"]
    ):
        raise ValueError("validation build changed")
    for file, key in (
        (root / "production.env", "config_sha256"),
        (root / "registry/environment.json", "registry_sha256"),
    ):
        if hashlib.sha256(file.read_bytes()).hexdigest() != expected[key]:
            raise ValidationRefused("ENVIRONMENT_VALIDATION_INSTALLATION_CHANGED")
    validate_candidate(
        root / "production.env",
        root / "registry/environment.json",
        settings.environment,
        runtime=root / "runtime",
        candidate_tenant=spec["tenant_id"],
    )
    files, manifest = runtime_files(
        root / "runtime/runtime.tar", expected["runtime_sha256"], settings.environment
    )
    registration = json.loads(files["registrations.json"])
    if (
        manifest["tenant_id"] != spec["tenant_id"]
        or registration["operator_app_id"] != spec["operator_app"]
        or registration["consumer_app_id"] != spec["consumer"]["app_id"]
    ):
        raise ValueError("validation resource changed")
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore

    store = SelfServiceStore(settings.database_file, readonly=True)
    for entry in ("operator", "consumer"):
        row = store.app(registration[entry + "_app_id"])
        if not row or (
            row["environment"],
            row["tenant_id"],
            row["workspace_id"],
            row["entry"],
            row["created_by"],
        ) != (
            spec["environment"],
            spec["tenant_id"],
            registration["workspace_id"],
            entry,
            "environment-runtime",
        ):
            raise ValidationRefused("ENVIRONMENT_VALIDATION_REGISTRATION_CHANGED")
    if (
        spec["consumer"]["workspace_id"] != registration["workspace_id"]
        or spec["consumer"]["knowledge_base_id"]
        not in next(
            c
            for c in json.loads(files["consumer-targets.json"])["targets"]
            if c["tenant_id"] == spec["tenant_id"]
        )["knowledge_base_ids"]
    ):
        raise ValidationRefused("ENVIRONMENT_VALIDATION_RESOURCE_CHANGED")
    return build


def operator_evidence(context, capability, trial_id, case, spec):
    run = context.agent_run_store.by_qa(trial_id)
    if (
        run is None
        or run.environment != spec["environment"]
        or scope_from_json(run.scope_json).effective_tenant_id != spec["tenant_id"]
        or run.publication_app_id != spec["operator_app"]
        or not run.exec_config_confirmed
        or not run.snapshot
    ):
        raise ValidationRefused("ENVIRONMENT_VALIDATION_RUN_MISMATCH")
    evidence = run.snapshot.get("evidence", {})
    if capability == "order":
        detail = evidence.get("cpms_order_detail", {}).get("data", {})
        if (
            run.order_no != case["order_no"]
            or str(detail.get("orderNo")) != case["order_no"]
            or role_of(detail.get("status"))
            not in {
                StatusRole.UNCONTROLLABLE_EXCEPTION,
                StatusRole.EXCEPTION_ALREADY_HANDLED,
                StatusRole.CONTROLLED_ABNORMAL_END,
            }
        ):
            raise ValidationRefused("ENVIRONMENT_VALIDATION_ABNORMAL_ORDER_REQUIRED")
        expected = {"cpms_order_detail", "cpms_charging_samples", "cpms_order_process_stats"}
        samples = evidence.get("cpms_charging_samples", {}).get("data", {}).get("records", [])
        stats = evidence.get("cpms_order_process_stats", {}).get("data", {})
        if (
            not expected <= set(evidence)
            or not isinstance(samples, list)
            or not samples
            or stats.get("sample_count") != len(samples)
            or not any(m.get("count", 0) > 0 for m in stats.get("metrics", []))
            or any(str(r.get("orderNo")) != case["order_no"] for r in samples)
        ):
            raise ValidationRefused("ENVIRONMENT_VALIDATION_PROCESS_EVIDENCE_REQUIRED")
        objects = [case["order_no"]]
    else:
        data = evidence.get("cpms_device_offline", {}).get("data", {})
        window = data.get("window", {})
        if data.get("scope", {}).get("device_id") != case["device_id"] or any(
            parse_stored_time(window.get(k, "")) != parse_stored_time(case[k]) for k in ("start", "end")
        ):
            raise ValidationRefused("ENVIRONMENT_VALIDATION_OFFLINE_SCOPE_MISMATCH")
        if not any(
            r.get("device_id") == case["device_id"] and r.get("raw_events", 0) > 0
            for r in data.get("top_devices", {}).get("items", [])
        ):
            raise ValidationRefused("ENVIRONMENT_VALIDATION_OFFLINE_EVENT_REQUIRED")
        expected, objects = {"cpms_device_offline"}, [case["device_id"]]
    return {
        "trial_id": trial_id,
        "run_id": run.run_id,
        "tools": sorted(expected),
        "objects": objects,
        "scope_fingerprint": run.scope_fingerprint,
        "node": run.actual_node_id,
        "gateway_version": run.gateway_version,
    }


def step(context, payload, *, start_trial, read_trial, resolve_identity):

    if (
        set(payload) != {"spec", "capability", "observe_only"}
        or type(payload["observe_only"]) is not bool
        or payload["capability"] not in CAPABILITIES
    ):
        raise ValueError("invalid validation step")
    spec, capability = payload["spec"], payload["capability"]
    valid_spec(spec)
    if capability not in spec["capabilities"]:
        raise ValueError("unselected capability")
    installation(context, spec)
    case, case_digest = load_case(context, spec, capability)
    selected = case["cases"][capability]
    directory = context.settings.data_home / "business-validation" / spec["id"]
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    validate_private_directory(directory)
    file = directory / (capability + ".json")
    lockfd = os.open(directory / (capability + ".lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lockfd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = None
        if file.exists() or file.is_symlink():
            validate_private_file(file)
            record = json.loads(file.read_text())
            if record["spec"] != spec or record["case_sha256"] != case_digest:
                raise ValueError("validation case changed")
            if "result" in record:
                # 恢复必须经过原run交付授权，不能把持久成功变成新的有效期。
                readback = read_trial(
                    context, {"app_id": spec["operator_app"], "trial_id": record["trial_id"]}
                )
                if readback["status"] != "completed":
                    raise ValidationRefused("ENVIRONMENT_VALIDATION_RESULT_EXPIRED")
                caller = resolve_identity(context, case["operator_token"])
                if caller.scope_fingerprint != record["result"]["evidence"]["scope_fingerprint"]:
                    raise ValueError("validation identity changed")
                return record["result"]
            if not record.get("trial_id"):
                raise ValidationRefused("ENVIRONMENT_VALIDATION_START_UNCONFIRMED")
        if record is None and payload["observe_only"]:
            raise ValidationRefused("ENVIRONMENT_VALIDATION_START_UNCONFIRMED")
        if capability == "consumer":
            if record:
                raise ValueError("consumer opening already attempted")
            write_private_text(file, json.dumps({"spec": spec, "case_sha256": case_digest}))
            descriptor = {**spec["consumer"], "order_no": selected["order_no"]}
            opened = context.consumer_candidate_leases.open(
                {"candidate": descriptor, "third_session": case["consumer_session"]}
            )
            caller = scope_from_json(opened["scope_json"])
            return {
                "candidate": descriptor,
                "token": opened["token"],
                "question": selected["question"],
                "expected_reference": selected["reference_id"],
                "scope_fingerprint": caller.scope_fingerprint,
                "case_sha256": case_digest,
            }
        if record is None:
            record = {"spec": spec, "case_sha256": case_digest}
            write_private_text(file, json.dumps(record))
            started = start_trial(
                context,
                {
                    "app_id": spec["operator_app"],
                    "runtime_sha256": spec["installed"]["runtime_sha256"],
                    "company_token": case["operator_token"],
                    "question": selected["question"],
                },
            )
            record["trial_id"] = started["trial_id"]
            write_private_text(file, json.dumps(record))
        deadline = time.monotonic() + 200
        while True:
            result = read_trial(context, {"app_id": spec["operator_app"], "trial_id": record["trial_id"]})
            if result["status"] not in {"pending", "queued", "running"}:
                break
            if time.monotonic() >= deadline:
                raise ValueError("validation run unfinished")
            time.sleep(1)
        installation(context, spec)
        if load_case(context, spec, capability)[1] != case_digest or result["status"] != "completed":
            raise ValidationRefused(result.get("error_code") or "ENVIRONMENT_VALIDATION_RUN_FAILED")
        proof = operator_evidence(context, capability, record["trial_id"], selected, spec)
        record["result"] = {
            "capability": capability,
            "state": "passed",
            "evidence": proof,
            "case_sha256": case_digest,
            "observed_at": time.time(),
            "business_accuracy": "unconfirmed",
        }
        write_private_text(file, json.dumps(record))
        return record["result"]


def validate_result(result, capability):
    """公开回执固定字段；缺少实际run与对象证明不得标passed。"""
    if not isinstance(result, dict) or result.get("capability") != capability:
        raise ValueError("invalid validation result")
    if result.get("state") == "blocked":
        if set(result) != {"capability", "state", "error_code"} or not re.fullmatch(
            r"[A-Z0-9_]{1,96}", str(result["error_code"])
        ):
            raise ValueError("invalid validation refusal")
        return
    if (
        set(result) != {"capability", "state", "evidence", "case_sha256", "observed_at", "business_accuracy"}
        or result["state"] != "passed"
        or result["business_accuracy"] != "unconfirmed"
        or not re.fullmatch(r"[a-f0-9]{64}", str(result["case_sha256"]))
        or type(result["observed_at"]) not in (int, float)
        or not math.isfinite(result["observed_at"])
        or not 0 <= time.time() - result["observed_at"] <= 900
    ):
        raise ValueError("invalid validation evidence")
    proof = result["evidence"]
    fields = {"trial_id", "run_id", "tools", "objects", "scope_fingerprint"} | (
        {"workflow_id", "app_id"} if capability == "consumer" else {"node", "gateway_version"}
    )
    expected = {
        "consumer": ["knowledge", "status"],
        "order": ["cpms_charging_samples", "cpms_order_detail", "cpms_order_process_stats"],
        "offline": ["cpms_device_offline"],
    }[capability]
    if (
        not isinstance(proof, dict)
        or set(proof) != fields
        or proof["tools"] != expected
        or not isinstance(proof["objects"], list)
        or len(proof["objects"]) != (2 if capability == "consumer" else 1)
    ):
        raise ValueError("invalid validation proof")
    values = [v for k, v in proof.items() if k not in {"tools", "objects", "node"}] + proof["objects"]
    if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.:+-]{1,160}", v) for v in values):
        raise ValueError("invalid validation evidence identity")
    if (
        capability != "consumer"
        and proof["node"] is not None
        and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", proof["node"])
    ):
        raise ValueError("invalid node identity")
