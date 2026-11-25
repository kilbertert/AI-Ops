"""Java 执行节点 StreamDeltaPusher 与 LoopTerminationGuard 真实 JVM 验证脚本（#1710）。

在具备本地 `docker`、`eclipse-temurin:21-jdk` 镜像以及已构建 `services/execution-node/target/`
产物时，编译并运行真实 Java 驱动对接本地回环 HTTP 桩，验证：
1. 最终答复增量按批/间隔单调递增推送（`chunk_index = 0, 1, ...`）并携带节点认证与执行配置头；
2. 工具调用轮（`hasToolCalls=true` 与 `before()` 轮次重置）不向流式回调泄露中间进度；
3. 旧版 Gateway 返回 404 时立即静默降级（`isDisabled() == true`），不抛错、不阻塞完成；
4. 取消/终局 `stop()` 后立即停止推送并丢弃缓冲。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NODE_DIR = ROOT / "services" / "execution-node"
NODE_JAR = NODE_DIR / "target" / "aiops-execution-node.jar"
NODE_CLASSES = NODE_DIR / "target" / "classes"

JAVA_DRIVER = r"""
package com.trendpower.aiops.node;

import org.springframework.ai.chat.client.ChatClientResponse;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.metadata.ChatGenerationMetadata;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.model.Generation;

import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

public class StreamDeltaDriver {

    private static ChatClientResponse textChunk(String text, String finishReason, boolean toolCall) {
        AssistantMessage msg = toolCall
                ? AssistantMessage.builder()
                    .content(text)
                    .toolCalls(List.of(new AssistantMessage.ToolCall(
                            "call_1", "function", "cpms_order_detail", "{}")))
                    .build()
                : new AssistantMessage(text);
        ChatGenerationMetadata meta = finishReason == null
                ? ChatGenerationMetadata.NULL
                : ChatGenerationMetadata.builder().finishReason(finishReason).build();
        Generation gen = new Generation(msg, meta);
        ChatResponse cr = new ChatResponse(List.of(gen));
        return ChatClientResponse.builder().chatResponse(cr).context(Map.of()).build();
    }

