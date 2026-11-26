"""从用户路由生成公网规范；新增内部路由不会自动进入公开清单。"""

from __future__ import annotations

import html
import json
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute

from aiops_diagnostics.common.i18n import SUPPORTED_LANGUAGES
from aiops_diagnostics.knowledge.knowledge_retrieval import ALLOWED_MEDIA_TYPES

# 方法也是边界：GET /shortcuts 是用户入口，POST 同路径却是管理接口。
OPERATIONS = {
    ("post", "/v1/assistant/questions"): ("客服问答", "提交问题或管家单订单任务", "Assistant"),
    ("get", "/v1/assistant/questions/{qa_id}"): ("客服问答", "查询问答结果", "QA"),
    ("get", "/v1/assistant/questions/{qa_id}/events"): (
        "客服问答",
        "订阅问答SSE实时事件流（执行路径+正文流式增量）",
        "QAEventStream",
    ),
    ("post", "/v1/assistant/questions/{qa_id}/cancel"): ("客服问答", "取消问答任务（无请求体）", "QA"),
    ("post", "/v1/assistant/questions/{qa_id}/regenerate"): (
        "后台AI抽屉",
        "原地重新生成会话最后一轮排查问答",
        "QA",
    ),
    ("post", "/v1/assistant/questions/{qa_id}/feedback"): (
        "客服问答",
        "提交或取消回答点赞/点踩反馈",
        "QAFeedback",
    ),
    ("post", "/v1/assistant/questions/{qa_id}/export"): (
        "后台AI抽屉",
        "导出排查报告至公司下载中心",
        "DownloadCenterTask",
    ),
    ("get", "/v1/assistant/questions"): ("客服问答", "查询本人问答历史", "QAList"),
    ("get", "/v1/assistant/agents"): ("后台AI抽屉", "读取可用后台智能体列表", "AssistantAgents"),
    ("post", "/v1/assistant/files"): (
        "后台AI抽屉",
        "上传排查附件（图片/PDF/Word）并登记下载中心",
        "AssistantFile",
    ),
    ("get", "/v1/assistant/files/{file_id}"): ("后台AI抽屉", "查询附件解析状态与元信息", "AssistantFile"),
    ("get", "/v1/assistant/files/{file_id}/preview"): (
        "后台AI抽屉",
        "读取图片附件缩略图预览",
        "FilePreview",
    ),
    ("post", "/v1/assistant/files/{file_id}/download-task"): (
        "后台AI抽屉",
        "登记附件下载任务至公司下载中心",
        "DownloadCenterTask",
    ),
    ("get", "/v1/conversations"): ("会话", "查询本人会话", "ConversationList"),
    ("get", "/v1/conversations/{conversation_id}"): ("会话", "查询会话轮次", "Conversation"),
    ("patch", "/v1/conversations/{conversation_id}"): ("会话", "重命名本人会话标题", "Conversation"),
    ("delete", "/v1/conversations/{conversation_id}"): ("会话", "删除本人测试会话", "Deleted"),
    ("get", "/v1/faq/catalog"): ("FAQ与入口", "读取固定FAQ目录", "Catalog"),
    ("get", "/v1/faq/recommendations"): ("FAQ与入口", "读取推荐问法", "Recommendations"),
    ("post", "/v1/faq/answer"): ("FAQ与入口", "读取固定FAQ答案", "FAQ"),
    ("get", "/v1/shortcuts"): ("FAQ与入口", "读取已发布快捷动作", "Shortcuts"),
    ("get", "/v1/assistant/starters"): ("FAQ与入口", "读取开场白与推荐语", "Starters"),
    ("get", "/v1/banner"): ("FAQ与入口", "读取入口横幅", "Banners"),
    ("get", "/v1/media/{signed_id}"): ("媒体", "读取签名图片或视频", "Media"),
    ("get", "/v1/orders/{order_no}/access"): ("订单", "检查当前主体的订单权限", "OrderAccess"),
    # 站点列表由 C 端会话身份推导范围（本人订单里出现过的站点），不接受任何范围参数。
    ("get", "/v1/sites"): ("站点", "查询本人订单关联的站点", "Sites"),
    ("post", "/v1/health-report-jobs"): ("健康报告", "创建充电健康报告", "Health"),
    ("get", "/v1/health-report-jobs/{job_id}"): ("健康报告", "查询健康报告", "Health"),
    ("post", "/v1/standard/analyses"): ("管家分析", "创建站点时间窗分析", "Analysis"),
    ("get", "/v1/standard/analyses/{analysis_id}"): ("管家分析", "查询站点分析报告", "Analysis"),
}

