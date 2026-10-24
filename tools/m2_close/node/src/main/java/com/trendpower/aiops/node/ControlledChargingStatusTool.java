package com.trendpower.aiops.node;

import com.fasterxml.jackson.databind.JsonNode;
import com.trendpower.aiops.node.exec.ExecManifest;
import com.trendpower.aiops.node.exec.GuardedChatModelFactory;
import org.springframework.ai.chat.model.ToolContext;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.definition.DefaultToolDefinition;
import org.springframework.ai.tool.definition.ToolDefinition;
import org.springframework.stereotype.Component;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 本切片**唯一**的可执行工具：向本环境的 Gateway 领取一次已授权的充电状态读取。
 *
 * 四个刻意的设计：
 * <ul>
 *   <li><b>零参数</b>：模型不能提交订单号、租户、用户、URL、agentId 或 run_id。
 *       业务输入来自 Gateway 的授权记录（契约 §3.4）。</li>
 *   <li><b>不是 {@code @Tool} 注解的 bean</b>：直接实现 {@link ToolCallback}，
 *       这样框架的自动发现不会把它注册两次，也不会因为扫描顺序产生两套定义。</li>
 *   <li><b>节点凭据只在请求头</b>：节点身份由 Gateway 从凭据派生；本类不发送
 *       任何自称的 node/host/agent 字段作为身份依据。</li>
 *   <li><b>启用清单时失败即中止整轮</b>：受控领取被拒意味着这次 run 的关键一步没有成立。
 *       返回 {@code "ERROR: …"} 文本会被模型当作普通工具结果继续跑，并最终产出一次
 *       「整轮完成」—— 那条路本片要堵死（M3-NODE-LINK-01）。未启用时保持 M2 的文本行为。</li>
 * </ul>
 */
@Component
public class ControlledChargingStatusTool implements ToolCallback {

    public static final String TOOL_NAME = "charging_status_snapshot";

    /** 受控工具的描述与输入 schema 是**发布契约**的一部分：清单声明的就是这两个值。 */
    public static final String DESCRIPTION =
            "Return the charging status snapshot for the order already authorized for this "
                    + "conversation. Takes no arguments; the order and identity come from the Gateway.";

    public static final String SCHEMA =
            "{\"type\":\"object\",\"properties\":{},\"required\":[],\"additionalProperties\":false}";

    private static final ToolDefinition DEFINITION = DefaultToolDefinition.builder()
            .name(TOOL_NAME)
            .description(DESCRIPTION)
            .inputSchema(SCHEMA)
            .build();

    private final GatewayClient gateway;
    private final GuardedChatModelFactory modelFactory;

    ControlledChargingStatusTool(GatewayClient gateway, GuardedChatModelFactory modelFactory) {
        this.gateway = gateway;
        this.modelFactory = modelFactory;
    }

    @Override
    public ToolDefinition getToolDefinition() {
        return DEFINITION;
    }

    @Override
    public String call(String toolInput) {
        // 模型没有参数可给；直接调用也不该成功——没有 run_id 就没有授权。
        return "ERROR: this tool requires the run context and cannot be called without it";
    }

    @Override
    public String call(String toolInput, ToolContext toolContext) {
        String runId = runId(toolContext);
        if (runId == null) {
            return refuse("ERROR: no authorized run in context", "no_run_context");
        }
        JsonNode body;
        try {
            body = gateway.post(
                    "/v1/internal/charging-status/claims/" + runId,
                    "{}",
                    (String) toolContext.getContext().getOrDefault("aiops.manifest_id", manifestId()));
        } catch (GatewayClient.GatewayRefused refused) {
            return refuse("ERROR: " + refused.getMessage(), "claim_refused");
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("order_no", body.path("order_no").asText(""));
        result.put("status_code", body.path("status_code").asText(""));
        result.put("status_role", body.path("status_role").asText(""));
        result.put("observed_at", body.path("observed_at").asText(""));
        result.put("source", body.path("source").asText(""));
        result.put("environment", body.path("environment").asText(""));
        result.put("gateway_version", body.path("gateway_version").asText(""));
        // 执行位置由 Gateway 的节点认证派生，工具原样转述；
        // 它不是模型文本，也不能由节点自报的头改写。
        result.put("actual_node_id", body.path("actual_node_id").asText(""));
        try {
            return new com.fasterxml.jackson.databind.ObjectMapper().writeValueAsString(result);
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            return refuse("ERROR: tool result could not be encoded", "encode_failed");
        }
    }

    /**
     * 本 run 已核对的执行配置身份。**取自守卫工厂持有的那一份清单对象**，
     * 不在工具里重读文件 —— 重读会得到「签发之后又被改过」的另一份身份，
     * 与本 run 实际运行的配置无关。
     */
    private String manifestId() {
        ExecManifest manifest = modelFactory.manifest();
        return manifest == null ? null : manifest.id();
    }

    /**
     * 失败的两条路，按有没有启用清单分流：
     * 启用 ⇒ 抛 {@code RuntimeException}，让整轮中止（框架不会把它转成普通工具文本）；
     * 未启用 ⇒ 保持 M2 的 {@code "ERROR: …"} 文本，行为与今天逐字一致。
     */
    private String refuse(String text, String code) {
        if (modelFactory.enabled()) {
            throw new ControlledClaimFailed(code, text);
        }
        return text;
    }

    @Override
    public String toString() {
        return TOOL_NAME;
    }

    private static String runId(ToolContext toolContext) {
        if (toolContext == null) {
            return null;
        }
        Object value = toolContext.getContext().get(GovernedToolRuntime.RUN_ID_KEY);
        String runId = value == null ? null : value.toString().trim();
        // run_ + 43 字符的 base64url；形状不对就不发请求。
        return runId != null && runId.matches("run_[A-Za-z0-9_-]{20,64}") ? runId : null;
    }

    /** 受控领取失败（启用清单时）。**不含凭据**：只带一个短码。 */
    public static final class ControlledClaimFailed extends RuntimeException {
        private final String code;

        ControlledClaimFailed(String code, String message) {
            super(message);
            this.code = code;
        }

        public String code() {
            return code;
        }
    }
}
