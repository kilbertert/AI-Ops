package com.trendpower.aiops.node;

import com.aizuda.snail.ai.common.dto.agent.ChatDispatchRequest;
import com.aizuda.snail.ai.agent.core.executor.client.ChatClientBuildContext;
import com.aizuda.snail.ai.agent.core.executor.client.ChatClientCustomizer;
import com.aizuda.snail.ai.agent.core.runtime.tool.ToolRuntime;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.context.annotation.Primary;
import org.springframework.core.Ordered;
import org.springframework.stereotype.Component;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * M2-CLOSE-01 的执行节点接缝。两件事，都在模型看到请求之前发生：
 *
 * <ol>
 *   <li><b>把工具集合收成只有受控工具</b>：用 {@link Primary} 的 {@link ToolRuntime}
 *       覆盖框架默认实现，不再解析 shell / HTTP / RAG / 技能 / 动态 MCP。仅改描述或
 *       隐藏清单都不算数（契约 §5）。</li>
 *   <li><b>把本次 run_id 交给工具上下文</b>：它来自 Snail 调度 DTO 的 {@code sid}，
 *       只在本进程内可见，不发给模型；同时把它从会被拼进 prompt 的字段里清掉。</li>
 * </ol>
 *
 * 模型参数里没有订单号、租户、用户、URL、agentId、run_id —— 工具只用上下文里的
 * run_id 去 Gateway 领取，业务输入由 Gateway 的授权记录决定。
 */
@Component
@Primary
class GovernedToolRuntime extends ToolRuntime {

    static final String RUN_ID_KEY = "aiops.run_id";

    private final ControlledChargingStatusTool tool;

    GovernedToolRuntime(ControlledChargingStatusTool tool) {
        // 父类构造要求四个解析器；传 null 是刻意的：本类**从不**调用 super.resolve，
        // 任何一次误用都会立刻 NPE，而不是悄悄把 shell 注册回模型。
        super(null, null, null, null);
        this.tool = tool;
    }

    @Override
    public ToolResolution resolve(ChatDispatchRequest dispatchRequest) {
        if (dispatchRequest == null) {
            throw new IllegalArgumentException("ChatDispatchRequest is required");
        }
        String runId = dispatchRequest.getSid();
        if (runId == null || runId.isBlank()) {
            // 没有 run_id 就没有可领取的授权；给模型一个空工具集合，
            // 绝不回退到框架的默认解析。
            return new ToolResolution(List.of(), null);
        }
        scrubDispatchText(dispatchRequest, runId);
        return new ToolResolution(List.of(tool), null);
    }

    /** 把 run_id 从会被拼进 prompt 的字段里去掉：它是关联编号，不该出现在模型可见文本。 */
    private void scrubDispatchText(ChatDispatchRequest request, String runId) {
        if (request.getAgentConfig() != null) {
            request.getAgentConfig().setInstruction(strip(request.getAgentConfig().getInstruction(), runId));
            request.getAgentConfig().setName(strip(request.getAgentConfig().getName(), runId));
        }
        request.setSystemPrompt(strip(request.getSystemPrompt(), runId));
        request.setMemoryContext(strip(request.getMemoryContext(), runId));
    }

    private static String strip(String value, String runId) {
        return value == null ? null : value.replace(runId, "[run]");
    }
}

/**
 * 把 run_id 绑到 {@code ToolContext}。Spring AI 的 ToolContext 不进入模型请求，
 * 但**工具结果会**（契约 §7），所以这里只放编号，不放任何身份或业务数据。
 */
@Component
class RunIdToolContextCustomizer implements ChatClientCustomizer {

    @Override
    public int order() {
        return Ordered.HIGHEST_PRECEDENCE;
    }

    @Override
    public void customize(ChatClientBuildContext context) {
        ChatDispatchRequest dispatch =
                context.getRequest() == null ? null : context.getRequest().getDispatchRequest();
        String runId = dispatch == null ? null : dispatch.getSid();
        Map<String, Object> toolContext = new HashMap<>();
        if (runId != null && !runId.isBlank()) {
            toolContext.put(GovernedToolRuntime.RUN_ID_KEY, runId);
        }
        context.getBuilder().defaultToolContext(toolContext);
    }
}
