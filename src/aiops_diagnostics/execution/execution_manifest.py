"""受控**执行配置清单**的身份读取（M3-GATEWAY-01）。

这是 Gateway 侧唯一的期望值来源。三条约束与节点侧同源，不是新协议：

1. **身份 = 清单原始字节的 sha256**。不对解析后的对象做序列化再算摘要 —— 那会把
   JSON 归一化差异引成身份差异，让两端算出不同的值。
2. **不启用就不存在**：路径为空返回 ``None``，调用方据此走「与今天逐字一致」的路径。
3. **配了就必须真的合格**：不可读、非普通文件、符号链接、权限过宽、非法 JSON、
   schema 不受支持 —— 一律抛错，由 ``validate()`` 变成**启动失败**，不降级。

清单里**没有凭据**：它只声明期望（system 消息、模型标识、有效选项、受控工具）。
``apiKey`` 之类永远不进这个文件，也不进它的摘要之外任何输出。
"""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path

#: 本片只接受这一个 schema 版本；其它值拒绝而不是忽略。
SUPPORTED_SCHEMA = "m3-exec-config/1"
OPERATOR_SCHEMA = "operator-exec-config/1"

#: 固定无历史模式。清单声明别的取值时拒绝整份清单。
FIXED_HISTORY_POLICY = "none"


class ExecutionManifestError(ValueError):
    """清单缺失、不可读或不合格。调用方应把它变成**启动失败**，而不是运行期降级。"""


@dataclass(frozen=True, slots=True)
class ExecutionManifest:
    """一份已校验的清单。只保留身份与做校验必需的字段。"""

    identity: str
    name: str
    model_key: str
    provider_key: str
    adapter_key: str
    endpoint: str
    system_messages: tuple[str, ...]
    tool_names: tuple[str, ...]
    history_policy: str


def load(path: str | Path | None) -> ExecutionManifest | None:
    """读清单。``path`` 为空 ⇒ ``None``（未启用）；任何不合格 ⇒ :class:`ExecutionManifestError`。"""
    if not path:
        return None
    declared = Path(path).expanduser().absolute()
    # 符号链接在 resolve 之前判定：否则读的是链接目标，而我们声称校验了这个路径。
    if declared.is_symlink():
        raise ExecutionManifestError("execution manifest must not be a symlink")
    try:
        info = declared.lstat()
    except OSError as exc:
        raise ExecutionManifestError("execution manifest is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ExecutionManifestError("execution manifest must be a regular file")
    # 与 agent run config 同一条纪律：里面有期望值（含 endpoint），不留给同组/他人。
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ExecutionManifestError("execution manifest must not be group/other accessible")
    try:
        raw = declared.read_bytes()
    except OSError as exc:
        raise ExecutionManifestError("execution manifest is unreadable") from exc
    if not raw:
        raise ExecutionManifestError("execution manifest is empty")
    identity = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionManifestError("execution manifest is not valid JSON") from exc
    return _parse(payload, identity)


def from_text(text: str) -> ExecutionManifest:
    """已发布版本持有的原始清单字节，身份规则与文件相同。"""
    try:
        if not text or len(text.encode("utf-8")) > 262144:
            raise ExecutionManifestError("execution manifest size is invalid")
        return _parse(json.loads(text), hashlib.sha256(text.encode("utf-8")).hexdigest())
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExecutionManifestError("execution manifest is not valid JSON") from exc


def _parse(payload: object, identity: str) -> ExecutionManifest:
    if not isinstance(payload, dict):
        raise ExecutionManifestError("execution manifest must be a JSON object")
    if payload.get("schema") not in {SUPPORTED_SCHEMA, OPERATOR_SCHEMA, "operator-conversation/1"}:
        raise ExecutionManifestError("unsupported execution manifest schema")
    if payload.get("history_policy") != FIXED_HISTORY_POLICY:
        raise ExecutionManifestError("unsupported history policy")
    model = payload.get("model")
    if not isinstance(model, dict):
        raise ExecutionManifestError("execution manifest model is missing")
    # 与节点侧**同一组必填字段**：节点的 `ExecManifest.parse` 要求 `model_options`
    # 存在（选项可比量只从它取），并要求每个工具同时声明 description 与 input_schema。
    # 少要求一项的后果是可测的：一份缺 `model_options` 的清单 Gateway 收下、节点拒绝装配，
    # 两端对「合格清单」的定义不一致 —— 身份算得再准，那份配置也跑不起来。
    # 这条一致性由 `tools/experiments/m2_close/m3node/manifest_parity.py` 用同一组样本逐条比对。
    options = payload.get("model_options")
    if not isinstance(options, dict):
        raise ExecutionManifestError("execution manifest model options are missing")
    system_messages = payload.get("system_messages")
    if not isinstance(system_messages, list) or not system_messages:
        raise ExecutionManifestError("execution manifest system messages are missing")
    if not all(isinstance(item, str) and item for item in system_messages):
        raise ExecutionManifestError("execution manifest system messages are invalid")
    tools = payload.get("tools")
    if not isinstance(tools, list) or (
        not tools and payload.get("schema") not in {OPERATOR_SCHEMA, "operator-conversation/1"}
    ):
        # 空工具集合不是合法期望，也不是拒绝手段（节点侧同一结论）。
        raise ExecutionManifestError("execution manifest declares no controlled tools")
    # 对话回合**可以**带工具（ADR-0182）：无订单不等于无能力。哪些工具能出现在哪一类
    # 运行里，由声明（`operator_tools.json` 的 `applies_to`）判，不在这里按协议名判 ——
    # 协议名说「这次怎么回答」，说不清「这次能查什么」。
    names: list[str] = []
    for item in tools:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ExecutionManifestError("execution manifest tool is invalid")
        # description / input_schema 与 name 同为必填：节点侧用 `text(t, field)` 读，
        # 缺失或空白即拒绝清单。这里对齐，不留「Gateway 收、节点拒」的缝。
        for field in ("description", "input_schema"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ExecutionManifestError(f"execution manifest tool {field} is missing")
        names.append(item["name"])
    return ExecutionManifest(
        identity=identity,
        name=_required_text(payload, "name"),
        model_key=_required(model, "model_key"),
        provider_key=_required(model, "provider_key"),
        adapter_key=_required(model, "adapter_key"),
        endpoint=_required(model, "endpoint"),
        system_messages=tuple(system_messages),
        tool_names=tuple(names),
        history_policy=FIXED_HISTORY_POLICY,
    )


def _required_text(parent: dict[str, object], key: str) -> str:
    """节点侧 `text(parent, field)` 的同一条判据：缺失、非字符串、空白都拒绝。"""
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExecutionManifestError(f"execution manifest {key} is missing")
    return value


def _required(model: dict[str, object], key: str) -> str:
    value = model.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExecutionManifestError(f"execution manifest model.{key} is missing")
    return value
