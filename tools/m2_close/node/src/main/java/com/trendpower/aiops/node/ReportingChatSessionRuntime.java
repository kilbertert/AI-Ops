package com.trendpower.aiops.node;

import com.aizuda.snail.ai.agent.core.executor.ClientChatExecutor;
import com.aizuda.snail.ai.agent.common.counter.ActiveChatCounter;
import com.aizuda.snail.ai.agent.core.executor.client.ChatClientBuildContext;
import com.aizuda.snail.ai.agent.core.executor.client.ChatClientCustomizer;
import com.aizuda.snail.ai.agent.core.runtime.ChatSessionRequest;
import com.aizuda.snail.ai.agent.core.runtime.ChatSessionRuntime;
import com.aizuda.snail.ai.agent.core.runtime.tool.ToolRuntime;
import com.trendpower.aiops.node.exec.GuardedChatModelFactory;
import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Primary;
import org.springframework.stereotype.Component;

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
    private final RunExecution.ActiveRuns activeRuns;
    @org.springframework.beans.factory.annotation.Value("${aiops.node.managed-publication:false}")
    private boolean managedPublication;

    @Autowired
    ReportingChatSessionRuntime(ClientChatExecutor executor,
                                ActiveChatCounter counter,
                                ToolRuntime toolRuntime,
                                GatewayClient gateway,
                                GuardedChatModelFactory modelFactory,
                                RunExecution.ActiveRuns activeRuns) {
        super(executor, counter, toolRuntime);
        this.gateway = gateway;
        this.modelFactory = modelFactory;
        this.activeRuns = activeRuns;
    }

    @Override
    public void execute(ChatSessionRequest request) {
        String runId = RunExecution.runIdOf(request);
        if (managedPublication && runId == null) {
            forwardError(request, new IllegalArgumentException("managed publication requires an authorized run"));
            return;
        }
        if ((!modelFactory.enabled() && !managedPublication) || runId == null) {
            super.execute(request);
            return;
        }
        RunExecution execution = RunExecution.create(runId);
        RunExecution.ActiveRuns.Refusal refusal = activeRuns.begin(execution);
        if (refusal != RunExecution.ActiveRuns.Refusal.NONE) {
            // **本地拒绝，且不发任何通知**：这条 run 正在本机执行（或登记表已满）。
            // Gateway 对 running+快照的重试会回 200 并重放快照，所以「能领到」并不能
            // 证明这次执行拥有这条 run；让第二个执行发终局会把原 run 杀掉。
            forwardError(request, new DuplicateRunRefused(
                    refusal == RunExecution.ActiveRuns.Refusal.DUPLICATE
                            ? "this run is already executing in this process"
                            : "too many active runs in this process"));
            return;
        }
        // 守卫在**调用线程**上交接：框架在 subscribe 之前同步构建 ChatClient，
        // 所以这一次调用期间的线程局部作用域正好覆盖 customizer 的可见范围。
        // 原样保存前值：嵌套执行的可能是**另一个** run 的作用域，不能按本 run 过滤。
        RunExecution previous = RunExecution.peek();
        RunExecution.enter(execution);
        com.trendpower.aiops.node.exec.ExecManifest previousManifest = null;
        String manifestId = null;
        boolean publicationInstalled = false;
        try {
            if (managedPublication) {
                JsonNode publication = gateway.post(
                        "/v1/internal/charging-status/execution-config/" + runId, "{}", null);
                if (!runId.equals(publication.path("run_id").asText())) {
                    throw new IllegalStateException("publication run identity differs");
                }
                var frozen = com.trendpower.aiops.node.exec.ExecManifest.fromText(
                        publication.path("manifest").asText(), publication.path("sha256").asText());
                if (frozen.systemMessages().size() != 1) {
                    throw new IllegalStateException("node SOP requires one system message");
                }
                previousManifest = modelFactory.usePublication(frozen);
                publicationInstalled = true;
                request.getDispatchRequest().setSystemPrompt(frozen.systemMessages().getFirst());
                if (request.getDispatchRequest().getAgentConfig() != null) {
                    request.getDispatchRequest().getAgentConfig().setInstruction("");
                }
            }
            manifestId = manifestId();
            boolean operator = modelFactory.manifest().tools().stream()
                    .anyMatch(t -> CpmsReadTool.NAMES.contains(t.name()));
            super.execute(wrap(request, execution, manifestId, operator));
        } catch (RuntimeException | Error failure) {
            // 构造期/派发期失败（含清单/工具漂移）：这是**本次执行**的终局失败，
            // 必须从 issued/running 落 failed，而不是留在 issued。
            String failedManifest = manifestId;
            finishOnce(execution, request, () -> {
                reportFailed(runId, "node_failed", failedManifest);
                forwardError(request, failure);
            });
            throw failure;
        } finally {
            // 只恢复线程上下文：super.execute 在订阅后立即返回，流还在跑。
            if (publicationInstalled) modelFactory.usePublication(previousManifest);
            RunExecution.exit(previous);
        }
    }

    private ChatSessionRequest wrap(ChatSessionRequest request, RunExecution execution, String manifestId, boolean operator) {
        String runId = execution.runId();
        return ChatSessionRequest.builder()
                .dispatchRequest(request.getDispatchRequest())
                .textConsumer(request.getTextConsumer())
                .thinkingConsumer(request.getThinkingConsumer())
                .completionConsumer(result -> finishOnce(execution, request, () -> {
                    if (!execution.guard().finishedCleanly()) {
                        // 循环"结束"了，但最后一轮没有正常说完整（截断/长度/内容过滤/缺失）。
                        reportFailed(runId, "model_stream_incomplete", manifestId);
                        forwardError(request, new LoopIncomplete(
                                "model stream did not finish cleanly"));
                        return;
                    }
                    if (!reportCompleted(runId, manifestId, operator ? result.fullText() : null)) {
                        // Gateway 未确认：**不能**把这次 run 报成成功。
                        forwardError(request, new CompletionNotConfirmed(
                                "gateway did not confirm loop completion"));
                        return;
                    }
                    request.acceptCompletion(result);
                }))
                .errorConsumer(error -> finishOnce(execution, request, () -> {
                    reportFailed(runId, "model_failed", manifestId);
                    forwardError(request, error);
                }))
                .build();
    }

    /**
     * 只结算一次，并在**同一次**结算里把这条 run 从本机登记表移除。
     *
     * <p>移除点就是真实终局：completion 或 error 回调。取消没有回调，这类登记由
     * {@link RunExecution.ActiveRuns} 的超时清扫与硬上限收口（见那里的注释）。
     */
    private void finishOnce(RunExecution execution, ChatSessionRequest request, Runnable settle) {
        if (!execution.settled.compareAndSet(false, true)) {
            return;
        }
        try {
            settle.run();
        } finally {
            activeRuns.finish(execution);
        }
    }

    /**
     * 完成通知：必须**校验回执**才算确认，2xx 本身不算。
     *
     * <p>复核反例：`200 {}`、`200 {"run_id":"别的run","state":"succeeded"}`、
     * `200 {"run_id":本run,"state":"failed"}` 都不能被记成成功 —— 一个认错记录的应答
     * 和一次拒绝在 HTTP 状态上无法区分。**不重试**（宁可少报成功，不伪造）。
     */
    private boolean reportCompleted(String runId, String manifestId, String answer) {
        JsonNode ack;
        try {
            var body = new com.fasterxml.jackson.databind.ObjectMapper().createObjectNode();
            body.put("status", "completed");
            if (answer != null) body.put("answer", answer);
            ack = gateway.post(
                    answer == null ? "/v1/internal/charging-status/completions/" + runId
                            : "/v1/internal/operator-runs/" + runId + "/completions",
                    body.toString(),
                    manifestId);
        } catch (RuntimeException e) {
            return false;
        }
        return ack != null
                && runId.equals(ack.path("run_id").asText(null))
                && "succeeded".equals(ack.path("state").asText(null));
    }

    /** 失败通知：尽力而为，绝不因为通知失败而改变本地错误路径。 */
    private void reportFailed(String runId, String reason, String manifestId) {
        try {
            gateway.post(
                    "/v1/internal/charging-status/completions/" + runId,
                    "{\"status\":\"failed\",\"reason\":\"" + reason + "\"}",
                    manifestId);
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

    /**
     * 同一 run 的重复请求：本次执行没有领到业务，原 run 仍在跑。
     * 这是**本次请求**的失败，不是那条 run 的终局 —— 因此不发任何完成/失败通知。
     */
    public static final class DuplicateRunRefused extends RuntimeException {
        public DuplicateRunRefused(String message) {
            super(message);
        }
    }

    /** Gateway 未确认整轮完成。平台据此判本次 run 失败（而不是成功）。 */
    public static final class CompletionNotConfirmed extends RuntimeException {
        public CompletionNotConfirmed(String message) {
            super(message);
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
            // 只挂到**本次执行**的守卫上；拿不到（run_id 不匹配或未启用）就不挂，
            // 绝不猜一个别人的守卫。
            RunExecution execution = RunExecution.current(runId);
            if (execution == null) {
                return;
            }
            context.getBuilder().defaultAdvisors(execution.guard());
        }
    }
}
