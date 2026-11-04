#!/usr/bin/env python3
"""用外部adbx驱动已登录安卓应用；仅将新出现的当前页面内容视为验收证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path


class Unverified(RuntimeError):
    pass


def devices(text):
    return {
        p[0]: p[1]
        for line in text.splitlines()
        if len(p := line.split()) >= 2 and p[1] in {"device", "offline", "unauthorized"}
    }


def active_nodes(xml, package, page):
    root = ET.fromstring(xml)
    nodes = [n for n in root.iter("node") if n.get("package") == package]
    pages = [n for n in nodes if n.get("class") == "android.webkit.WebView" and n.get("text")]
    if page:
        # uni-app会把历史页面也留在UI树；第一个带页面标题的WebView才是当前页面。
        if not pages or not re.fullmatch(re.escape(page) + r"(?:\[\d+\])?", pages[0].get("text", "")):
            return []
        nodes = list(pages[0].iter("node"))
    return [
        n
        for n in nodes
        if bounds(n) and n.get("enabled", "true") == "true" and n.get("visible-to-user", "true") != "false"
    ]


def bounds(node):
    found = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not found:
        return None
    x1, y1, x2, y2 = map(int, found.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2) if x2 > x1 and y2 > y1 else None


def label(node):
    return node.get("text", "") or node.get("content-desc", "")


def target(nodes, selector):
    matched = [n for n in nodes if all(n.get(k, "") == v for k, v in selector.items())]
    points = {bounds(n) for n in matched}
    if len(points) != 1:
        raise AssertionError("目标控件缺失或不唯一")
    return points.pop()


def expectations(nodes, expected, assistant_marker=None):
    if assistant_marker:
        # 本APK的回答与AI标记是同一容器的直接子节点；不在页面祖先层匹配。
        visible = set(nodes)
        nodes = [
            child
            for parent in nodes
            if any(c in visible and label(c) == assistant_marker for c in parent)
            for child in parent
            if child in visible and label(child) != assistant_marker
        ]
    labels = [label(n) for n in nodes if n.get("class") != "android.widget.EditText"]
    return any(all(value in text for value in expected) for text in labels)


def validate(plan):
    if not isinstance(plan, dict) or set(plan) != {"package", "steps"}:
        raise ValueError("计划需要package和steps")
    if not isinstance(plan["package"], str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", plan["package"]
    ):
        raise ValueError("非法package")
    if not isinstance(plan["steps"], list) or not 1 <= len(plan["steps"]) <= 20:
        raise ValueError("steps需为1至20项")
    for step in plan["steps"]:
        if not isinstance(step, dict) or set(step) - {
            "name",
            "page",
            "action",
            "target",
            "text",
            "key",
            "swipe",
            "expect",
            "next_page",
            "timeout",
            "fresh",
            "absent",
            "assistant_marker",
        }:
            raise ValueError("非法步骤字段")
        for key in ("name", "page", "action"):
            if not isinstance(step.get(key), str) or not step[key]:
                raise ValueError("步骤需要name/page/action")
        if step["action"] not in {"tap", "text", "key", "swipe", "observe"}:
            raise ValueError("只支持tap/text/key/swipe/observe")
        expected = step.get("expect")
        if (
            not isinstance(expected, list)
            or not expected
            or any(not isinstance(s, str) or not s for s in expected)
        ):
            raise ValueError("每步需要非空expect")
        if (
            type(step.get("fresh", True)) is not bool
            or type(step.get("timeout", 60)) is not int
            or not 1 <= step.get("timeout", 60) <= 240
        ):
            raise ValueError("非法fresh/timeout")
        if "assistant_marker" in step and (
            not isinstance(step["assistant_marker"], str) or not step["assistant_marker"]
        ):
            raise ValueError("非法assistant_marker")
        absent = step.get("absent", [])
        if not isinstance(absent, list) or any(not isinstance(v, str) or not v for v in absent):
            raise ValueError("非法absent")
        if not isinstance(step.get("next_page", step["page"]), str):
            raise ValueError("非法next_page")
        if step["action"] in {"tap", "text"}:
            selector = step.get("target", {})
            if (
                not isinstance(selector, dict)
                or not selector
                or set(selector) - {"text", "content-desc", "resource-id", "class"}
            ):
                raise ValueError("点击/输入需要精确控件选择器")
            if any(not isinstance(v, str) or not v for v in selector.values()):
                raise ValueError("控件选择器必须是文本")
        if step["action"] == "text":
            value = step.get("text", "")
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 1000
                or not all(32 <= ord(c) < 127 for c in value)
                or "%" in value
            ):
                raise ValueError("input text仅接受可打印ASCII（不含%）；中文优先点击推荐问题")
        if step["action"] == "key" and step.get("key") not in {"KEYCODE_BACK", "KEYCODE_ENTER"}:
            raise ValueError("不支持的按键")
        if step["action"] == "swipe":
            values = step.get("swipe")
            if (
                not isinstance(values, list)
                or len(values) != 5
                or any(type(v) is not int or not 0 <= v <= 5000 for v in values)
            ):
                raise ValueError("swipe需要x1/y1/x2/y2/duration五个整数")


class Device:
    def __init__(self, adbx, serial, package):
        self.adbx, self.serial, self.package = adbx, serial, package
        self.env = dict(os.environ, ANDROID_SERIAL=serial)

    def call(self, *args, timeout=25):
        with subprocess.Popen(
            [self.adbx, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            start_new_session=True,
        ) as process:
            try:
                stdout, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise Unverified("adbx调用超时") from exc
            if process.returncode:
                raise Unverified("adbx调用失败，检查设备与隧道（原始输出未打印）")
            return stdout

    def ui(self):
        data = self.call(
            "exec-out",
            "rm -f /sdcard/aiops-acceptance-ui.xml; "
            "uiautomator dump /sdcard/aiops-acceptance-ui.xml >/dev/null 2>&1 && "
            "cat /sdcard/aiops-acceptance-ui.xml",
        )
        try:
            ET.fromstring(data)
        except ET.ParseError as exc:
            raise Unverified("设备UI树不可读") from exc
        return data

    def foreground(self):
        output = self.call("shell", "dumpsys window | grep mCurrentFocus").decode(errors="replace")
        if not re.search(r"\b" + re.escape(self.package) + r"/", output):
            raise AssertionError("目标应用不在前台，停止操作")

    def capture(self, out, stem, xml):
        (out / (stem + ".xml")).write_bytes(xml)
        photo = out / (stem + ".jpg")
        self.call("shot", str(photo), "1400")
        if not photo.exists() or not photo.read_bytes().startswith(b"\xff\xd8"):
            raise Unverified("adbx未生成有效截图")

    def perform(self, step, nodes):
        action = step["action"]
        if action in {"tap", "text"}:
            x, y = target(nodes, step["target"])
            self.call("shell", f"input tap {x} {y}")
        if action == "text":
            self.call("shell", "input text " + shlex.quote(step["text"].replace(" ", "%s")))
        if action == "key":
            self.call("shell", "input keyevent " + step["key"])
        if action == "swipe":
            self.call("shell", "input swipe " + " ".join(map(str, step["swipe"])))


def run(plan, out, adbx, serial):
    validate(plan)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "package": plan["package"],
        "plan_sha256": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
        "status": "UNVERIFIED",
        "evidence_scope": "device-ui-expectations; semantic review required",
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "adbx_sha256": hashlib.sha256(Path(adbx).read_bytes()).hexdigest() if Path(adbx).is_file() else None,
        "steps": [],
    }
    device = Device(adbx, serial or "", plan["package"])
    try:
        found = devices(device.call("devices", "-l").decode())
        if not serial:
            if len(found) != 1:
                raise Unverified("需要唯一设备或显式--serial")
            serial = next(iter(found))
        if found.get(serial) != "device":
            raise Unverified("所选设备未在线授权")
        device = Device(adbx, serial, plan["package"])
        report["device_sha256"] = hashlib.sha256(serial.encode()).hexdigest()
        report["model"] = device.call("shell", "getprop ro.product.model").decode().strip()
        report["display"] = device.call("shell", "wm size").decode().strip()
        report["android_release"] = device.call("shell", "getprop ro.build.version.release").decode().strip()
        version = device.call("shell", "dumpsys package " + plan["package"]).decode()
        report["version"] = {
            k: re.search(k + r"=([^\s]+)", version)[1]
            for k in ("versionName", "versionCode")
            if re.search(k + r"=([^\s]+)", version)
        }
        if len(report["version"]) != 2:
            raise Unverified("无法核对已安装应用版本")
        for index, step in enumerate(plan["steps"], 1):
            entry = {
                "name": step["name"],
                "status": "UNVERIFIED",
                "started_at": datetime.now(UTC).isoformat(),
            }
            report["steps"].append(entry)
            device.foreground()
            before = device.ui()
            nodes = active_nodes(before, plan["package"], step["page"])
            device.capture(out, f"{index:02d}-before", before)
            if not nodes:
                raise AssertionError("当前页面不符或UI为空")
            next_page = step.get("next_page", step["page"])
            if step.get("fresh", True) and expectations(
                active_nodes(before, plan["package"], ""), step["expect"]
            ):
                raise AssertionError("预期内容已存在，不能证明本次操作产生新结果")
            # 预期不能由本轮用户气泡本身满足；断言应来自助手或后续页面。
            submitted = [step.get("text", ""), step.get("target", {}).get("text", "")]
            submitted.extend(
                label(n)
                for n in ET.fromstring(before).iter("node")
                if n.get("package") == plan["package"] and n.get("class") == "android.widget.EditText"
            )
            if any(text and all(value in text for value in step["expect"]) for text in submitted):
                raise AssertionError("预期与操作文本相同，可能只验证了用户回显")
            device.perform(step, nodes)
            deadline = time.monotonic() + step.get("timeout", 60)
            after = before
            while time.monotonic() < deadline:
                device.foreground()
                after = device.ui()
                current = active_nodes(after, plan["package"], next_page)
                if expectations(current, step["expect"], step.get("assistant_marker")) and not any(
                    expectations(current, [v]) for v in step.get("absent", [])
                ):
                    break
                time.sleep(1)
            else:
                device.capture(out, f"{index:02d}-failed", after)
                raise AssertionError("期限内未出现预期的新页面内容")
            device.capture(out, f"{index:02d}-after", after)
            entry.update(status="PASS", finished_at=datetime.now(UTC).isoformat())
        report["status"] = "PASS"
    except (Unverified, OSError) as exc:
        report["reason"] = str(exc) if isinstance(exc, Unverified) else "本地执行工具不可用"
    except AssertionError as exc:
        report.update(status="FAIL", reason=str(exc))
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        if report["steps"] and report["steps"][-1]["status"] != "PASS":
            report["steps"][-1].update(status=report["status"], reason=report.get("reason"))
        report["artifacts"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(out.iterdir())
            if p.suffix in {".jpg", ".xml"}
        }
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True, help="库外JSON步骤计划，登录完成后运行")
    parser.add_argument("--out", type=Path, required=True, help="全新私有证据目录")
    parser.add_argument("--adbx", default=str(Path.home() / "bin/adbx"))
    parser.add_argument("--serial", help="多设备时必须指定，不自动选择第一台")
    parser.add_argument("--execute", action="store_true", help="实际操作设备；缺省只校验计划")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        plan = json.loads(args.plan.read_text())
        validate(plan)
        if not args.execute:
            print("UNVERIFIED: 计划有效，未操作设备；执行需--execute")
            return 2
        args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    except (ValueError, OSError) as exc:
        print("UNVERIFIED: 无效计划或输出目录：" + str(exc))
        return 2
    report = run(plan, args.out.resolve(), args.adbx, args.serial)
    print(report["status"] + ": " + str(args.out / "report.json"))
    return {"PASS": 0, "FAIL": 1, "UNVERIFIED": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