    public static void main(String[] args) throws Exception {
        String baseUrl = args[0];
        // Case 1: tool-call round discarded, final answer round streamed & flushed monotonically.
        GatewayClient okClient = new GatewayClient(baseUrl + "/ok", "node-secret-token", "build-test", 5);
        AtomicBoolean settled1 = new AtomicBoolean(false);
        StreamDeltaPusher pusher1 = new StreamDeltaPusher(
                okClient,
                "run_stream_ok_1234567890",
                "manifest-hash-1",
                settled1::get,
                10,
                40L,
                Duration.ofSeconds(2));
        LoopTerminationGuard guard = new LoopTerminationGuard();
        guard.captureAnswer();
        guard.streamDeltasTo(pusher1);

        // Round 1: progress text then tool_calls -> must be discarded before reaching Gateway.
        guard.before(null, null);
        guard.after(textChunk("正在查询", null, false), null);
        guard.after(textChunk("", "TOOL_CALLS", true), null);

        // Round 2: final natural-language answer streamed across multiple small chunks.
        guard.before(null, null);
        guard.after(textChunk("订单费用", null, false), null);
        guard.after(textChunk("已核对完成，", null, false), null);
        Thread.sleep(100);
        guard.after(textChunk("实付12.50元。", "STOP", false), null);
        if (!guard.finishedCleanly()) {
            throw new AssertionError("expected finishedCleanly");
        }
        pusher1.flushAndClose();
        if (pusher1.isDisabled()) {
            throw new AssertionError("ok pusher should not be disabled");
        }

        // Case 2: old Gateway 404 -> graceful degradation without throwing.
        GatewayClient oldClient = new GatewayClient(baseUrl + "/404", "node-secret-token", "build-test", 5);
        AtomicBoolean settled2 = new AtomicBoolean(false);
        StreamDeltaPusher pusher2 = new StreamDeltaPusher(
                oldClient,
                "run_stream_404_1234567890",
                "manifest-hash-2",
                settled2::get,
                4,
                20L,
                Duration.ofSeconds(2));
        pusher2.offer("第一段内容");
        Thread.sleep(150);
        pusher2.offer("第二段内容");
        pusher2.flushAndClose();
        if (!pusher2.isDisabled()) {
            throw new AssertionError("404 pusher must disable itself cleanly");
        }

        // Case 3: stop() immediately halts pushing and drops buffered text.
        GatewayClient cancelClient = new GatewayClient(
                baseUrl + "/cancel", "node-secret-token", "build-test", 5);
        AtomicBoolean settled3 = new AtomicBoolean(false);
        StreamDeltaPusher pusher3 = new StreamDeltaPusher(
                cancelClient,
                "run_stream_cancel_1234567890",
                "manifest-hash-3",
                settled3::get,
                100,
                5000L,
                Duration.ofSeconds(2));
        pusher3.offer("尚未刷出的文本");
        pusher3.stop();
        pusher3.offer("取消后的文本");
        pusher3.flushAndClose();
        if (pusher3.sentChunks() != 0) {
            throw new AssertionError("stopped pusher must send 0 chunks");
        }
        System.out.println("STREAM_DELTA_DRIVER_OK");
    }
}
"""


def main() -> None:
    if not NODE_JAR.is_file() or not NODE_CLASSES.is_dir():
        raise SystemExit(f"missing built node artifacts under {NODE_DIR / 'target'}")

    received: dict[str, list[dict]] = {"ok": [], "404": [], "cancel": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
            record = {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "exec_config": self.headers.get("X-AI-Ops-Exec-Config"),
                "node_build": self.headers.get("X-AI-Ops-Node-Build"),
                "body": payload,
            }
            if self.path.startswith("/ok/"):
                received["ok"].append(record)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"accepted":true}')
            elif self.path.startswith("/404/"):
                received["404"].append(record)
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"code":"NOT_FOUND"}}')
            else:
                received["cancel"].append(record)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"accepted":true}')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        with tempfile.TemporaryDirectory(prefix="aiops-stream-delta-") as td:
            work = Path(td)
            lib_dir = work / "lib"
            lib_dir.mkdir()
            with zipfile.ZipFile(NODE_JAR) as zf:
                for info in zf.infolist():
                    if info.filename.startswith("BOOT-INF/lib/") and info.filename.endswith(".jar"):
                        (lib_dir / Path(info.filename).name).write_bytes(zf.read(info.filename))

            src_dir = work / "src/com/trendpower/aiops/node"
            src_dir.mkdir(parents=True)
            (src_dir / "StreamDeltaDriver.java").write_text(JAVA_DRIVER, encoding="utf-8")
            out_classes = work / "out"
            out_classes.mkdir()

            compile_cmd = [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--cap-drop=ALL",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-v",
                f"{NODE_CLASSES}:/node-classes:ro",
                "-v",
                f"{lib_dir}:/deps:ro",
                "-v",
                f"{work}:/work",
                "eclipse-temurin:21-jdk",
                "javac",
                "-cp",
                "/node-classes:/deps/*",
                "-d",
                "/work/out",
                "/work/src/com/trendpower/aiops/node/StreamDeltaDriver.java",
            ]
            compiled = subprocess.run(compile_cmd, capture_output=True, text=True, check=False)
            assert compiled.returncode == 0, compiled.stderr

            run_cmd = [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network=host",
                "--cap-drop=ALL",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-v",
                f"{NODE_CLASSES}:/node-classes:ro",
                "-v",
                f"{lib_dir}:/deps:ro",
                "-v",
                f"{out_classes}:/out:ro",
                "eclipse-temurin:21-jdk",
                "java",
                "-cp",
                "/out:/node-classes:/deps/*",
                "com.trendpower.aiops.node.StreamDeltaDriver",
                f"http://127.0.0.1:{server.server_port}",
            ]
            ran = subprocess.run(run_cmd, capture_output=True, text=True, check=False, timeout=30)
            assert ran.returncode == 0, f"stdout={ran.stdout}\nstderr={ran.stderr}"
            assert "STREAM_DELTA_DRIVER_OK" in ran.stdout
    finally:
        server.shutdown()
        server.server_close()

    assert len(received["ok"]) >= 2
    assert [item["body"]["chunk_index"] for item in received["ok"]] == list(range(len(received["ok"])))
    combined = "".join(item["body"]["delta"] for item in received["ok"])
    assert combined == "订单费用已核对完成，实付12.50元。"
    assert "正在查询" not in combined
    for item in received["ok"]:
        assert item["path"] == "/ok/v1/internal/operator-runs/run_stream_ok_1234567890/stream-chunks"
        assert item["auth"] == "Bearer node-secret-token"
        assert item["exec_config"] == "manifest-hash-1"
        assert item["node_build"] == "build-test"
    assert len(received["404"]) == 1
    assert received["cancel"] == []
    print(
        json.dumps(
            {
                "real_jvm_stream_delta": True,
                "ok_chunks": len(received["ok"]),
                "graceful_404_degraded": True,
                "cancel_chunks_sent": 0,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
