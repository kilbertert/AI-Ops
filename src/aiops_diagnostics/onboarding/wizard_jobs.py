"""V2.1 自助开通长耗时任务（infra_provision / publish_environment）的零密钥异步队列账本（ADR-0358）。

职责边界：
1. 将原本超过 HTTP 超时或跨越 41/36/客户机的基建配套与正式发布收编为持久任务账本；
2. 沿 ``drafts.py`` 既有 ``environment_wizard_jobs`` 表运行，严格剥离任何密码/口令/私钥/会话键；
3. 供管理面 API（``wizard_dispatch.py``）入队与查询进度，供 dev-host worker 经内部受签端点认领并回报分步状态。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.private_files import ensure_private_directory, protect_private_file
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.onboarding import drafts

JOB_STATUS_SCHEMA = "aiops-wizard-job/1"
INFRA_STEPS = (
    "allowlist",
    "execution_node",
    "cold_install_and_onboard",
    "customer_nginx",
    "live_probe",
)
PUBLISH_STEPS = (
    "enable_registry",
    "sync_target_runtime",
    "activate_node_route",
)
_FORBIDDEN_SECRET_PARTS = frozenset({"password", "passwd", "secret", "private_key", "token", "third_session"})
_TERMINAL_STATES = frozenset({"completed", "failed"})


class WizardJobError(ValueError):
    """异步向导任务输入校验、读取或状态迁移异常。"""


def is_valid_job_id(job_id: str) -> bool:
    """校验异步向导任务标识格式，禁止异常字符或注入（无脆弱正则）。"""
    if not isinstance(job_id, str) or not job_id.startswith("wizjob_"):
        return False
    suffix = job_id[len("wizjob_") :]
    return 16 <= len(suffix) <= 48 and all(ch in "0123456789abcdef" for ch in suffix)


def contains_secret_key(obj: Any) -> bool:
    """递归检查载荷键名是否包含任何敏感凭据片段。"""
    if isinstance(obj, dict):
        for key, val in obj.items():
            lowered = str(key).lower()
            if any(part in lowered for part in _FORBIDDEN_SECRET_PARTS):
                return True
            if contains_secret_key(val):
                return True
    elif isinstance(obj, list):
        return any(contains_secret_key(item) for item in obj)
    return False


def sanitize_public_dict(payload: Any) -> Any:
    """递归移除任何敏感字段后再序列化，确保零明文凭据落盘或回显。"""
    if isinstance(payload, dict):
        cleaned: dict[str, Any] = {}
        for key, val in payload.items():
            lowered = str(key).lower()
            if any(part in lowered for part in _FORBIDDEN_SECRET_PARTS):
                continue
            cleaned[str(key)] = sanitize_public_dict(val)
        return cleaned
    if isinstance(payload, list):
        return [sanitize_public_dict(item) for item in payload]
    return payload


def _initial_steps(action: str) -> list[dict[str, Any]]:
    names = PUBLISH_STEPS if action == "publish_environment" else INFRA_STEPS
    return [{"step": name, "state": "pending", "reason": ""} for name in names]


def _public_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    progress = json.loads(str(data.get("progress") or "{}"))
    result = json.loads(str(data.get("result") or "{}"))
    req = json.loads(str(data.get("request") or "{}"))
    state = str(data.get("state") or "queued")
    steps = progress.get("steps")
    if not isinstance(steps, list):
        steps = _initial_steps(str(data.get("action") or ""))
    public_result = sanitize_public_dict(result)
    out: dict[str, Any] = {
        "schema": JOB_STATUS_SCHEMA,
        "job_id": str(data["id"]),
        "environment": str(data["environment"]),
        "action": str(data["action"]),
        "state": state,
        "provisioned": state in _TERMINAL_STATES or state == "running",
        "passed": state == "completed" and not data.get("error_code"),
        "failed_step": str(progress.get("failed_step") or ""),
        "current_step": str(progress.get("current_step") or ""),
        "error_code": str(data.get("error_code") or ""),
        "steps": sanitize_public_dict(steps),
        "request": sanitize_public_dict(req),
        "result": public_result if isinstance(public_result, dict) else {},
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }
    if isinstance(out["result"], dict):
        for k in (
            "consumer_kb_id",
            "operator_kb_id",
            "workspace_id",
            "consumer_app_id",
            "operator_app_id",
            "tenant_id",
            "gateway_url",
            "published",
        ):
            if k in out["result"]:
                out[k] = out["result"][k]
    return out


def enqueue_job(
    registry_path: str | Path,
    *,
    environment: str,
    action: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """幂等入队异步向导任务；同环境同动作尚有进行中任务时复用活跃记录。"""
    if action not in {"infra_provision", "publish_environment"}:
        raise WizardJobError("ENVIRONMENT_WIZARD_ACTION_UNSUPPORTED")
    if contains_secret_key(request):
        raise WizardJobError("ENVIRONMENT_WIZARD_SECRET_PERSISTENCE_FORBIDDEN")

    path = str(registry_path)
    ensure_private_directory(Path(path).parent)
    clean_req = sanitize_public_dict(request)
    canonical_req = json.dumps(clean_req, ensure_ascii=False, sort_keys=True)
    now = time.time()

    db_path = drafts._database(path)
    with drafts._locked(path), gateway_db.initialize(db_path) as conn:
        protect_private_file(Path(db_path))
        drafts.initialize_wizard_jobs(conn)
        existing = conn.execute(
            "SELECT * FROM environment_wizard_jobs "
            "WHERE environment=? AND action=? AND state IN ('queued','running') "
            "ORDER BY updated_at DESC LIMIT 1",
            (environment, action),
        ).fetchone()
        if existing is not None:
            return _public_row(existing)

        digest = hashlib.sha256(f"{environment}:{action}:{canonical_req}:{now}".encode()).hexdigest()[:24]
        job_id = f"wizjob_{digest}"
        progress = {
            "current_step": "",
            "failed_step": "",
            "steps": _initial_steps(action),
        }
        conn.execute(
            "INSERT INTO environment_wizard_jobs("
            "id,environment,action,state,request,progress,result,error_code,created_at,updated_at,lease_expires_at"
            ") VALUES(?,?,?,'queued',?,?,?,'',?,?,0)",
            (
                job_id,
                environment,
                action,
                canonical_req,
                json.dumps(progress, ensure_ascii=False),
                json.dumps({}, ensure_ascii=False),
                now,
                now,
            ),
        )
        created = conn.execute("SELECT * FROM environment_wizard_jobs WHERE id=?", (job_id,)).fetchone()
        assert created is not None
        return _public_row(created)


def get_job(registry_path: str | Path, job_id: str) -> dict[str, Any] | None:
    """按任务 ID 只读查询向导异步任务最新进度与脱敏结果。"""
    if not is_valid_job_id(job_id):
        raise WizardJobError("ENVIRONMENT_WIZARD_INVALID")
    path = str(registry_path)
    db_path = Path(drafts._database(path))
    if not db_path.exists():
        return None
    with gateway_db.initialize(db_path) as conn:
        protect_private_file(db_path)
        drafts.initialize_wizard_jobs(conn)
        row = conn.execute("SELECT * FROM environment_wizard_jobs WHERE id=?", (job_id,)).fetchone()
    return _public_row(row) if row is not None else None


def get_latest_job(
    registry_path: str | Path,
    *,
    environment: str,
    action: str | None = None,
) -> dict[str, Any] | None:
    """按环境（及可选动作）查询最新一条异步任务。"""
    path = str(registry_path)
    db_path = Path(drafts._database(path))
    if not db_path.exists():
        return None
    with gateway_db.initialize(db_path) as conn:
        protect_private_file(db_path)
        drafts.initialize_wizard_jobs(conn)
        if action:
            row = conn.execute(
                "SELECT * FROM environment_wizard_jobs "
                "WHERE environment=? AND action=? ORDER BY updated_at DESC, created_at DESC LIMIT 1",
                (environment, action),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM environment_wizard_jobs "
                "WHERE environment=? ORDER BY updated_at DESC, created_at DESC LIMIT 1",
                (environment,),
            ).fetchone()
    return _public_row(row) if row is not None else None


def claim_next_job(
    registry_path: str | Path,
    *,
    lease_seconds: float = 600.0,
) -> dict[str, Any] | None:
    """由 dev-host worker 原子认领下一条待执行或租约已过期的异步向导任务。"""
    path = str(registry_path)
    ensure_private_directory(Path(path).parent)
    now = time.time()
    lease_until = now + max(float(lease_seconds), 30.0)
    db_path = drafts._database(path)
    with drafts._locked(path), gateway_db.initialize(db_path) as conn:
        protect_private_file(Path(db_path))
        drafts.initialize_wizard_jobs(conn)
        row = conn.execute(
            "SELECT * FROM environment_wizard_jobs "
            "WHERE state='queued' OR (state='running' AND lease_expires_at > 0 AND lease_expires_at < ?) "
            "ORDER BY created_at ASC LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE environment_wizard_jobs SET state='running', updated_at=?, lease_expires_at=? WHERE id=?",
            (now, lease_until, row["id"]),
        )
        claimed = conn.execute("SELECT * FROM environment_wizard_jobs WHERE id=?", (row["id"],)).fetchone()
        assert claimed is not None
        return _public_row(claimed)


def update_job_progress(
    registry_path: str | Path,
    job_id: str,
    *,
    step: str,
    step_state: str,
    steps_payload: list[dict[str, Any]] | None = None,
    result_patch: dict[str, Any] | None = None,
    lease_seconds: float = 600.0,
) -> dict[str, Any]:
    """逐子步刷新任务实时进度并续租，绝不持久化敏感键。"""
    if not is_valid_job_id(job_id):
        raise WizardJobError("ENVIRONMENT_WIZARD_INVALID")
    if step_state not in {"pending", "running", "passed", "failed", "skipped"}:
        raise WizardJobError("ENVIRONMENT_WIZARD_INVALID")
    if result_patch is not None and contains_secret_key(result_patch):
        raise WizardJobError("ENVIRONMENT_WIZARD_SECRET_PERSISTENCE_FORBIDDEN")
    if steps_payload is not None and contains_secret_key(steps_payload):
        raise WizardJobError("ENVIRONMENT_WIZARD_SECRET_PERSISTENCE_FORBIDDEN")

    path = str(registry_path)
    now = time.time()
    with drafts._locked(path), gateway_db.initialize(drafts._database(path)) as conn:
        drafts.initialize_wizard_jobs(conn)
        row = conn.execute("SELECT * FROM environment_wizard_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise WizardJobError("ENVIRONMENT_WIZARD_JOB_NOT_FOUND")
        progress = json.loads(str(row["progress"] or "{}"))
        if steps_payload is not None:
            progress["steps"] = sanitize_public_dict(steps_payload)
        else:
            steps = list(progress.get("steps") or _initial_steps(str(row["action"])))
            updated = False
            for item in steps:
                if item.get("step") == step:
                    item["state"] = step_state
                    updated = True
                    break
            if not updated and step:
                steps.append({"step": step, "state": step_state, "reason": ""})
            progress["steps"] = steps
        progress["current_step"] = step
        if step_state == "failed":
            progress["failed_step"] = step
        existing_result = json.loads(str(row["result"] or "{}"))
        if result_patch:
            existing_result.update(sanitize_public_dict(result_patch))
        conn.execute(
            "UPDATE environment_wizard_jobs "
            "SET state='running', progress=?, result=?, updated_at=?, lease_expires_at=? WHERE id=?",
            (
                json.dumps(progress, ensure_ascii=False),
                json.dumps(existing_result, ensure_ascii=False),
                now,
                now + max(float(lease_seconds), 30.0),
                job_id,
            ),
        )
        updated_row = conn.execute("SELECT * FROM environment_wizard_jobs WHERE id=?", (job_id,)).fetchone()
        assert updated_row is not None
        return _public_row(updated_row)


def complete_job(
    registry_path: str | Path,
    job_id: str,
    *,
    passed: bool,
    result: dict[str, Any] | None = None,
    error_code: str = "",
    failed_step: str = "",
    steps_payload: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """将异步任务推向终态（completed / failed），剥离一切敏感字段。"""
    if not is_valid_job_id(job_id):
        raise WizardJobError("ENVIRONMENT_WIZARD_INVALID")
    if result is not None and contains_secret_key(result):
        raise WizardJobError("ENVIRONMENT_WIZARD_SECRET_PERSISTENCE_FORBIDDEN")

    path = str(registry_path)
    now = time.time()
    with drafts._locked(path), gateway_db.initialize(drafts._database(path)) as conn:
        drafts.initialize_wizard_jobs(conn)
        row = conn.execute("SELECT * FROM environment_wizard_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise WizardJobError("ENVIRONMENT_WIZARD_JOB_NOT_FOUND")
        progress = json.loads(str(row["progress"] or "{}"))
        if steps_payload is not None:
            progress["steps"] = sanitize_public_dict(steps_payload)
        if failed_step:
            progress["failed_step"] = failed_step
        existing_result = json.loads(str(row["result"] or "{}"))
        if result:
            existing_result.update(sanitize_public_dict(result))
        final_state = "completed" if passed else "failed"
        conn.execute(
            "UPDATE environment_wizard_jobs "
            "SET state=?, progress=?, result=?, error_code=?, updated_at=?, lease_expires_at=0 WHERE id=?",
            (
                final_state,
                json.dumps(progress, ensure_ascii=False),
                json.dumps(existing_result, ensure_ascii=False),
                "" if passed else str(error_code or "ENVIRONMENT_WIZARD_JOB_FAILED"),
                now,
                job_id,
            ),
        )
        done_row = conn.execute("SELECT * FROM environment_wizard_jobs WHERE id=?", (job_id,)).fetchone()
        assert done_row is not None
        return _public_row(done_row)
