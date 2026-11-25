"""公司对象存储（cloud-upms）与下载中心台账（cloud-mall-admin）零改码适配客户端。

严格遵循既有公司源码行为：
- 上传文件调用 ``POST <upms_base>/file/upload``，固定 ``fileType=doc`` 以绕开公司租户图片水印；
  响应为无 ``R`` 信封的 JSON（``{"link": "https://..."}``）。
- 登记台账调用 ``POST <mall_base>/importRecord/saveImportRecord`` 与
  ``PUT <mall_base>/importRecord/updateImportRecord``：
  - 上传默认 ``exportType=3101``（不在公司既有 ``EXPORT_TYPES`` 内 ⇒ 恒落导入中心 ``direction=1``）；
  - 下载/导出默认复用 ``exportType=10086``（在公司既有 ``EXPORT_TYPES`` 内 ⇒ 恒落导出中心 ``direction=-1``）；
  - JSON 序列化固定 ``exportType`` 字段在 ``direction`` 之前，消除 Jackson 顺序漂移；
  - 字段沿用公司现有拼写 ``sussessCount`` / ``failCount`` / ``resultDetail``。
"""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.common.bounded_http import (
    JSON_CONTENT_TYPE,
    TENANT_ID_HEADER,
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    join_url,
    request_json,
)
from aiops_diagnostics.common.scalar_rules import now_utc

_LOGGER = logging.getLogger("aiops.operator.download_center")

_POLICY_KEYWORDS = (
    "allowed",
    "suffix",
    "extension",
    "后缀",
    "文件类型",
    "格式不支持",
    "不允许上传",
    "maximum upload size",
    "max upload size",
    "filesizelimitexceeded",
    "size limit",
    "文件过大",
    "大小超过",
)


class DownloadCenterError(RuntimeError):
    """公司对象存储或下载中心台账调用失败（携带稳定对外错误码与状态码）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 502,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable


def encode_multipart_form(
    *,
    fields: list[tuple[str, str]],
    file_field: str,
    file_name: str,
    file_bytes: bytes,
    content_type: str,
) -> tuple[str, bytes]:
    """构造标准 RFC 7578 ``multipart/form-data`` 请求体（不依赖第三方库）。"""
    boundary = "----AIOpsBoundary" + secrets.token_hex(16)
    safe_name = file_name.replace('"', "_").replace("\r", "").replace("\n", "")
    parts: list[bytes] = []
    for key, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def _is_policy_rejection(failure: HttpFailure) -> bool:
    if failure.status == 413:
        return True
    detail = (failure.detail or "").lower()
    if failure.status in {400, 415, 422} and any(word in detail for word in _POLICY_KEYWORDS):
        return True
    return any(word in detail for word in _POLICY_KEYWORDS)


def _ordered_record_json(
    *,
    export_type: int,
    direction: int,
    extra: dict[str, Any],
) -> bytes:
    """序列化台账 JSON，强制 ``exportType`` 位于 ``direction`` 之前。"""
    ordered: dict[str, Any] = {
        "exportType": int(export_type),
        "direction": int(direction),
    }
    for key, value in extra.items():
        if key not in {"exportType", "direction"}:
            ordered[key] = value
    return json.dumps(ordered, ensure_ascii=False, sort_keys=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class DownloadCenterClient:
    upms_base_url: str
    mall_base_url: str
    upload_export_type: int = 3101
    download_export_type: int = 10086
    timeout_seconds: float = 15.0

    @classmethod
    def from_settings(cls, settings: Any) -> DownloadCenterClient:
        return cls(
            upms_base_url=str(getattr(settings, "download_center_upms_base_url", "") or "").strip(),
            mall_base_url=str(getattr(settings, "download_center_mall_base_url", "") or "").strip(),
            upload_export_type=int(getattr(settings, "download_center_upload_export_type", 3101) or 3101),
            download_export_type=int(
                getattr(settings, "download_center_download_export_type", 10086) or 10086
            ),
            timeout_seconds=float(getattr(settings, "download_center_timeout_seconds", 15.0) or 15.0),
        )

    def is_configured(self) -> bool:
        return bool(self.upms_base_url and self.mall_base_url)

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise DownloadCenterError(
                "DOWNLOAD_CENTER_UNAVAILABLE",
                "下载中心暂不可用，请稍后重试",
                http_status=502,
                retryable=True,
            )

    def _unavailable(self, _: HttpFailure) -> DownloadCenterError:
        return DownloadCenterError(
            "DOWNLOAD_CENTER_UNAVAILABLE",
            "下载中心暂不可用，请稍后重试",
            http_status=502,
            retryable=True,
        )

    def _upload_http_error(self, failure: HttpFailure) -> DownloadCenterError:
        if _is_policy_rejection(failure):
            return DownloadCenterError(
                "UPSTREAM_FILE_POLICY_REJECTED",
                "上游文件存储策略拒绝了该文件类型或大小，请联系运维核对上传配置",
                http_status=413 if failure.status == 413 else 400,
                retryable=False,
            )
        return self._unavailable(failure)

    def upload_file(
        self,
        *,
        file_bytes: bytes,
        file_name: str,
        content_type: str,
        tenant_id: str,
        directory: str,
    ) -> str:
        """上传文件至公司 ``cloud-upms`` 对象存储（固定 ``fileType=doc`` 规避水印）。"""
        self._require_configured()
        multipart_type, body = encode_multipart_form(
            fields=[("dir", directory), ("fileType", "doc")],
            file_field="file",
            file_name=file_name,
            file_bytes=file_bytes,
            content_type=content_type,
        )
        mapping = ErrorMapping(
            auth_rejected=self._unavailable,
            http_error=self._upload_http_error,
            unavailable=self._unavailable,
            invalid_body=self._unavailable,
            invalid_envelope=self._unavailable,
        )
        payload = request_json(
            RequestSpec(
                url=join_url(self.upms_base_url, "/file/upload"),
                method="POST",
                headers={
                    "from": "Y",
                    TENANT_ID_HEADER: tenant_id,
                    "Content-Type": multipart_type,
                },
                body=body,
                timeout=self.timeout_seconds,
                max_read_bytes=65536,
                follow_redirects=False,
            ),
            mapping=mapping,
        )
        # cloud-upms 返回无 R 信封的 JSON {"link":"https://..."}；若返回错误码也需拦截
        if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200):
            msg = str(payload.get("msg") or payload.get("message") or "")
            if any(word in msg.lower() for word in _POLICY_KEYWORDS):
                raise DownloadCenterError(
                    "UPSTREAM_FILE_POLICY_REJECTED",
                    "上游文件存储策略拒绝了该文件类型或大小，请联系运维核对上传配置",
                    http_status=400,
                    retryable=False,
                )
            raise DownloadCenterError(
                "DOWNLOAD_CENTER_UNAVAILABLE",
                "下载中心上传失败，请稍后重试",
                http_status=502,
                retryable=True,
            )
        link = str(payload.get("link") or "").strip() if isinstance(payload, dict) else ""
        if not link.startswith("https://"):
            raise DownloadCenterError(
                "DOWNLOAD_CENTER_UNAVAILABLE",
                "下载中心未返回有效的 HTTPS 文件地址",
                http_status=502,
                retryable=True,
            )
        return link

    def save_import_record(
        self,
        *,
        tenant_id: str,
        user_id: str,
        username: str,
        file_name: str,
        export_type: int,
        direction: int,
        state: int = 0,
        result: str = "",
        result_detail: str = "",
        success_count: int = 0,
        fail_count: int = 0,
    ) -> str:
        """在公司 ``cloud-mall-admin`` 创建一条文件中心台账记录。"""
        self._require_configured()
        body = _ordered_record_json(
            export_type=export_type,
            direction=direction,
            extra={
                "fileName": file_name[:200],
                "state": int(state),
                "userId": user_id,
                "username": username,
                "result": result,
                "resultDetail": result_detail,
                "sussessCount": int(success_count),
                "failCount": int(fail_count),
            },
        )
        mapping = ErrorMapping(
            auth_rejected=self._unavailable,
            http_error=self._unavailable,
            unavailable=self._unavailable,
            invalid_body=self._unavailable,
            invalid_envelope=self._unavailable,
        )
        envelope = request_json(
            RequestSpec(
                url=join_url(self.mall_base_url, "/importRecord/saveImportRecord"),
                method="POST",
                headers={
                    "from": "Y",
                    TENANT_ID_HEADER: tenant_id,
                    "Content-Type": JSON_CONTENT_TYPE,
                },
                body=body,
                timeout=self.timeout_seconds,
                max_read_bytes=65536,
                follow_redirects=False,
            ),
            mapping=mapping,
        )
        if not isinstance(envelope, dict) or envelope.get("code") not in (0, 200):
            raise DownloadCenterError(
                "DOWNLOAD_CENTER_UNAVAILABLE",
                "下载中心台账创建失败，请稍后重试",
                http_status=502,
                retryable=True,
            )
        data = envelope.get("data")
        record_id = str(data.get("id") or "").strip() if isinstance(data, dict) else str(data or "").strip()
        if not record_id:
            raise DownloadCenterError(
                "DOWNLOAD_CENTER_UNAVAILABLE",
                "下载中心台账未返回记录编号",
                http_status=502,
                retryable=True,
            )
        return record_id

    def update_import_record(
        self,
        *,
        tenant_id: str,
        record_id: str,
        export_type: int,
        direction: int,
        state: int,
        file_name: str = "",
        result: str = "",
        result_detail: str = "",
        success_count: int = 0,
        fail_count: int = 0,
        cost_time: int = 0,
    ) -> None:
        """更新公司文件中心台账状态与最终 HTTPS 下载链接。"""
        self._require_configured()
        extra: dict[str, Any] = {
            "id": record_id,
            "state": int(state),
            "result": result,
            "resultDetail": result_detail,
            "sussessCount": int(success_count),
            "failCount": int(fail_count),
            "costTime": max(0, int(cost_time)),
        }
        if file_name:
            extra["fileName"] = file_name[:200]
        body = _ordered_record_json(
            export_type=export_type,
            direction=direction,
            extra=extra,
        )
        mapping = ErrorMapping(
            auth_rejected=self._unavailable,
            http_error=self._unavailable,
            unavailable=self._unavailable,
            invalid_body=self._unavailable,
            invalid_envelope=self._unavailable,
        )
        envelope = request_json(
            RequestSpec(
                url=join_url(self.mall_base_url, "/importRecord/updateImportRecord"),
                method="PUT",
                headers={
                    "from": "Y",
                    TENANT_ID_HEADER: tenant_id,
                    "Content-Type": JSON_CONTENT_TYPE,
                },
                body=body,
                timeout=self.timeout_seconds,
                max_read_bytes=65536,
                follow_redirects=False,
            ),
            mapping=mapping,
        )
        if not isinstance(envelope, dict) or envelope.get("code") not in (0, 200):
            raise DownloadCenterError(
                "DOWNLOAD_CENTER_UNAVAILABLE",
                "下载中心台账更新失败，请稍后重试",
                http_status=502,
                retryable=True,
            )

    def upload_and_register_import(
        self,
        *,
        tenant_id: str,
        user_id: str,
        username: str,
        file_name: str,
        file_bytes: bytes,
        content_type: str,
    ) -> tuple[str, str]:
        """两段式附件上传与导入中心登记，返回 ``(storage_url, record_id)``。"""
        self._require_configured()
        started = time.monotonic()
        ym = now_utc().strftime("%Y%m")
        display_name = f"AI排查附件_{file_name}"[:200]
        record_id = self.save_import_record(
            tenant_id=tenant_id,
            user_id=user_id,
            username=username,
            file_name=display_name,
            export_type=self.upload_export_type,
            direction=1,
            state=0,
            result="正在上传排查附件",
        )
        try:
            storage_url = self.upload_file(
                file_bytes=file_bytes,
                file_name=file_name,
                content_type=content_type,
                tenant_id=tenant_id,
                directory=f"aiops/chat/{ym}/",
            )
        except DownloadCenterError as exc:
            cost_ms = int((time.monotonic() - started) * 1000)
            with contextlib.suppress(DownloadCenterError):
                self.update_import_record(
                    tenant_id=tenant_id,
                    record_id=record_id,
                    export_type=self.upload_export_type,
                    direction=1,
                    state=3,
                    file_name=display_name,
                    result=f"上传失败({exc.code})",
                    fail_count=1,
                    cost_time=cost_ms,
                )
            raise
        cost_ms = int((time.monotonic() - started) * 1000)
        self.update_import_record(
            tenant_id=tenant_id,
            record_id=record_id,
            export_type=self.upload_export_type,
            direction=1,
            state=1,
            file_name=display_name,
            result="上传成功",
            result_detail=storage_url,
            success_count=1,
            cost_time=cost_ms,
        )
        return storage_url, record_id

    def register_download_export(
        self,
        *,
        tenant_id: str,
        user_id: str,
        username: str,
        file_name: str,
        storage_url: str,
    ) -> str:
        """向公司文件中心「导出中心」（``direction=-1``）登记可下载文件记录。"""
        self._require_configured()
        if not storage_url.startswith("https://"):
            raise DownloadCenterError(
                "DOWNLOAD_CENTER_UNAVAILABLE",
                "文件下载链接无效，请重新上传后再试",
                http_status=502,
                retryable=True,
            )
        started = time.monotonic()
        record_id = self.save_import_record(
            tenant_id=tenant_id,
            user_id=user_id,
            username=username,
            file_name=file_name[:200],
            export_type=self.download_export_type,
            direction=-1,
            state=0,
            result="正在准备下载文件",
        )
        cost_ms = int((time.monotonic() - started) * 1000)
        self.update_import_record(
            tenant_id=tenant_id,
            record_id=record_id,
            export_type=self.download_export_type,
            direction=-1,
            state=1,
            file_name=file_name[:200],
            result="导出成功",
            result_detail=storage_url,
            success_count=1,
            cost_time=cost_ms,
        )
        return record_id

    def upload_and_register_export(
        self,
        *,
        tenant_id: str,
        user_id: str,
        username: str,
        file_name: str,
        file_bytes: bytes,
        content_type: str,
    ) -> tuple[str, str]:
        """上传排查报告并登记到公司文件中心「导出中心」（``direction=-1``）。"""
        self._require_configured()
        started = time.monotonic()
        ym = now_utc().strftime("%Y%m")
        record_id = self.save_import_record(
            tenant_id=tenant_id,
            user_id=user_id,
            username=username,
            file_name=file_name[:200],
            export_type=self.download_export_type,
            direction=-1,
            state=0,
            result="正在生成排查报告",
        )
        try:
            storage_url = self.upload_file(
                file_bytes=file_bytes,
                file_name=file_name,
                content_type=content_type,
                tenant_id=tenant_id,
                directory=f"aiops/export/{ym}/",
            )
        except DownloadCenterError as exc:
            cost_ms = int((time.monotonic() - started) * 1000)
            with contextlib.suppress(DownloadCenterError):
                self.update_import_record(
                    tenant_id=tenant_id,
                    record_id=record_id,
                    export_type=self.download_export_type,
                    direction=-1,
                    state=3,
                    file_name=file_name[:200],
                    result=f"导出失败({exc.code})",
                    fail_count=1,
                    cost_time=cost_ms,
                )
            _LOGGER.warning("download_center_export_failed code=%s", exc.code)
            raise
        cost_ms = int((time.monotonic() - started) * 1000)
        self.update_import_record(
            tenant_id=tenant_id,
            record_id=record_id,
            export_type=self.download_export_type,
            direction=-1,
            state=1,
            file_name=file_name[:200],
            result="导出成功",
            result_detail=storage_url,
            success_count=1,
            cost_time=cost_ms,
        )
        return storage_url, record_id