OPERATOR_ONLY_PATHS = {
    "/v1/assistant/agents",
    "/v1/assistant/files",
    "/v1/assistant/files/{file_id}",
    "/v1/assistant/files/{file_id}/preview",
    "/v1/assistant/files/{file_id}/download-task",
    "/v1/assistant/questions/{qa_id}/regenerate",
    "/v1/assistant/questions/{qa_id}/export",
}

DESCRIPTION = """用户接口测试规范。C端：third-session + X-Business-Entry: consumer；
管家：公司个人Bearer + X-Business-Entry: operator，不发送third-session。身份不得混用。
凭据由正常公司登录取得，服务密钥与代理来源头不属于客户端参数。
当前服务器是现役入口：提交问答可能读取业务数据、消耗模型额度；使用授权测试账号和少量请求。
202仅表示受理。按type选qa_id/diagnosis_id/analysis_id，按retry_after_ms轮询对应GET；
终态或HTTP错误即停止，POST超时不要盲目重发。HTTP 200并不代表业务结论正确。
本规范只公开用户接口；内部回调和管理接口不在本清单中。
"""


def _object(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _schemas() -> dict[str, Any]:
    string = {"type": "string"}
    nullable_object = {"type": ["object", "null"], "additionalProperties": True}
    objects = {"type": "array", "items": {"type": "object", "additionalProperties": True}}
    error = _object(code=string, message=string, retryable={"type": "boolean"})
    error["required"] = ["code", "message", "retryable"]
    job = {
        "status": {
            "type": "string",
            "enum": ["queued", "running", "completed", "inconclusive", "failed", "cancelled", "expired"],
        },
        "language": string,
        "retry_after_ms": {"type": ["integer", "null"], "description": "活动任务等待毫秒数；终态为null"},
        "result": nullable_object,
        "error": {"anyOf": [_ref("APIError"), {"type": "null"}]},
    }
    qa = _object(
        **job,
        type={"const": "qa", "type": "string"},
        qa_id=string,
        question=string,
        attempt_no={"type": "integer"},
        parent_qa_id={"type": ["string", "null"]},
        stream_url=string,
        agent=_ref("AssistantAgentBrief"),
        attachments={"type": "array", "items": _ref("AssistantFile")},
        feedback={"anyOf": [_ref("QAFeedback"), {"type": "null"}]},
        conversation_id=string,
        turn_no={"type": "integer"},
    )
    qa["required"] = ["type", "qa_id", "question", "status", "language", "retry_after_ms", "result", "error"]
    qa["properties"]["result"] = {"anyOf": [_ref("Answer"), {"type": "null"}]}
    analysis = _object(
        **job,
        analysis_id=string,
        card=string,
        site_ids={"type": ["array", "null"], "items": string},
        window_start=string,
        window_end=string,
    )
    analysis["required"] = ["analysis_id", "card", "status"]
    conversation = _object(
        conversation_id=string,
        business_entry=string,
        agent_id=string,
        title=string,
        turn_count={"type": "integer"},
        agent_version_key=string,
        active_order_no={"type": ["string", "null"]},
        is_generating={"type": "boolean"},
        turns=objects,
        created_at=string,
        updated_at=string,
        expires_at=string,
    )
    citation = _object(
        citation_id=string,
        source_type={"type": "string", "enum": ["cpms_tool", "attachment", "knowledge"]},
        title=string,
        snippet=string,
        observed_at=string,
        file_id={"type": ["string", "null"]},
        page={"type": ["integer", "null"]},
        downloadable={"type": "boolean"},
    )
    assistant_file = _object(
        file_id=string,
        file_name=string,
        category={"type": "string", "enum": ["image", "document"]},
        extension=string,
        mime_type=string,
        size_bytes={"type": "integer"},
        upload_status=string,
        parse_status=string,
        parse_error={"type": ["string", "null"]},
        page_count={"type": ["integer", "null"]},
        truncated={"type": "boolean"},
        preview_url={"type": ["string", "null"]},
        download_center_record_id={"type": ["string", "null"]},
        created_at=string,
        expires_at=string,
    )
    download_task = _object(
        download_center_record_id=string,
        direction={"type": "integer", "enum": [-1]},
        status=string,
        file_name=string,
        format=string,
        notify_file_center={"type": "boolean"},
    )
    qa_feedback = _object(
        qa_id=string,
        rating={"type": ["string", "null"], "enum": ["up", "down", None]},
        reasons={"type": "array", "items": string},
        comment={"type": ["string", "null"]},
        updated_at=string,
    )
    agent_brief = _object(agent_id=string, name=string, subtitle=string)
    return {
        "APIError": error,
        "ErrorEnvelope": _object(error=_ref("APIError")),
        "Citation": citation,
        "AssistantAgentBrief": agent_brief,
        "AssistantAgents": _object(
            default_agent_id=string,
            language=string,
            agents=objects,
        ),
        "AssistantFile": assistant_file,
        "DownloadCenterTask": download_task,
        "QAFeedback": qa_feedback,
        "Answer": _object(
            blocks={
                "type": "array",
                "items": _object(
                    kind={"type": "string", "enum": ["text", "reference", "image", "video"]},
                    text=string,
                    resource_id=string,
                    reference_id=string,
                    title=string,
                    media={"anyOf": [_ref("MediaResource"), {"type": "null"}]},
                    unavailable={"type": "boolean"},
                ),
            },
            citations={"type": "array", "items": _ref("Citation")},
            retrieval_status={"type": "string", "enum": ["found", "not_found", "unavailable", "limited"]},
        ),
        "QA": qa,
        "MediaResource": _object(
            resource_id=string,
            url={"type": "string", "description": "原样使用返回URL；可能是同源签名路径或外部视频"},
            kind=string,
            mime_type=string,
            title={"type": ["string", "null"]},
            reference_id=string,
        ),
        "Analysis": analysis,
        "Health": _object(
            **{k: v for k, v in job.items() if k not in {"result", "rule_version", "language"}},
            job_id=string,
            order_no=string,
            rule_version={
                "type": "string",
                "description": "规则版本（现行 health-v3），与 report.rule_version 严格一致",
            },
            language={"type": "string", "description": "创建作业时解析的报告正文语言"},
            report={
                "anyOf": [
                    {
                        "type": "object",
                        "description": (
                            "完成态返回 health-v3 报告正文，含 overview、overall_health_score、"
                            "conclusion、7 项 indicators、5 维 radar、curves、health_metrics、"
                            "completeness 与 source_summary"
                        ),
                        "additionalProperties": True,
                    },
                    {"type": "null"},
                ]
            },
        ),
        "Clarification": _object(
            type={"type": "string", "const": "clarification"},
            code=string,
            message=string,
            missing_fields={"type": "array", "items": string},
        ),
        "FAQ": _object(question_id=string, question=string, answer=string, format=string, language=string),
        "QAList": _object(count={"type": "integer"}, questions=objects),
        "Conversation": conversation,
        "ConversationList": _object(
            count={"type": "integer"}, conversations={"type": "array", "items": _ref("Conversation")}
        ),
        "Deleted": _object(type={"type": "string", "const": "conversation_deleted"}, conversation_id=string),
        "Catalog": _object(entries=objects),
        "Recommendations": _object(recommendations=objects),
        "Shortcuts": _object(count={"type": "integer"}, shortcuts=objects),
        "Banners": _object(count={"type": "integer"}, banners=objects),
        "Starters": {
            **_object(
                agent_id=string,
                language=string,
                starters_version=string,
                greeting=string,
                supporting_text=string,
                opening=string,
                suggested_questions={
                    "type": "array",
                    "items": {
                        "anyOf": [
                            string,
                            {**_object(id=string, text=string), "required": ["id", "text"]},
                        ]
                    },
                },
                disclaimer=string,
                opening_source=string,
                suggested_source=string,
            ),
            "required": ["opening", "suggested_questions"],
        },
        "OrderAccess": _object(
            order_no=string, accessible={"type": "boolean", "const": True}, scope_fingerprint=string
        ),
        # 站点列表：范围由会话身份推导，`total` 恒为 null —— 上游那个计数是租户口径，
        # 与「本人可见」不是同一个集合，给出去就会被当成"你有多少个"。
        "Sites": _object(
            sites={
                "type": "array",
                "items": _object(id=string, name=string, address=string),
            },
            shown={"type": "integer"},
            total={"type": ["integer", "null"]},
            scope={"type": "string", "const": "own_orders"},
            scope_truncated={"type": "boolean"},
            limited={"type": "boolean"},
            empty={"type": ["string", "null"], "enum": ["none_visible", "no_match", None]},
            source=string,
            observed_at=string,
        ),
    }


def _response(name: str, description: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": _ref(name)}}}


def public_schema(app: FastAPI) -> dict[str, Any]:
    routes = [
        r
        for r in app.routes
        if isinstance(r, APIRoute) and any((m.lower(), r.path) in OPERATIONS for m in r.methods)
    ]
    document = get_openapi(
        title="AI-Ops 用户接口", version=app.version, openapi_version="3.1.0", routes=routes
    )
    document["paths"] = {
        path: {method: operation for method, operation in item.items() if (method, path) in OPERATIONS}
        for path, item in document["paths"].items()
    }
    document["info"]["description"] = DESCRIPTION
    # 固定公开入口，不从客户端Host头、配置或数据库导出服务器地址。
    document["servers"] = [
        {"url": "https://api.mall.qushiyun.com", "description": "现役API；调用业务需授权账号"}
    ]
    components = document.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    for model in schemas.values():
        model.pop("description", None)
    schemas.update(_schemas())
    components["securitySchemes"] = {
        "ConsumerSession": {
            "type": "apiKey",
            "in": "header",
            "name": "third-session",
            "description": "C端正常登录态；配合consumer入口；不同时发送Bearer",
        },
        "OperatorBearer": {
            "type": "http",
            "scheme": "bearer",
            "description": "公司个人access_token；配合operator入口；不同时发送third-session",
        },
    }
    for (method, path), (tag, summary, name) in OPERATIONS.items():
        operation = document["paths"][path][method]
        operation.update(summary=summary, description=summary, tags=[tag])
        # 公网代理翻译会话/来源字段，不能把内部依赖的Header原样交给测试同事。
        operation["parameters"] = [
            p
            for p in operation.get("parameters", [])
            if p["in"] != "header" or p["name"].lower() in {"range", "last-event-id"}
        ]
        operation["security"] = [] if name == "Media" else [{"ConsumerSession": []}, {"OperatorBearer": []}]
        if name != "Media":
            operation["parameters"].extend(
                [
                    {
                        "name": "X-Business-Entry",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string", "enum": ["consumer", "operator"]},
                        "description": "与所选身份一致；后台AI抽屉与站点分析仅支持operator",
                    },
                    {
                        "name": "Accept-Language",
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string", "default": "zh"},
                        "description": "App语言："
                        + ", ".join(SUPPORTED_LANGUAGES)
                        + "；遵循HTTP语言协商，结果language以实际返回为准",
                    },
                ]
            )
        responses = {
            "default": _response("ErrorEnvelope", "请求拒绝或服务异常；按error.code判断，不按文案猜测"),
            "422": _response("ErrorEnvelope", "参数校验失败：INVALID_REQUEST"),
        }
        if name == "Media":
            media = {
                "description": "签名媒体字节；无需个人请求头",
                "content": {
                    mime: {"schema": {"type": "string", "format": "binary"}}
                    for mime in sorted(ALLOWED_MEDIA_TYPES)
                },
            }
            responses.update(
                {
                    "200": media,
                    "206": media,
                    "403": {"description": "签名或授权失效；空正文"},
                    "416": {"description": "Range范围无效；空正文，Content-Range给出总长度"},
                    "404": {"description": "资源不存在或媒体未配置；可能为空正文或JSON错误信封"},
                }
            )
        elif name == "QAEventStream":
            responses["200"] = {
                "description": (
                    "SSE实时事件流（run_started/step_started/step_finished/message_delta/"
                    "message_replace/citations/message_done/run_failed/run_cancelled/ping）"
                ),
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            }
        elif name == "FilePreview":
            responses["200"] = {
                "description": "规范化后的图片缩略图（JPEG 二进制流）",
                "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}},
            }
        elif name == "Assistant":
            responses["200"] = {
                "description": "同步答案/补充信息，不轮询",
                "content": {"application/json": {"schema": {"anyOf": [_ref("FAQ"), _ref("Clarification")]}}},
            }
            responses["202"] = {
                "description": "已受理；按type和对应ID轮询或订阅stream_url；不等于完成",
                "content": {"application/json": {"schema": {"anyOf": [_ref("QA"), _ref("Analysis")]}}},
            }
            operation["description"] = (
                "普通WF01不带shortcut_code。管家Java提交question（可选order_no/attachment_ids/agent_id），"
                "返回type=qa/qa_id/stream_url；分析为type=analysis/analysis_id。缺订单可200澄清。"
            )
            operation["requestBody"]["content"]["application/json"]["examples"] = {
                "knowledge": {"summary": "C端知识问答", "value": {"question": "怎么开始充电？"}},
                "order": {"summary": "C端本人订单", "value": {"question": "我上一个订单现在什么情况"}},
                "operator": {
                    "summary": "管家单订单，替换成有权限订单",
                    "value": {"question": "检查这张订单", "order_no": "ORDER-EXAMPLE-001"},
                },
            }
        else:
            if method == "post" and path == "/v1/assistant/files":
                code = "201"
            elif method == "post" and (
                (name in {"Health", "Analysis"} and not path.endswith("/cancel"))
                or path.endswith("/regenerate")
            ):
                code = "202"
            else:
                code = "200"
            responses[code] = _response(
                name,
                "读取status判断是否终态；completed/inconclusive后核正文"
                if name in {"QA", "Health", "Analysis"}
                else "成功响应",
            )
        operation["responses"] = responses
        if path.startswith("/v1/standard/analyses") or path in OPERATOR_ONLY_PATHS:
            operation["security"] = [{"OperatorBearer": []}]
            operation["parameters"][-2]["schema"]["enum"] = ["operator"]
    # FastAPI附带默认422模型；本服务统一错误信封不用它，避免导入器显示错误形状。
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    return document


def install_public_docs(app: FastAPI) -> None:
    """只注册两个无业务副作用的公开GET；规范在全部路由注册后生成。"""
    schema: dict[str, Any] | None = None

    def document() -> dict[str, Any]:
        nonlocal schema
        if schema is None:
            schema = public_schema(app)
        return schema

    @app.get("/v1/openapi.json", include_in_schema=False)
    def openapi_document() -> JSONResponse:
        return JSONResponse(document(), headers={"Cache-Control": "no-cache"})

    @app.get("/v1/docs", include_in_schema=False)
    def api_docs() -> HTMLResponse:
        spec = document()
        sections = []
        for (method, path), (_, summary, _) in OPERATIONS.items():
            operation = spec["paths"][path][method]
            sections.append(
                f"<details><summary>{method.upper()} {html.escape(path)} — {html.escape(summary)}</summary>"
                f"<pre>{html.escape(json.dumps(operation, ensure_ascii=False, indent=2))}</pre></details>"
            )
        body = (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>AI-Ops 用户接口</title><style>body{max-width:1050px;margin:32px auto;padding:0 20px;"
            "font:16px/1.6 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f5f7;"
            "padding:16px}summary{cursor:pointer;padding:12px 0}a{color:#075db7}</style>"
            '<h1>AI-Ops 用户接口</h1><p><a href="/v1/openapi.json">'
            "OpenAPI JSON（导入 Apifox / Postman）</a></p>"
            "<p>在接口测试工具中选择“导入 → OpenAPI/Swagger → URL”，粘贴上方链接。"
            "本页用于查阅，不保存凭据、不发送业务请求。展开接口查看参数、请求体、响应；"
            "字段约束在下方 schemas 中按名称查找。</p>"
            f"<pre>{html.escape(DESCRIPTION)}</pre>"
            + "".join(sections)
            + "<details><summary>schemas：请求与响应字段</summary><pre>"
            + html.escape(json.dumps(spec["components"], ensure_ascii=False, indent=2))
            + "</pre></details></html>"
        )
        return HTMLResponse(
            body,
            headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )
