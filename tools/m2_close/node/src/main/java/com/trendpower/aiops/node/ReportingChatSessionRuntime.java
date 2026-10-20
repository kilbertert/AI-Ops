package com.trendpower.aiops.node;

import com.aizuda.snail.ai.agent.core.executor.ClientChatExecutor;
import com.aizuda.snail.ai.agent.common.counter.ActiveChatCounter;
import com.aizuda.snail.ai.agent.core.executor.client.ChatClientBuildContext;
import com.aizuda.snail.ai.agent.core.executor.client.ChatClientCustomizer;
import com.aizuda.snail.ai.agent.core.runtime.ChatSessionRequest;
import com.aizuda.snail.ai.agent.core.runtime.ChatSessionRuntime;
import com.aizuda.snail.ai.agent.core.runtime.tool.ToolRuntime;
import com.trendpower.aiops.node.exec.GuardedChatModelFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Primary;
import org.springframework.stereotype.Component;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 把「整轮模型工具循环结束」如实报给 Gateway —— **并只在 Gateway 确认之后**才让本次
 * run 被算作成功。
 *
 * <p>为什么必须接管 {@link ChatSessionRuntime} 而不是只加一个 advisor：
 * <ul>
 *   <li>整轮结束的唯一回调是 {@code completionConsumer}；失败只有 {@code errorConsumer}；
 *       取消与进程退出**没有任何回调**（本片探针实测）。</li>
 *   <li>「完成」回调本身不够：末轮被截断时它照样触发（completion 文本是半截）。所以还要
 *       看 {@link LoopTerminationGuard} 记录的最后一次 finish reason。</li>
 *   <li>框架的 completion 回调会直接把「成功」发给平台；要保证「Gateway 未确认就不算成功」，
 *       就只能在它**之前**插入一次有界请求，未确认时改走 {@code errorConsumer}。</li>
 * </ul>
 *
 * <p>自动配置里的 {@code chatSessionRuntime} 是普通 {@code @Bean}（没有
 * {@code ConditionalOnMissingBean}），所以接管方式是 {@code @Primary} 子类 ——
 * 实测自动配置的 gRPC handler 拿到的就是子类实例（本片探针 {@code handler-holds-the-primary}）。
 *
 * <p>未启用清单时**原样透传**，与今天逐字一致：不发身份头、不发完成通知。
 */
@Component
@Primary
class ReportingChatSessionRuntime extends ChatSessionRuntime {

    private final GatewayClient gateway;
    private final GuardedChatModelFactory modelFactory;
    private final RunScope scope;

    @Autowired
    ReportingChatSessionRuntime(ClientChatExecutor executor,
                                ActiveChatCounter counter,
                                ToolRuntime toolRuntime,
                                GatewayClient gateway,
                                GuardedChatModelFactory modelFactory,
                                RunScope scope) {
        super(executor, counter, toolRuntime);
        this.gateway = gateway;
        this.modelFactory = modelFactory;
        this.scope = scope;
    }

    @Override
    public void execute(ChatSessionRequest request) {
        String runId = runIdOf(request);
        if (!modelFactory.enabled() || runId == null) {
            super.execute(request);
            return;
        }
        // 先登记本次 run：ChatClient（以及挂守卫的 customizer）是在这之后才构造的。
        RunScope.Slot slot = scope.open(runId);
        ChatSessionRequest wrapped = ChatSessionRequest.builder()
                .dispatchRequest(request.getDispatchRequest())
                .textConsumer(request.getTextConsumer())
                .thinkingConsumer(request.getThinkingConsumer())
                .completionConsumer(result -> {
                    try {
                        LoopTerminationGuard guard = slot.guard();
                        if (guard == null || !guard.finishedCleanly()) {
                            // 循环"结束"了，但最后一轮没有正常说完整（截断/长度/内容过滤/缺失）。
                            reportFailed(runId, "model_stream_incomplete");
                            forwardError(request, new LoopIncomplete(
                                    "model stream did not finish cleanly"));
                            return;
                        }
                        if (!reportCompleted(runId)) {
                            // Gateway 未确认：**不能**把这次 run 报成成功。
                            forwardError(request, new CompletionNotConfirmed(
                                    "gateway did not confirm loop completion"));
                            return;
                        }
                        request.acceptCompletion(result);
                    } finally {
                        scope.close(runId);
                    }
                })
                .errorConsumer(error -> {
                    try {
                        reportFailed(runId, "model_failed");
                        forwardError(request, error);
                    } finally {
                        scope.close(runId);
                    }
                })
                .build();
        super.execute(wrapped);
    }

