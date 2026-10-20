package com.trendpower.aiops.node;

import org.springframework.stereotype.Component;

import java.util.concurrent.ConcurrentHashMap;

/**
 * 一次 {@code execute} 的**执行作用域**：本次 run 的终止守卫。
 *
 * <p>为什么是线程内的，而不是一张按 run_id 索引的全局表：框架在**调用线程**上同步构建
 * ChatClient（{@code ClientChatExecutor.streamResponses} 在 {@code Flux.subscribe} 之前
 * 完成构建），所以「把守卫交给 customizer」只需要在这一次调用期间可见。全局表按 run_id
 * 索引时，同一 JVM 里相同的 run_id 重入会互相覆盖槽位：后者的 attach 落到前者的槽里，
 * 前者循环正常却找不到自己的守卫，于是把一次成功的 run 报成
 * {@code model_stream_incomplete}。
 *
 * <p>**不把 {@code super.execute} 的返回当作整轮结束**：实测 {@code executeStream} 在
 * {@code Flux.subscribe(...)} 之后直接 return，流仍在跑。所以线程作用域只在调用期间存在，
 * 而「这条 run 正在本机执行」这一事实由 {@link ActiveRuns} 按真实终局回调登记/移除。
 */
final class RunExecution {

    /** 构建期交接：仅在调用 {@code execute} 的那个线程上可见。 */
    private static final ThreadLocal<RunExecution> CURRENT = new ThreadLocal<>();

    private final String runId;
    private final LoopTerminationGuard guard = new LoopTerminationGuard();
    private final long startedAtNanos = System.nanoTime();
    /** 本次执行是否已经结算（完成/失败/构造期失败），保证登记与通知各一次。 */
    final java.util.concurrent.atomic.AtomicBoolean settled =
            new java.util.concurrent.atomic.AtomicBoolean();

    private RunExecution(String runId) {
        this.runId = runId;
    }

    static RunExecution create(String runId) {
        return new RunExecution(runId);
    }

    /**
     * 原样保存前值：嵌套执行时外层可能是**另一个** run 的作用域，
     * 按本 run 过滤会把它丢掉（退出后就恢复不回来了）。
     */
    static RunExecution peek() {
        return CURRENT.get();
    }

    static void enter(RunExecution execution) {
        CURRENT.set(execution);
    }

    static void exit(RunExecution previous) {
        if (previous == null) {
            CURRENT.remove();
        } else {
            CURRENT.set(previous);
        }
    }

    /** 构建期取当前执行；run_id 不匹配就当没有（绝不把守卫挂到别人的会话上）。 */
    static RunExecution current(String runId) {
        RunExecution execution = CURRENT.get();
        return execution != null && execution.runId.equals(runId) ? execution : null;
    }

    String runId() {
        return runId;
    }

    LoopTerminationGuard guard() {
        return guard;
    }

    long startedAtNanos() {
        return startedAtNanos;
    }

    /** 便于日志：只带 run 编号，不带任何身份或业务数据。 */
    static String runIdOf(com.aizuda.snail.ai.agent.core.runtime.ChatSessionRequest request) {
        var dispatch = request == null ? null : request.getDispatchRequest();
        String sid = dispatch == null ? null : dispatch.getSid();
        return sid != null && sid.matches("run_[A-Za-z0-9_-]{20,64}") ? sid : null;
    }

    /**
     * 本机「正在执行的 run」登记表：**同 run 重入在本地拒绝**，不向 Gateway 发任何通知。
     *
     * <p>为什么不能靠「是否领到业务」来区分：Gateway 对 `running` + 已有快照的重试会回
     * 200 并重放快照，所以同一 run 的第二个执行**也能**领到业务；若让两个执行都发终局，
     * 后失败的那个会把正在正常跑的原 run 杀掉。拒绝必须发生在**进入执行之前**。
     *
     * <p>移除发生在**真实终局回调**（completion / error）里，而不是 {@code execute} 返回时
     * —— 后者在订阅后立刻返回，流还在跑。
     *
     * <p>取消（订阅方 dispose）在框架里**没有回调**（实测两个回调都不到），因此这类登记
     * 不会被主动移除。这里用两条**实际生效**的规则收口，而不是一句「有界」的声明：
     * <ul>
     *   <li>超过 {@link #MAX_AGE_MILLIS} 的登记在每次进入时清扫。这个界限大于 Gateway 的
     *       run TTL（180 秒），超过它的 run 在 Gateway 侧已经过期，终局通知本来也会被拒。</li>
     *   <li>清扫后仍达到 {@link #HARD_CAP} 时，**拒绝新的执行**并如实报本地错误
     *       （{@link Refusal#AT_CAP}）—— 上限是被执行的，不是装饰。</li>
     * </ul>
     */
    @Component
    static class ActiveRuns {

        /** 必须大于 Gateway 的 RUN_TTL_SECONDS（180s）：更老的 run 已不可能成功。 */
        static final long MAX_AGE_MILLIS = 15 * 60 * 1000L;

        /** 同时活跃的上限。达到时拒绝新执行，而不是无界增长。 */
        static final int HARD_CAP = 256;

        private final ConcurrentHashMap<String, RunExecution> active = new ConcurrentHashMap<>();

        enum Refusal { NONE, DUPLICATE, AT_CAP }

        /**
         * 登记与上限判定必须在**同一临界区**：「先查 size 再 putIfAbsent」会让并发的
         * 不同 run 一起越过上限。这里用最轻的 synchronized(active)，而不是宣称一个
         * 自己没执行的约束。
         */
        Refusal begin(RunExecution execution) {
            synchronized (active) {
                sweepExpiredLocked();
                if (active.size() >= HARD_CAP) {
                    return Refusal.AT_CAP;
                }
                RunExecution previous = active.putIfAbsent(execution.runId(), execution);
                return previous == null ? Refusal.NONE : Refusal.DUPLICATE;
            }
        }

        /** 条件移除：只删**自己那一格**，不会误删同 run 的后来者。 */
        void finish(RunExecution execution) {
            synchronized (active) {
                active.remove(execution.runId(), execution);
            }
        }

        int size() {
            return active.size();
        }

        boolean isActive(String runId) {
            return active.containsKey(runId);
        }

        private void sweepExpiredLocked() {
            long cutoff = System.nanoTime() - MAX_AGE_MILLIS * 1_000_000L;
            active.entrySet().removeIf(entry -> entry.getValue().startedAtNanos() < cutoff);
        }
    }
}
