"""真机运行器的反向判据：空/旧/后台UI不能产出假通过。"""

import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / ".claude/skills/verify-aiops-client-e2e/scripts/android_adbx.py"
adbx = types.ModuleType("android_adbx")
adbx.__file__ = str(PATH)
exec(compile(PATH.read_bytes(), str(PATH), "exec"), adbx.__dict__)


def ui(page="chat[2]", text="new answer", background="old answer"):
    return f'''<hierarchy><node package="test.app" class="android.webkit.WebView"
    text="{page}" bounds="[0,0][1080,2000]">
    <node text="{text}" package="test.app" bounds="[0,200][800,400]"/></node>
    <node package="test.app" class="android.webkit.WebView" text="other[1]" bounds="[0,0][1080,2000]">
    <node text="{background}" package="test.app" bounds="[0,200][800,400]"/></node></hierarchy>'''.encode()


def plan():
    return {
        "package": "test.app",
        "steps": [
            {
                "name": "new response",
                "page": "chat",
                "action": "tap",
                "target": {"text": "ask"},
                "expect": ["new answer"],
                "timeout": 1,
            }
        ],
    }


def test_background_page_cannot_satisfy_expected_answer():
    nodes = adbx.active_nodes(ui(text="ask", background="new answer"), "test.app", "chat")
    assert not adbx.expectations(nodes, ["new answer"])
    assert not adbx.active_nodes(ui(), "test.app", "other")


def test_user_input_and_zero_sized_nodes_do_not_count():
    xml = (
        b'<hierarchy><node package="test.app" class="android.widget.EditText" '
        b'text="answer" bounds="[0,0][100,100]"/><node package="test.app" '
        b'text="answer" bounds="[0,0][0,0]"/></hierarchy>'
    )
    assert not adbx.expectations(adbx.active_nodes(xml, "test.app", ""), ["answer"])


def test_target_must_be_unique_and_positive():
    nodes = adbx.active_nodes(ui(text="ask"), "test.app", "chat")
    assert adbx.target(nodes, {"text": "ask"}) == (400, 300)
    with pytest.raises(AssertionError):
        adbx.target(nodes, {"text": "missing"})


@pytest.mark.parametrize("fault", ["empty-expect", "shell", "unicode-input", "unknown-field", "timeout"])
def test_bad_plan_is_rejected_before_device_use(fault):
    p = plan()
    step = p["steps"][0]
    if fault == "empty-expect":
        step["expect"] = []
    if fault == "shell":
        step["action"] = "shell"
    if fault == "unicode-input":
        step.update(action="text", text="中文")
    if fault == "unknown-field":
        step["command"] = "anything"
    if fault == "timeout":
        step["timeout"] = 500
    with pytest.raises(ValueError):
        adbx.validate(p)


class FakeDevice:
    changed = False
    available = "serial device"
    performed = 0

    def __init__(self, *args):
        pass

    def call(self, *args, **kwargs):
        if args[0] == "devices":
            return self.available.encode()
        if args[-1].startswith("dumpsys package"):
            return b"versionCode=1 versionName=1.0"
        return b"test"

    def foreground(self):
        pass

    def ui(self):
        return ui(text="new answer" if self.changed else "ask")

    def capture(self, *args):
        pass

    def perform(self, *args):
        type(self).performed += 1
        type(self).changed = True


def test_receipt_requires_new_current_screen_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(adbx, "Device", FakeDevice)
    FakeDevice.changed = False
    FakeDevice.available = "serial device"
    FakeDevice.performed = 0
    r = adbx.run(plan(), tmp_path, "adbx", None)
    assert r["status"] == "PASS" and r["steps"][0]["status"] == "PASS"
    assert FakeDevice.performed == 1
    # 同一响应再次存在时不得将旧答案当新结果。
    r = adbx.run(plan(), tmp_path, "adbx", None)
    assert r["status"] == "FAIL" and FakeDevice.performed == 1


