"""Gateway HTTP 请求体 Pydantic 模型唯一声明（原位于 gateway_api.py 顶部）。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aiops_diagnostics.identity.order_identity import ORDER_ID_ATOM
from aiops_diagnostics.operator import operator_tools
from aiops_diagnostics.publication.agent_lifecycle import AgentConfig


class ChargingStatusDelegationRequest(BaseModel):
    """委派入口的请求体：**空**。

    环境、appId、agentId、sid 都不接受：``extra="forbid"`` 下多一个字段就是 422。
    """

    model_config = ConfigDict(extra="forbid")


class NodeClaimRequest(BaseModel):
    """节点领取端点的请求体：同样为空，用于拒绝任何覆盖身份或业务范围的尝试。"""

    model_config = ConfigDict(extra="forbid")


class OperatorToolAuthorizationRequest(BaseModel):
    """领工具授权时的请求体：**只有**模型提议的参数，别的一律 422。"""

    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, str | list[str]] = Field(default_factory=dict, max_length=8)


class NodeLoopCompletionRequest(BaseModel):
    """整轮终局通知的请求体。只有状态与一个短原因码，**没有业务数据、没有身份字段**。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    reason: str | None = Field(
        default=None, max_length=96, pattern=r"^([a-z0-9_]*|tool_refused:[A-Za-z0-9_]{1,80})$"
    )


class OperatorStreamChunkRequest(BaseModel):
    """执行节点推送最终答复阶段增量文本分片的内部受控请求体。"""

    model_config = ConfigDict(extra="forbid")

    delta: str = Field(min_length=1, max_length=2000)
    chunk_index: int | None = Field(default=None, ge=0)


class HealthReportJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str = Field(min_length=1, max_length=128, pattern=rf"^{ORDER_ID_ATOM}$")


