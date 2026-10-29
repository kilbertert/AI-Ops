package com.trendpower.aiops.node;

import com.aizuda.snail.ai.common.dto.agent.ChatDispatchRequest;
import com.aizuda.snail.ai.agent.core.executor.client.ChatClientBuildContext;
import com.aizuda.snail.ai.agent.core.executor.client.ChatClientCustomizer;
import com.aizuda.snail.ai.agent.core.runtime.tool.ToolRuntime;
import com.trendpower.aiops.node.exec.ExecConfigViolation;
import com.trendpower.aiops.node.exec.GuardedChatModelFactory;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.context.annotation.Primary;
import org.springframework.core.Ordered;
import org.springframework.stereotype.Component;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * M2-CLOSE-01 的执行节点接缝，M3-NODE-GUARD-01 在此接入执行配置守卫。四件事，都在模型看到请求之前：
 *
 * <ol>
 *   <li><b>把工具集合收成只有受控工具</b>：用 {@link Primary} 的 {@link ToolRuntime}
 *       覆盖框架默认实现，不再解析 shell / HTTP / RAG / 技能 / 动态 MCP。仅改描述或
 *       隐藏清单都不算数（契约 §5）。</li>
 *   <li><b>把本次 run_id 交给工具上下文</b>：它来自 Snail 调度 DTO 的 {@code sid}，
 *       只在本进程内可见，不发给模型；同时把它从会被拼进 prompt 的字段里清掉。</li>
 *   <li><b>在发模型之前先核对本 run 的模型配置</b>：provider / adapter / 模型标识 / endpoint
 *       与受控清单比对，不符即显式失败。这一步是**同步**的 ⇒ 失败发生在任何模型与业务调用
 *       之前，计数天然为 0；同时它发生在工具被交给模型之前，因此不需要在拒绝路径上额外
 *       处理工具资源释放。</li>
 *   <li><b>固定无历史模式的输入约束</b>：历史消息 / 媒体附件 / 记忆上下文在这里显式拒绝，
 *       而不是等它们被拼进 prompt。</li>
 * </ol>
 *
 * 工具循环**中途**的漂移不在这里拦 —— 它由 {@code GuardedChatModel} 在每一轮的最终
 * ChatModel 边界拦住（工具循环的每一轮都会再次经过那里）。
 *
 * 模型参数里没有订单号、租户、用户、URL、agentId、run_id —— 工具只用上下文里的
 * run_id 去 Gateway 领取，业务输入由 Gateway 的授权记录决定。
 */
@Component
@Primary
class GovernedToolRuntime extends ToolRuntime {

    static final String RUN_ID_KEY = "aiops.run_id";

    private final ControlledChargingStatusTool tool;
    private final GuardedChatModelFactory modelFactory;
    private final CpmsReadTool.Authorization cpmsAuthorization;

    GovernedToolRuntime(ControlledChargingStatusTool tool, GuardedChatModelFactory modelFactory) {
        this(tool, modelFactory, null);
    }

    @org.springframework.beans.factory.annotation.Autowired
    GovernedToolRuntime(ControlledChargingStatusTool tool, GuardedChatModelFactory modelFactory,
                        CpmsGatewayAuthorization cpmsAuthorization) {
        // 父类构造要求四个解析器；传 null 是刻意的：本类**从不**调用 super.resolve，
        // 任何一次误用都会立刻 NPE，而不是悄悄把 shell 注册回模型。
        super(null, null, null, null);
        this.tool = tool;
        this.modelFactory = modelFactory;
        this.cpmsAuthorization = cpmsAuthorization;
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
        List<org.springframework.ai.tool.ToolCallback> resolved = resolvedTools();
        if (modelFactory.enabled()) {
            modelFactory.requireRunConfig(dispatchRequest.getModelConfig());
            requireFixedInputMode(dispatchRequest);
            requireOnlyResolvedTools(dispatchRequest, resolved);
        }
        scrubDispatchText(dispatchRequest, runId);
        return new ToolResolution(resolved, null);
    }

    private List<org.springframework.ai.tool.ToolCallback> resolvedTools() {
        var manifest = modelFactory.manifest();
        if (manifest == null) return List.of(tool);
        var result = new java.util.ArrayList<org.springframework.ai.tool.ToolCallback>();
        for (var declared : manifest.tools()) {
            if (ControlledChargingStatusTool.TOOL_NAME.equals(declared.name())) result.add(tool);
            else if (CpmsReadTool.NAMES.contains(declared.name()) && cpmsAuthorization != null)
                result.add(new CpmsReadTool(declared.name(), cpmsAuthorization));
            else throw new ExecConfigViolation("TOOLS", "发布清单包含未实现的工具");
        }
        if (result.stream().map(t -> t.getToolDefinition().name()).distinct().count() != result.size()
                || (result.contains(tool) && result.size() != 1)) {
            throw new ExecConfigViolation("TOOLS", "状态工具不能与管家CPMS工具混用或重复");
        }
        return List.copyOf(result);
    }

    /**
     * 有效工具面：本节点只解析一个受控工具，所以「解析出来的工具集合」必须正好是清单声明的那个。
     * 这里能看见的是 {@link ToolRuntime} 真正交付出去的集合 —— 即模型侧最终拿到的工具，
     * 而不是某个早期 DTO。清单启用时，任何多出来的工具（例如被后续扩展注册回来的 shell/HTTP）
     * 都会在这里被拒。
     */
    private void requireOnlyResolvedTools(ChatDispatchRequest dispatchRequest,
                                         List<org.springframework.ai.tool.ToolCallback> resolved) {
        modelFactory.requireResolvedTools(resolved);
        if (dispatchRequest.getMcpServers() != null && !dispatchRequest.getMcpServers().isEmpty()) {
            throw new ExecConfigViolation("TOOLS",
                    "固定受控工具模式下不接受动态 MCP 服务器，实际 " + dispatchRequest.getMcpServers().size() + " 个");
        }
        if (dispatchRequest.getSkills() != null && !dispatchRequest.getSkills().isEmpty()) {
            throw new ExecConfigViolation("TOOLS",
                    "固定受控工具模式下不接受技能工具，实际 " + dispatchRequest.getSkills().size() + " 个");
        }
    }

    /**
     * M2 固定无历史模式。清单声明的是 {@code history_policy="none"}，那么运行输入就必须真的是
     * 无历史、无媒体、无记忆 —— 否则清单说的和实际跑的不是同一件事。
     */
    private static void requireFixedInputMode(ChatDispatchRequest request) {
        List<ChatDispatchRequest.HistoryMessage> history = request.getHistoryMessages();
        if (history != null && !history.isEmpty()) {
            throw new ExecConfigViolation("INPUT_SHAPE",
                    "固定无历史模式下不接受历史消息，实际 " + history.size() + " 条");
        }
        List<ChatDispatchRequest.MediaAttachment> media = request.getMediaAttachments();
        if (media != null && !media.isEmpty()) {
            throw new ExecConfigViolation("INPUT_SHAPE",
                    "固定无历史模式下不接受媒体附件，实际 " + media.size() + " 个");
        }
        String memory = request.getMemoryContext();
        if (memory != null && !memory.isBlank()) {
            throw new ExecConfigViolation("INPUT_SHAPE", "固定无历史模式下不接受记忆上下文");
        }
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
    private final GuardedChatModelFactory factory;

    RunIdToolContextCustomizer(GuardedChatModelFactory factory) {
        this.factory = factory;
    }


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
            if (factory.manifest() != null) {
                toolContext.put("aiops.manifest_id", factory.manifest().id());
            }
        }
        context.getBuilder().defaultToolContext(toolContext);
    }
}
