package com.trendpower.aiops.node;

import org.springframework.ai.chat.client.ChatClientRequest;
import org.springframework.ai.chat.client.ChatClientResponse;
import org.springframework.ai.chat.client.advisor.api.AdvisorChain;
import org.springframework.ai.chat.client.advisor.api.BaseAdvisor;

import java.util.concurrent.atomic.AtomicReference;

/**
 * 逐轮记录**真实消费到的 finish metadata**，用来判断最后模型轮是否完整。
 *
 * <p>为什么必须有它：实测（本片接缝探针）末轮被截断时，框架照样调用
 * {@code completionConsumer}、completion 文本是半截 —— 「循环结束回调」本身
 * **不能**证明上游把最后一轮发完了。唯一可观察的终止证据是 finish reason：
 * 正常两轮是 {@code [TOOL_CALLS, STOP]}，末轮截断是 {@code [TOOL_CALLS, _UNKNOWN]}，
 * 上游 500 时这一轮的 after 根本不到达。
 *
 * <p>order 必须**晚于** {@code ToolCallingAdvisor}（{@code -2147483348}）才会每一轮都经过；
 * 只实现 {@code CallAdvisor} 的 advisor 在流式路径上不生效，所以必须是 {@link BaseAdvisor}。
 * 每次 run 一个实例，**不共享跨 run 的可变状态**。
 */
public final class LoopTerminationGuard implements BaseAdvisor {

    /** 只有这个值代表「模型这一轮正常说完」。其余一律 fail closed。 */
    public static final String FINISHED = "STOP";

    private final AtomicReference<String> lastFinishReason = new AtomicReference<>(null);
    private final AtomicReference<Integer> rounds = new AtomicReference<>(0);

    @Override
    public String getName() {
        return "aiops-loop-termination";
    }

    @Override
    public int getOrder() {
        return 0;
    }

    @Override
    public ChatClientRequest before(ChatClientRequest request, AdvisorChain chain) {
        return request;
    }

    @Override
    public ChatClientResponse after(ChatClientResponse response, AdvisorChain chain) {
        String reason = null;
        if (response != null && response.chatResponse() != null
                && response.chatResponse().getResult() != null
                && response.chatResponse().getResult().getMetadata() != null) {
            reason = response.chatResponse().getResult().getMetadata().getFinishReason();
        }
        lastFinishReason.set(reason);
        rounds.updateAndGet(n -> n == null ? 1 : n + 1);
        return response;
    }

    /** 最后一次观测到的 finish reason；从未观测到（例如首轮就失败）为 {@code null}。 */
    public String lastFinishReason() {
        return lastFinishReason.get();
    }

    public int rounds() {
        Integer value = rounds.get();
        return value == null ? 0 : value;
    }

    /** 最后一轮是否被明确标记为正常结束。 */
    public boolean finishedCleanly() {
        return FINISHED.equals(lastFinishReason.get());
    }
}