class AssistantQuestionRequest(BaseModel):
    """统一助手问答入口请求体；``order_no``、``conversation_id``、``shortcut_code`` 均可选。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    photo_id: str | None = Field(default=None, pattern=r"^photo_[a-f0-9]{32}$")
    order_no: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=rf"^{ORDER_ID_ATOM}$",
    )
    conversation_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=64,
        pattern=r"^conv_[A-Za-z0-9]{1,57}$",
    )
    shortcut_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
    )
    agent_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
    )
    attachment_ids: list[str] | None = Field(default=None)


class AssistantQuestionExportRequest(BaseModel):
    """将已完成排查轮次导出为报告并写入公司下载中心的请求体。"""

    model_config = ConfigDict(extra="forbid")

    format: Literal["docx", "md"] = "docx"


class AssistantQuestionFeedbackRequest(BaseModel):
    """回答点赞/点踩反馈请求体；``rating=None`` 表示取消已有评价。"""

    model_config = ConfigDict(extra="forbid")

    rating: Literal["up", "down"] | None = None
    reasons: list[str] = Field(default_factory=list, max_length=5)
    comment: str | None = Field(default=None, max_length=500)

    @field_validator("reasons")
    @classmethod
    def _clean_reasons(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            text = " ".join(str(item).split()).strip()
            if not text or len(text) > 64:
                raise ValueError("each reason must be 1..64 non-blank characters")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned


class AssistantQuestionRegenerateRequest(BaseModel):
    """最后一轮原地重新生成请求体（空对象，拒绝未声明字段）。"""

    model_config = ConfigDict(extra="forbid")


class OperationsAnalysisRequest(BaseModel):
    """站点集合 + 时间窗 + 报告卡类型（failure_rate/offline/fault_code/payment）。"""

    model_config = ConfigDict(extra="forbid")

    card: str = Field(default="failure_rate", pattern=r"^(failure_rate|offline|fault_code|payment)$")
    site_ids: list[str] | None = Field(default=None, max_length=1000)
    window_start: datetime
    window_end: datetime


class OperatorPublishRequest(BaseModel):
    """运营自助发布请求体：只接受已声明绑定的 ``app_id``。"""

    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=1, max_length=64)


class DifyPublishRequest(BaseModel):
    """Dify 发布请求体：只接受已绑定 ``app_id``，其余配置由服务端登记表决定。"""

    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class DifyRetractRequest(BaseModel):
    """撤回一个已绑定 app 的正式版本；仅接受 ``app_id``。"""

    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class DifyRestoreRequest(DifyPublishRequest):
    version_no: int = Field(ge=1, strict=True)


class OperatorPreviewTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1, max_length=64)
    provider_type: Literal["api"]
    tool_name: str = Field(min_length=1, max_length=64)
    enabled: bool = Field(default=True, strict=True)

    @field_validator("tool_name")
    @classmethod
    def _declared_tool(cls, value: str) -> str:
        if value not in operator_tools.names():
            raise ValueError("undeclared operator tool")
        return value


class OperatorPreviewDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pre_prompt: str = Field(min_length=1, max_length=8000)
    tools: list[OperatorPreviewTool] = Field(max_length=len(operator_tools.names()))


class OperatorTrialCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=1, max_length=64)


class DifyTrialRequest(BaseModel):
    """试用已登记应用的草稿，执行路线由服务端绑定决定。"""

    model_config = ConfigDict(extra="forbid")
    order_no: str = Field(default="", max_length=128, pattern=rf"^({ORDER_ID_ATOM})?$")
    app_id: str = Field(min_length=1, max_length=64, pattern=r"^[0-9a-fA-F-]{8,64}$")
    question: str = Field(default="check order status", min_length=1, max_length=4000)
    operator_preview: OperatorPreviewDraft | None = None
    conversation_id: str | None = Field(default=None, pattern=r"^conv_[a-f0-9]{32}$")


class ManagedTrialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    order_no: str = Field(default="", max_length=128, pattern=rf"^({ORDER_ID_ATOM})?$")
    question: str = Field(min_length=1, max_length=4000)


class ManagedTrialResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    trial_id: str = Field(pattern=r"^tr_[a-f0-9]{32}$")


class NodeCapabilitiesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemas: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(min_length=1, max_length=10)
    tool_contract: str = Field(min_length=1, max_length=64)
    tools_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class FAQAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z]+\.[a-z0-9-]+\.q[0-9]{3}$")


class AgentConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_type: str = Field(min_length=1, max_length=32)
    prompt: str = Field(min_length=1, max_length=8000)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=20)
    model: str = Field(min_length=1, max_length=128)
    output_contract: str = Field(min_length=1, max_length=64)
    opening_questions: list[str] = Field(default_factory=list, max_length=20)
    quick_commands: list[str] = Field(default_factory=list, max_length=20)

    def to_config(self) -> AgentConfig:
        return AgentConfig(
            agent_type=self.agent_type,
            prompt=self.prompt,
            knowledge_base_ids=tuple(self.knowledge_base_ids),
            model=self.model,
            output_contract=self.output_contract,
            opening_questions=tuple(self.opening_questions),
            quick_commands=tuple(self.quick_commands),
        )


class AgentCreateRequest(AgentConfigRequest):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1000)


class AgentUpdateRequest(AgentCreateRequest):
    expected_revision: int = Field(ge=1)


class AgentActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class KnowledgeSourceRequest(BaseModel):
    """Dify 知识源配置项；``knowledge_base_ids`` 去重且校验合法字符。"""

    model_config = ConfigDict(extra="forbid")

    knowledge_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    tenant_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    knowledge_base_ids: list[str] = Field(min_length=1, max_length=20)
    display_name: str = Field(default="", max_length=200)

    @field_validator("knowledge_base_ids")
    @classmethod
    def _valid_ids(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            candidate = item.strip()
            if not candidate or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", candidate):
                raise ValueError("knowledge_base_ids contains an invalid id")
            if candidate not in cleaned:
                cleaned.append(candidate)
        return cleaned


class ConversationCreateRequest(BaseModel):
    """创建绑定当前调用者、入口与智能体版本的会话。"""

    model_config = ConfigDict(extra="forbid")

    agent_version_key: str = Field(
        min_length=6,
        max_length=80,
        pattern=r"^agt_[A-Za-z0-9]{8,64}#v\d{1,6}$",
    )


class ConversationRenameRequest(BaseModel):
    """修改会话标题请求体（1–80 字符，禁止空白串）。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=80)

    @field_validator("title")
    @classmethod
    def _non_blank_title(cls, value: str) -> str:
        cleaned = " ".join(value.split()).strip()
        if not cleaned or len(cleaned) > 80:
            raise ValueError("title must be 1..80 non-blank characters")
        return cleaned


class ShortcutFieldsRequest(BaseModel):
    """快捷动作草稿公共字段（创建与更新共用）。"""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1, max_length=32)
    requires_order: bool = False
    sort_order: int = Field(default=100, ge=0, le=9999)
    labels: dict[str, str] = Field(min_length=1, max_length=6)
    descriptions: dict[str, str] = Field(default_factory=dict, max_length=6)
    question_templates: dict[str, str] = Field(default_factory=dict, max_length=6)
    target_agent_version: str | None = Field(
        default=None,
        min_length=6,
        max_length=80,
        pattern=r"^agt_[A-Za-z0-9]{8,64}#v\d{1,6}$",
    )
    jump_path: str | None = Field(
        default=None,
        max_length=512,
        pattern=r"^/(?:[^/]\S*)?$",
    )
    image_url: str | None = Field(
        default=None,
        max_length=512,
        pattern=r"^https?://\S+$",
    )


class ShortcutCreateRequest(ShortcutFieldsRequest):
    business_entry: str = Field(min_length=1, max_length=32, pattern=r"^(consumer|operator)$")
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class ShortcutUpdateRequest(ShortcutFieldsRequest):
    expected_revision: int = Field(ge=1)


class ShortcutActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class ShortcutRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    version_no: int = Field(ge=1)
