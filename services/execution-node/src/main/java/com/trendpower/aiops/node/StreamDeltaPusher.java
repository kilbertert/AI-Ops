package com.trendpower.aiops.node;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Duration;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.BooleanSupplier;

/**
 * 管家自然语言答复的增量回调推送器：把模型流式 chunk 按字符数/时间窗口合批推回 Gateway
 * {@code POST /v1/internal/operator-runs/{runId}/stream-chunks}。
 *
 * <p>设计约束：
 * <ul>
 *   <li>严格单 run 串行发送，保证 {@code chunk_index} 单调递增且不并发乱序。</li>
 *   <li>单次 POST 有界（最多 2000 字、3 秒超时）；单 run 推送总长上限 24000 字。</li>
 *   <li>任意 HTTP 异常（含旧版 Gateway 返回 404、超时、5xx）立即置 {@code disabled}
 *       并静默降级，绝不阻塞或打断主流程及最终 {@code reportCompleted} 终局上报。</li>
 *   <li>run 取消/失败/终局后立即停止推送，且日志只记录 runId 与错误码，不输出正文或凭据。</li>
 * </ul>
 */
final class StreamDeltaPusher {

    private static final Logger LOGGER = LoggerFactory.getLogger(StreamDeltaPusher.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    static final int DEFAULT_BATCH_CHARS = 24;
    static final long DEFAULT_FLUSH_INTERVAL_MS = 80L;
    static final int MAX_POST_CHARS = 2000;
    static final int MAX_TOTAL_CHARS = 24000;
    static final Duration DEFAULT_POST_TIMEOUT = Duration.ofSeconds(3);
    private static final long FLUSH_WAIT_MS = 2000L;

    private static final ScheduledExecutorService TIMER = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "aiops-stream-delta-timer");
        t.setDaemon(true);
        return t;
    });

    private static final ExecutorService SENDER = Executors.newFixedThreadPool(4, r -> {
        Thread t = new Thread(r, "aiops-stream-delta-sender");
        t.setDaemon(true);
        return t;
    });

    private final GatewayClient gateway;
    private final String runId;
    private final String manifestId;
    private final BooleanSupplier settled;
    private final int batchChars;
    private final long flushIntervalMs;
    private final Duration postTimeout;

    private final Object lock = new Object();
    private final StringBuilder buffer = new StringBuilder();
    private final AtomicBoolean stopped = new AtomicBoolean(false);
    private boolean timerScheduled;
    private boolean draining;
    private boolean closed;
    private volatile boolean disabled;
    private int nextChunkIndex;
    private int totalBufferedOrSent;

    StreamDeltaPusher(GatewayClient gateway, String runId, String manifestId, AtomicBoolean settled) {
        this(gateway, runId, manifestId, settled::get, DEFAULT_BATCH_CHARS, DEFAULT_FLUSH_INTERVAL_MS,
                DEFAULT_POST_TIMEOUT);
    }

    StreamDeltaPusher(GatewayClient gateway,
                      String runId,
                      String manifestId,
                      BooleanSupplier settled,
                      int batchChars,
                      long flushIntervalMs,
                      Duration postTimeout) {
        this.gateway = gateway;
        this.runId = runId;
        this.manifestId = manifestId;
        this.settled = settled;
        this.batchChars = Math.max(1, batchChars);
        this.flushIntervalMs = Math.max(10L, flushIntervalMs);
        this.postTimeout = postTimeout == null ? DEFAULT_POST_TIMEOUT : postTimeout;
    }

    void offer(String delta) {
        if (delta == null || delta.isEmpty() || disabled || stopped.get() || settled.getAsBoolean()) {
            return;
        }
        synchronized (lock) {
            if (closed || disabled || stopped.get() || settled.getAsBoolean()) {
                return;
            }
            int remaining = MAX_TOTAL_CHARS - totalBufferedOrSent;
            if (remaining <= 0) {
                return;
            }
            String piece = delta.length() > remaining ? delta.substring(0, remaining) : delta;
            buffer.append(piece);
            totalBufferedOrSent += piece.length();
            if (buffer.length() >= batchChars) {
                scheduleDrainLocked();
            } else if (!timerScheduled) {
                timerScheduled = true;
                TIMER.schedule(this::onTimer, flushIntervalMs, TimeUnit.MILLISECONDS);
            }
        }
    }

    /** 工具调用轮开始/切换时丢弃尚未发出的中间缓冲，避免工具轮进度文本污染最终流。 */
    void discardPending() {
        synchronized (lock) {
            if (!closed && buffer.length() > 0) {
                totalBufferedOrSent = Math.max(0, totalBufferedOrSent - buffer.length());
                buffer.setLength(0);
            }
        }
    }

    /** 异常/截断/取消时立即停推，丢弃未发送缓冲。 */
    void stop() {
        stopped.set(true);
        disabled = true;
        synchronized (lock) {
            closed = true;
            buffer.setLength(0);
            lock.notifyAll();
        }
    }

    /** 正常终局前调用：等待在飞推送并同步刷出尾部剩余增量，再关闭推送器。 */
    void flushAndClose() {
        long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(FLUSH_WAIT_MS);
        synchronized (lock) {
            while (draining && !stopped.get() && !disabled) {
                long remainingMs = TimeUnit.NANOSECONDS.toMillis(deadline - System.nanoTime());
                if (remainingMs <= 0) {
                    break;
                }
                try {
                    lock.wait(remainingMs);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
            if (draining || stopped.get() || disabled) {
                closed = true;
                buffer.setLength(0);
                return;
            }
            draining = true;
        }
        try {
            drainSync(true);
        } finally {
            synchronized (lock) {
                draining = false;
                closed = true;
                buffer.setLength(0);
                lock.notifyAll();
            }
        }
    }

    boolean isDisabled() {
        return disabled;
    }

    int sentChunks() {
        synchronized (lock) {
            return nextChunkIndex;
        }
    }

    private void onTimer() {
        synchronized (lock) {
            timerScheduled = false;
            if (!closed && !disabled && !stopped.get() && !settled.getAsBoolean() && buffer.length() > 0) {
                scheduleDrainLocked();
            }
        }
    }

    private void scheduleDrainLocked() {
        if (draining || closed || disabled || stopped.get()) {
            return;
        }
        draining = true;
        SENDER.execute(() -> {
            try {
                drainSync(false);
            } finally {
                synchronized (lock) {
                    draining = false;
                    lock.notifyAll();
                    if (!closed && !disabled && !stopped.get() && !settled.getAsBoolean()
                            && buffer.length() >= batchChars) {
                        scheduleDrainLocked();
                    } else if (!closed && !disabled && !stopped.get() && !settled.getAsBoolean()
                            && buffer.length() > 0 && !timerScheduled) {
                        timerScheduled = true;
                        TIMER.schedule(this::onTimer, flushIntervalMs, TimeUnit.MILLISECONDS);
                    }
                }
            }
        });
    }

    private void drainSync(boolean finalFlush) {
        while (true) {
            String chunk;
            int chunkIndex;
            synchronized (lock) {
                if (disabled || stopped.get() || (!finalFlush && (closed || settled.getAsBoolean()))) {
                    buffer.setLength(0);
                    return;
                }
                if (buffer.length() == 0) {
                    return;
                }
                int take = Math.min(buffer.length(), MAX_POST_CHARS);
                chunk = buffer.substring(0, take);
                buffer.delete(0, take);
                chunkIndex = nextChunkIndex++;
            }
            if (!postChunk(chunk, chunkIndex)) {
                return;
            }
        }
    }

    private boolean postChunk(String chunk, int chunkIndex) {
        try {
            var body = MAPPER.createObjectNode();
            body.put("delta", chunk);
            body.put("chunk_index", chunkIndex);
            gateway.post(
                    "/v1/internal/operator-runs/" + runId + "/stream-chunks",
                    body.toString(),
                    manifestId,
                    null,
                    postTimeout);
            return true;
        } catch (RuntimeException e) {
            disabled = true;
            synchronized (lock) {
                buffer.setLength(0);
            }
            String code = e instanceof GatewayClient.GatewayRefused refused
                    ? refused.status() + ":" + refused.code()
                    : e.getClass().getSimpleName();
            LOGGER.warn("operator stream-chunks disabled for run {} ({})", runId, code);
            return false;
        }
    }
}
