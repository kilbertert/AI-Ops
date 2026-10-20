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


def test_model_options_are_the_measured_platform_defaults():
    """`model_options` 必须写**平台实测下发的值**，不是猜的。

    第一次真实请求就把它证伪了：早先猜 `{"temperature": 1.0, "max_tokens": 4096}`，
    而平台不传 `configJson` 时下发的**全为空**，节点在最终边界以 `OPTIONS` 拒绝
    （`实际 temperature=null,max_tokens=null,…`）。

    所以这里断言的是「空」，而**不是**「非空就行」：一个非空的猜测值会让每一次真实
    请求都被拒，而且看起来像守卫坏了。这条测试的名字写的就是这件事 —— 是实测值，
    不是「声明过」。
    """
    module = _load()
    payload = module.build(model_id="m", endpoint="https://e", system_message="S")
    assert payload["model_options"] == {}, "平台当前不设任何旋钮；写成非空值会被节点按 OPTIONS 拒绝"
    # 序列化后仍在（不是被 json 丢掉的字段）。
    assert json.loads(json.dumps(payload))["model_options"] == {}


@pytest.mark.parametrize("field", ["schema", "history_policy", "system_messages", "tools"])
def test_manifest_carries_the_fields_the_loader_requires(field):
    """这些字段缺一个，两侧的加载器都会拒 —— 写出来就该是可直接用的。"""
    module = _load()
    payload = module.build(model_id="m", endpoint="https://e", system_message="S")
    assert field in payload
