"""Java 执行节点 StreamDeltaPusher 与 LoopTerminationGuard 静态契约测试（#1710）。

真实 JVM 端到端回环验证脚本位于 `tools/verification/verify_node_stream_delta.py`
（需本地 `docker` 与 `eclipse-temurin:21-jdk` 镜像，不占用 CI 跳过配额）。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NODE_DIR = ROOT / "services" / "execution-node"


def test_execution_node_stream_delta_callback_source_contracts():
    """纯静态契约检查（无需 JDK/Docker，CI 必跑）：回调路径、降级与单消息上限边界。"""
    pusher_src = (NODE_DIR / "src/main/java/com/trendpower/aiops/node/StreamDeltaPusher.java").read_text(
        encoding="utf-8"
    )
    runtime_src = (
        NODE_DIR / "src/main/java/com/trendpower/aiops/node/ReportingChatSessionRuntime.java"
    ).read_text(encoding="utf-8")
    guard_src = (NODE_DIR / "src/main/java/com/trendpower/aiops/node/LoopTerminationGuard.java").read_text(
        encoding="utf-8"
    )
    assert "/v1/internal/operator-runs/" in pusher_src and "/stream-chunks" in pusher_src
    assert "chunk_index" in pusher_src and '"delta"' in pusher_src
    assert "disabled = true;" in pusher_src
    assert "deltaPusher.flushAndClose();" in runtime_src
    assert "deltaPusher.stop();" in runtime_src
    assert "hasToolCalls" in guard_src and "discardPending()" in guard_src
    assert "48000" in runtime_src