    /** 完成通知：只在 2xx 时算确认。**不重试**（宁可少报成功，不伪造）。 */
    private boolean reportCompleted(String runId) {
        try {
            gateway.post(
                    "/v1/internal/charging-status/completions/" + runId,
                    "{\"status\":\"completed\"}",
                    manifestId());
            return true;
        } catch (RuntimeException e) {
            return false;
        }
    }

    /** 失败通知：尽力而为，绝不因为通知失败而改变本地错误路径。 */
    private void reportFailed(String runId, String reason) {
        try {
            gateway.post(
                    "/v1/internal/charging-status/completions/" + runId,
                    "{\"status\":\"failed\",\"reason\":\"" + reason + "\"}",
                    manifestId());
        } catch (RuntimeException ignored) {
            // 通知发不出去时记录停在 running，由 TTL 收敛 —— 不会变成假成功。
        }
    }

    /** 本 run 已核对的执行配置身份：取自守卫持有的**同一份**清单对象，不重读文件。 */
    private String manifestId() {
        var manifest = modelFactory.manifest();
        return manifest == null ? null : manifest.id();
    }

    private void forwardError(ChatSessionRequest request, Throwable error) {
        if (request.getErrorConsumer() != null) {
            request.getErrorConsumer().accept(error);
        }
    }

    private static String runIdOf(ChatSessionRequest request) {
        var dispatch = request == null ? null : request.getDispatchRequest();
        String sid = dispatch == null ? null : dispatch.getSid();
        return sid != null && sid.matches("run_[A-Za-z0-9_-]{20,64}") ? sid : null;
    }

    /** 末轮不完整。只带结论，不带上游正文或凭据。 */
    public static final class LoopIncomplete extends RuntimeException {
        public LoopIncomplete(String message) {
            super(message);
        }
    }

    /** Gateway 未确认整轮完成。平台据此判本次 run 失败（而不是成功）。 */
    public static final class CompletionNotConfirmed extends RuntimeException {
        public CompletionNotConfirmed(String message) {
            super(message);
        }
    }

    /** 每次 run 的登记表：execute 先开一格，customizer 把守卫填进去。 */
    @Component
    static class RunScope {
        private final Map<String, Slot> slots = new ConcurrentHashMap<>();

        Slot open(String runId) {
            Slot slot = new Slot();
            slots.put(runId, slot);
            return slot;
        }

        void close(String runId) {
            slots.remove(runId);
        }

        void attach(String runId, LoopTerminationGuard guard) {
            Slot slot = slots.get(runId);
            if (slot != null) {
                slot.attach(guard);
            }
        }

        static final class Slot {
            private volatile LoopTerminationGuard guard;

            void attach(LoopTerminationGuard value) {
                this.guard = value;
            }

            LoopTerminationGuard guard() {
                return guard;
            }
        }
    }

    /**
     * 把 {@link LoopTerminationGuard} 挂到**每一次** run 的 ChatClient 上。
     *
     * <p>它必须晚于 {@code ToolCallingAdvisor}（{@code -2147483348}）才会每轮都经过；
     * order 0 满足这一点（拦截器链是 300）。
     */
    @Component
    static class LoopGuardCustomizer implements ChatClientCustomizer {

        private final RunScope scope;

        LoopGuardCustomizer(RunScope scope) {
            this.scope = scope;
        }

        @Override
        public int order() {
            return 0;
        }

        @Override
        public void customize(ChatClientBuildContext context) {
            var request = context.getRequest() == null ? null : context.getRequest().getDispatchRequest();
            String runId = request == null ? null : request.getSid();
            if (runId == null || !runId.matches("run_[A-Za-z0-9_-]{20,64}")) {
                return;
            }
            LoopTerminationGuard guard = new LoopTerminationGuard();
            scope.attach(runId, guard);
            context.getBuilder().defaultAdvisors(guard);
        }
    }
}
