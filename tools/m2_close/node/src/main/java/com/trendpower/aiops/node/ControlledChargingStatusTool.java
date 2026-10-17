package com.trendpower.aiops.node;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.ai.chat.model.ToolContext;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.definition.DefaultToolDefinition;
import org.springframework.ai.tool.definition.ToolDefinition;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Map;

/**
 * 本切片**唯一**的可执行工具：向本环境的 Gateway 领取一次已授权的充电状态读取。
 *
 * 三个刻意的设计：
 * <ul>
 *   <li><b>零参数</b>：模型不能提交订单号、租户、用户、URL、agentId 或 run_id。
 *       业务输入来自 Gateway 的授权记录（契约 §3.4）。</li>
 *   <li><b>不是 {@code @Tool} 注解的 bean</b>：直接实现 {@link ToolCallback}，
 *       这样框架的自动发现不会把它注册两次，也不会因为扫描顺序产生两套定义。</li>
 *   <li><b>节点凭据只在请求头</b>：节点身份由 Gateway 从凭据派生；本类不发送
 *       任何自称的 node/host/agent 字段作为身份依据。</li>
 * </ul>
 */
@Component
class ControlledChargingStatusTool implements ToolCallback {

    static final String TOOL_NAME = "charging_status_snapshot";

    private static final String INPUT_SCHEMA = """
            {"type":"object","properties":{},"required":[],"additionalProperties":false}
            """;

    private static final ToolDefinition DEFINITION = DefaultToolDefinition.builder()
            .name(TOOL_NAME)
            .description("Return the charging status snapshot for the order already authorized for this "
                    + "conversation. Takes no arguments; the order and identity come from the Gateway.")
            .inputSchema(INPUT_SCHEMA)
            .build();

    private final String gatewayBaseUrl;
    private final String nodeToken;
    private final String nodeBuild;
    private final Duration timeout;
    private final HttpClient client = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .followRedirects(HttpClient.Redirect.NEVER)
            .build();
    private final ObjectMapper mapper = new ObjectMapper();

    ControlledChargingStatusTool(
            @Value("${aiops.gateway.base-url}") String gatewayBaseUrl,
            @Value("${aiops.gateway.node-token}") String nodeToken,
            @Value("${aiops.node.build:unknown}") String nodeBuild,
            @Value("${aiops.gateway.timeout-seconds:30}") long timeoutSeconds) {
        this.gatewayBaseUrl = gatewayBaseUrl.replaceAll("/+$", "");
        this.nodeToken = nodeToken;
        this.nodeBuild = nodeBuild;
        this.timeout = Duration.ofSeconds(timeoutSeconds);
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
            return "ERROR: no authorized run in context";
        }
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(gatewayBaseUrl + "/v1/internal/charging-status/claims/" + runId))
                .timeout(timeout)
                .header("Authorization", "Bearer " + nodeToken)
                .header("X-AI-Ops-Node-Build", nodeBuild)
                .header("Content-Type", "application/json")
                // 空对象而不是空 body：Gateway 的严格模型会拒掉任何字段，但要求一个合法 JSON 体。
                .POST(HttpRequest.BodyPublishers.ofString("{}"))
                .build();
        try {
            HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 200) {
                // 上游正文可能含内部码；只透出状态码与 Gateway 自己的错误码。
                return "ERROR: gateway refused (" + response.statusCode() + "): " + errorCode(response.body());
            }
            JsonNode body = mapper.readTree(response.body());
            return mapper.writeValueAsString(Map.of(
                    "order_no", body.path("order_no").asText(""),
                    "status_code", body.path("status_code").asText(""),
                    "status_role", body.path("status_role").asText(""),
                    "observed_at", body.path("observed_at").asText(""),
                    "source", body.path("source").asText(""),
                    "environment", body.path("environment").asText(""),
                    "gateway_version", body.path("gateway_version").asText(""),
                    // 执行位置由 Gateway 的节点认证派生，工具原样转述；
                    // 它不是模型文本，也不能由节点自报的头改写。
                    "actual_node_id", body.path("actual_node_id").asText("")));
        } catch (Exception e) {
            return "ERROR: gateway unreachable";
        }
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

    private String errorCode(String body) {
        try {
            return mapper.readTree(body).path("error").path("code").asText("unknown");
        } catch (Exception e) {
            return "unknown";
        }
    }
}
