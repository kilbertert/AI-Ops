"""向导内置知识库上传代理、默认对客试跑 FAQ 与免 JSON 瞬时三能力自验（ADR-0358 P1-4）。

解决交付现场两大断点：
1. **知识库投喂割裂**：直接在开通向导内向对客知识库 / 独立管家知识库上传文档，并提供一键
   装入默认充电桩通用对客 FAQ 模板（标注临时验收模板，上线后建议替换为客户正式文档）；
2. **手工拼 ``validation-cases/*.json`` 与抓凭据门槛过高**：支持在内存中以测试手机号+验证码
   换取瞬时 ``thirdSession``、用标准化全证据问句跑通三能力自验，并在 ``finally`` 中强制
   吊销临时管家令牌、清空本地凭据引用——零磁盘明文持久化、零凭据回显。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from aiops_diagnostics.identity.order_identity import is_order_identifier

MAX_KNOWLEDGE_DOC_BYTES = 10 * 1024 * 1024
ALLOWED_KNOWLEDGE_ROLES = frozenset({"consumer", "operator"})
ALLOWED_KNOWLEDGE_SUFFIXES = frozenset({".md", ".txt", ".docx", ".pdf"})

DEFAULT_CONSUMER_STARTER_FAQ_FILENAME = "aiops-consumer-starter-faq.md"
DEFAULT_CONSUMER_STARTER_FAQ_CONTENT = """# 充电常见问题（AI-Ops 临时验收模板）

> 提示：本文件为 AI-Ops 开通首轮验收内置的通用对客 FAQ 模板，建议正式对外服务后替换为客户官方客服文档。

## 1. 充电结束后枪拔不出来怎么办？
请先在车辆中控屏执行「解锁充电口」或使用车钥匙解锁车门，切勿强行拉扯充电枪头；若解锁后仍无法拔出，请联系场站现场运维或客服人员协助处理。

## 2. 如何查询我的充电订单状态？
在智能客服对话框中直接提供您的充电订单号，即可查看订单当前记录状态（如充电中、已完成、异常结束等）。