@pytest.mark.parametrize("available", ["", "serial offline", "serial unauthorized", "a device\nb device"])
def test_unavailable_or_ambiguous_devices_never_pass(tmp_path, monkeypatch, available):
    monkeypatch.setattr(adbx, "Device", FakeDevice)
    FakeDevice.available = available
    FakeDevice.performed = 0
    assert adbx.run(plan(), tmp_path, "adbx", None)["status"] == "UNVERIFIED"
    assert FakeDevice.performed == 0


def test_expected_fragments_must_belong_to_one_answer_node():
    nodes = adbx.active_nodes(ui(text="one", background="two"), "test.app", "chat")
    assert not adbx.expectations(nodes, ["one", "two"])


def test_echoed_user_question_is_not_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(adbx, "Device", FakeDevice)
    FakeDevice.changed = False
    FakeDevice.available = "serial device"
    FakeDevice.performed = 0
    p = plan()
    p["steps"][0]["expect"] = ["ask"]
    p["steps"][0]["fresh"] = False
    assert adbx.run(p, tmp_path, "adbx", None)["status"] == "FAIL"
    assert FakeDevice.performed == 0


def test_invisible_node_is_not_evidence():
    xml = (
        b'<hierarchy><node package="test.app" text="answer" bounds="[0,0][100,100]" '
        b'visible-to-user="false"/></hierarchy>'
    )
    assert not adbx.expectations(adbx.active_nodes(xml, "test.app", ""), ["answer"])


@pytest.mark.parametrize(
    "key,value", [("package", None), ("timeout", "10"), ("target", ["text"]), ("absent", "x")]
)
def test_invalid_types_are_plan_errors(key, value):
    p = plan()
    if key == "package":
        p[key] = value
    else:
        p["steps"][0][key] = value
    with pytest.raises(ValueError):
        adbx.validate(p)


def test_send_with_input_echo_does_not_pass(tmp_path, monkeypatch):
    class SendEcho(FakeDevice):
        changed = False
        available = "serial device"

        def ui(self):
            if self.changed:
                return ui(text="How to charge safely")
            return ui(text="发送").replace(
                b"</hierarchy>",
                b'<node package="test.app" '
                b'class="android.widget.EditText" text="How to charge safely" '
                b'bounds="[0,0][100,100]"/></hierarchy>',
            )

    monkeypatch.setattr(adbx, "Device", SendEcho)
    p = plan()
    p["steps"][0].update(target={"text": "发送"}, expect=["How to charge safely"])
    assert adbx.run(p, tmp_path, "adbx", None)["status"] == "FAIL"


def test_navigation_cannot_relabel_old_destination_text_as_fresh(tmp_path, monkeypatch):
    class Navigation(FakeDevice):
        changed = False
        available = "serial device"

        def ui(self):
            return (
                ui(page="other[1]", text="old answer")
                if self.changed
                else ui(text="open", background="old answer")
            )

    monkeypatch.setattr(adbx, "Device", Navigation)
    p = plan()
    p["steps"][0].update(target={"text": "open"}, next_page="other", expect=["old answer"])
    assert adbx.run(p, tmp_path, "adbx", None)["status"] == "FAIL"


def test_answer_requires_shared_assistant_container():
    xml = (
        b'<hierarchy><node package="test.app" class="android.webkit.WebView" '
        b'text="chat[2]" bounds="[0,0][1080,2000]">'
        b'<node text="answer" bounds="[0,0][100,100]"/><node bounds="[0,150][100,400]">'
        b'<node text="AI notice" bounds="[0,150][100,200]"/><node text="failed" bounds="[0,200][100,400]"/>'
        b"</node></node></hierarchy>"
    )
    nodes = adbx.active_nodes(xml, "test.app", "chat")
    assert not adbx.expectations(nodes, ["answer"], "AI notice")
    assert adbx.expectations(nodes, ["failed"], "AI notice")
    for hidden in (b'bounds="[0,0][0,0]"', b'bounds="[0,200][100,400]" visible-to-user="false"'):
        hidden_xml = xml.replace(b'bounds="[0,200][100,400]"', hidden)
        assert not adbx.expectations(
            adbx.active_nodes(hidden_xml, "test.app", "chat"), ["failed"], "AI notice"
        )
