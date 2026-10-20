"""清单写入脚本的判据：它必须从**真实源码**取值，且拒绝猜。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRITER = REPO / "tools/m2_close/m3live/write_exec_manifest.py"


def _load():
    spec = importlib.util.spec_from_file_location("write_exec_manifest_under_test", WRITER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_tool_constants_are_read_from_the_node_source():
    """工具三元组必须与节点源码逐字相同 —— 描述里那个分号曾经把解析截断过。"""
    module = _load()
    tool = module._tool_constants()
    source = module.TOOL_SOURCE.read_text(encoding="utf-8")

    assert tool["name"] == "charging_status_snapshot"
    # 描述里含 `;`：按第一个分号截断会得到半句。整句必须在。
    assert tool["description"].endswith("come from the Gateway.")
    assert "Takes no arguments; the order" in tool["description"]
    assert tool["input_schema"].startswith('{"type":"object"')
    # 常量确实来自源码：源码里是若干字面量拼接，所以按**片段**核对，
    # 而不是整句出现在源码里（那会逼着源码把长描述写成一行）。
    assert '"charging_status_snapshot"' in source
    assert '"Return the charging status snapshot' in source
    assert '"conversation. Takes no arguments; the order and identity come from the Gateway."' in source


def test_identity_is_the_raw_bytes_digest(tmp_path):
    module = _load()
    payload = module.build(model_id="m", endpoint="https://e", system_message="S")
    out = tmp_path / "exec-manifest.json"
    identity = module.write(out, payload)

    assert identity == hashlib.sha256(out.read_bytes()).hexdigest()
    assert out.stat().st_mode & 0o777 == 0o600, "清单含期望值（含 endpoint），必须私有"
    # 同一份 payload 写两次 ⇒ 同一身份（写入是确定性的）。
    again = tmp_path / "again.json"
    assert (
        module.write(again, module.build(model_id="m", endpoint="https://e", system_message="S")) == identity
    )


def test_missing_inputs_are_refused_rather_than_guessed(tmp_path, monkeypatch):
    """缺一项就拒绝：清单是期望值的唯一来源，猜一个值等于把漂移固化成契约。"""
    module = _load()
    out = tmp_path / "x.json"
    monkeypatch.setattr(
        "sys.argv", ["w", "--out", str(out), "--model-id", "m", "--endpoint", "", "--system-message", "S"]
    )
    assert module.main() == 2
    assert not out.exists()


def test_model_options_are_declared_explicitly():
    """`model_options` 不是空字典：空字典会让「没验」看起来像「验过」。"""
    module = _load()
    payload = module.build(model_id="m", endpoint="https://e", system_message="S")
    assert payload["model_options"], "必须显式声明期望的选项"
    # 序列化后仍在（不是被 json 丢掉的 None 字段）。
    assert json.loads(json.dumps(payload))["model_options"] == module.MODEL_OPTIONS


@pytest.mark.parametrize("field", ["schema", "history_policy", "system_messages", "tools"])
def test_manifest_carries_the_fields_the_loader_requires(field):
    """这些字段缺一个，两侧的加载器都会拒 —— 写出来就该是可直接用的。"""
    module = _load()
    payload = module.build(model_id="m", endpoint="https://e", system_message="S")
    assert field in payload