## 3. 充电扣费与余额不足异常结束如何核对？
充电费用由电费与服务费组成；若订单提示余额不足导致异常中止，请在客户端查看账户扣费流水与余额明细，如有疑问联系场站客服人工核对。
"""

HttpCaller = Callable[[str, str, dict[str, str], dict[str, Any] | None], tuple[int, str]]


class WizardValidationRefused(ValueError):
    """向导知识库上传或免 JSON 瞬时自验被安全规则拒绝时的稳定异常。"""


def _validate_https_url(url: str) -> str:
    if not isinstance(url, str):
        raise WizardValidationRefused("ENVIRONMENT_WIZARD_HTTPS_REQUIRED")
    raw = url.strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise WizardValidationRefused("ENVIRONMENT_WIZARD_HTTPS_REQUIRED")
    return raw


def _validate_safe_filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename.strip() or len(filename) > 200:
        raise WizardValidationRefused("ENVIRONMENT_KB_FILENAME_INVALID")
    name = filename.strip()
    if "/" in name or "\\" in name or ".." in name or "\x00" in name:
        raise WizardValidationRefused("ENVIRONMENT_KB_FILENAME_INVALID")
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_KNOWLEDGE_SUFFIXES:
        raise WizardValidationRefused("ENVIRONMENT_KB_EXTENSION_UNSUPPORTED")
    return name


def _validate_kb_id(kb_id: str) -> str:
    if (
        not isinstance(kb_id, str)
        or not (1 <= len(kb_id) <= 128)
        or not all(ch.isascii() and (ch.isalnum() or ch in "_-:") for ch in kb_id)
    ):
        raise WizardValidationRefused("ENVIRONMENT_KB_ID_INVALID")
    return kb_id


def upload_wizard_knowledge_doc(
    *,
    target_role: str,
    kb_id: str,
    filename: str,
    content_bytes: bytes,
    uploader: Callable[[str, str, str, bytes], dict[str, Any]],
    starter_template: bool = False,
) -> dict[str, Any]:
    """向指定角色（对客/管家）独立知识库上传单份文档并严守后缀、大小与路径穿越边界。"""
    if target_role not in ALLOWED_KNOWLEDGE_ROLES:
        raise WizardValidationRefused("ENVIRONMENT_KB_ROLE_INVALID")
    clean_kb_id = _validate_kb_id(kb_id)
    clean_name = _validate_safe_filename(filename)
    if not isinstance(content_bytes, (bytes, bytearray)) or not content_bytes:
        raise WizardValidationRefused("ENVIRONMENT_KB_CONTENT_EMPTY")
    if len(content_bytes) > MAX_KNOWLEDGE_DOC_BYTES:
        raise WizardValidationRefused("ENVIRONMENT_KB_FILE_TOO_LARGE")

    res = uploader(target_role, clean_kb_id, clean_name, bytes(content_bytes))
    return {
        "target_role": target_role,
        "kb_id": clean_kb_id,
        "filename": clean_name,
        "document_id": str(res.get("document_id") or ""),
        "chunk_count": int(res.get("chunk_count") or 0),
        "starter_template": bool(starter_template),
    }


def install_consumer_starter_faq(
    *,
    kb_id: str,
    uploader: Callable[[str, str, str, bytes], dict[str, Any]],
) -> dict[str, Any]:
    """一键向对客知识库安装内置充电桩通用试跑 FAQ 模板。"""
    return upload_wizard_knowledge_doc(
        target_role="consumer",
        kb_id=kb_id,
        filename=DEFAULT_CONSUMER_STARTER_FAQ_FILENAME,
        content_bytes=DEFAULT_CONSUMER_STARTER_FAQ_CONTENT.encode("utf-8"),
        uploader=uploader,
        starter_template=True,
    )


def _default_http_caller(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
) -> tuple[int, str]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        return 0, json.dumps({"error": type(exc).__name__})


def exchange_ephemeral_consumer_session(
    *,
    company_gateway_url: str,
    phone: str,
    code: str,
    tenant_id: str,
    http_caller: HttpCaller | None = None,
) -> str:
    """仅在内存中通过测试手机号+验证码换取瞬时 C 端 thirdSession，严禁落盘或出现在异常中。"""
    base = _validate_https_url(company_gateway_url)
    clean_phone = phone.strip()
    clean_code = code.strip()
    clean_tid = tenant_id.strip()
    if not clean_phone or not clean_code or not clean_tid:
        raise WizardValidationRefused("ENVIRONMENT_CONSUMER_LOGIN_INPUT_REQUIRED")
    caller = http_caller or _default_http_caller
    http_code, text = caller(
        "POST",
        f"{base}/mallapi/userinfo/phone/login",
        {"Content-Type": "application/json", "tenant-id": clean_tid},
        {"phone": clean_phone, "code": clean_code},
    )
    if http_code != 200:
        raise WizardValidationRefused("ENVIRONMENT_CONSUMER_LOGIN_FAILED")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise WizardValidationRefused("ENVIRONMENT_CONSUMER_LOGIN_FAILED") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    session = str((data or {}).get("thirdSession") or "").strip() if isinstance(data, dict) else ""
    if not session:
        raise WizardValidationRefused("ENVIRONMENT_CONSUMER_LOGIN_FAILED")
    return session


def revoke_ephemeral_operator_token(
    *,
    company_gateway_url: str,
    operator_token: str,
    http_caller: HttpCaller | None = None,
) -> bool:
    """在自验结束后调用公司认证端点吊销瞬时管家令牌。"""
    base = _validate_https_url(company_gateway_url)
    tok = operator_token.strip()
    if not tok:
        return False
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    caller = http_caller or _default_http_caller
    http_code, _ = caller(
        "DELETE",
        f"{base}/auth/token/logout",
        {"Authorization": f"Bearer {tok}"},
        None,
    )
    return http_code in (200, 204)


def run_zero_json_self_validation(
    *,
    environment: str,
    tenant_id: str,
    company_gateway_url: str,
    order_no: str,
    device_id: str,
    offline_start: str,
    offline_end: str,
    consumer_order_no: str,
    consumer_reference_id: str,
    validator_runner: Callable[[dict[str, Any]], dict[str, Any]],
    consumer_session: str | None = None,
    consumer_phone: str | None = None,
    consumer_code: str | None = None,
    operator_token: str | None = None,
    http_caller: HttpCaller | None = None,
) -> dict[str, Any]:
    """免手工 JSON 的瞬时三能力自验：内存换取会话、固化过程统计问句、finally 强制吊销清理。"""
    base = _validate_https_url(company_gateway_url)
    if not is_order_identifier(order_no.strip()) or not is_order_identifier(consumer_order_no.strip()):
        raise WizardValidationRefused("ENVIRONMENT_VALIDATION_ORDER_INVALID")
    if not device_id.strip() or not offline_start.strip() or not offline_end.strip():
        raise WizardValidationRefused("ENVIRONMENT_VALIDATION_OFFLINE_SAMPLE_REQUIRED")
    if not consumer_reference_id.strip():
        raise WizardValidationRefused("ENVIRONMENT_VALIDATION_REFERENCE_REQUIRED")

    ephemeral_consumer = (consumer_session or "").strip()
    ephemeral_operator = (operator_token or "").strip()
    if not ephemeral_consumer and consumer_phone and consumer_code:
        ephemeral_consumer = exchange_ephemeral_consumer_session(
            company_gateway_url=base,
            phone=consumer_phone,
            code=consumer_code,
            tenant_id=tenant_id,
            http_caller=http_caller,
        )
    if not ephemeral_consumer:
        raise WizardValidationRefused("ENVIRONMENT_VALIDATION_CONSUMER_REQUIRED")
    if not ephemeral_operator:
        raise WizardValidationRefused("ENVIRONMENT_VALIDATION_OPERATOR_REQUIRED")

    cases_payload = {
        "environment": environment.strip(),
        "tenant_id": tenant_id.strip(),
        "consumer_session": ephemeral_consumer,
        "operator_token": ephemeral_operator,
        "cases": {
            "consumer": {
                "order_no": consumer_order_no.strip(),
                "reference_id": consumer_reference_id.strip(),
                "question": (
                    f"充电结束后枪拔不出来怎么办？顺便帮我查一下订单 {consumer_order_no.strip()} 的状态。"
                ),
            },
            "order": {
                "order_no": order_no.strip(),
                "question": (
                    f"请核查订单 {order_no.strip()} 的充电状态、结束原因，"
                    "并给出电压/电流/功率等关键过程统计。"
                ),
            },
            "offline": {
                "device_id": device_id.strip(),
                "start": offline_start.strip(),
                "end": offline_end.strip(),
                "question": (
                    f"请查询设备 {device_id.strip()} 在 {offline_start.strip()} 至 "
                    f"{offline_end.strip()} 期间的离线记录并统计离线次数与时长。"
                ),
            },
        },
    }

    revoked = False
    try:
        raw_result = validator_runner(cases_payload)
    finally:
        try:
            revoked = revoke_ephemeral_operator_token(
                company_gateway_url=base,
                operator_token=ephemeral_operator,
                http_caller=http_caller,
            )
        except Exception:
            revoked = False
        cases_payload["consumer_session"] = ""
        cases_payload["operator_token"] = ""
        ephemeral_consumer = ""
        ephemeral_operator = ""

    capabilities = raw_result.get("capabilities") if isinstance(raw_result, dict) else {}
    passed = (
        isinstance(capabilities, dict)
        and set(capabilities) >= {"consumer", "order", "offline"}
        and all(
            isinstance(capabilities[k], dict) and capabilities[k].get("state") == "passed"
            for k in ("consumer", "order", "offline")
        )
    )
    return {
        "schema": "aiops-wizard-self-validation/1",
        "environment": environment.strip(),
        "tenant_id": tenant_id.strip(),
        "passed": passed,
        "capabilities": capabilities,
        "credentials_persisted": False,
        "operator_session_revoked": revoked,
        "business_accuracy": "unconfirmed",
    }
