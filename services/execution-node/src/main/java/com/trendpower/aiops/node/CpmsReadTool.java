package com.trendpower.aiops.node;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.ai.chat.model.ToolContext;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.definition.DefaultToolDefinition;
import org.springframework.ai.tool.definition.ToolDefinition;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Set;

/** CPMS 正式只读接口；任务身份和授权由装配层提供，不接受模型参数。 */
public final class CpmsReadTool implements ToolCallback {
    /** 工具授权契约版本。必须与 Gateway 侧 `operator_cpms.TOOL_CONTRACT` 逐字相同。 */
    public static final String CONTRACT = "request-target/1";
    private static final ObjectMapper JSON = new ObjectMapper()
            .enable(com.fasterxml.jackson.databind.DeserializationFeature.FAIL_ON_TRAILING_TOKENS)
            .enable(com.fasterxml.jackson.core.JsonParser.Feature.STRICT_DUPLICATE_DETECTION);
    private static final int MAX_BYTES = 262144;
    private static final String ID = "[A-Za-z0-9_-]{1,80}";
    private final ToolDefinition definition;
    private final Authorization authorization;
    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5)).followRedirects(HttpClient.Redirect.NEVER).build();

    /** 每次调用重新验证授权；交付前记录证据并再次核验，任何失败均不交付正文。 */
    public interface Authorization {
        /** ``arguments`` 是模型提议的参数；节点只检查形状，语义（能不能用）由网关判。 */
        Grant require(String runId, String manifestId, String tool, JsonNode arguments);
        void record(String runId, String manifestId, String tool, Grant grant, JsonNode evidence);
    }

    /** 这一次要发的请求。**节点不再自己拼路径** —— 授权给什么就发什么。 */
    public record Request(String method, String path, String query) {
        public Request {
            if (!"GET".equals(method) || path == null || !path.startsWith("/")
                    || path.length() > 2048 || path.contains("..") || path.contains("\n")
                    || query == null || query.length() > 2048 || query.contains("\n")
                    || (path + (query.isEmpty() ? "" : "?" + query)).length() > 4096) {
                throw refused("invalid_request_target");
            }
        }

        String target() {
            return query.isEmpty() ? path : path + "?" + query;
        }
    }

    public record Grant(String authorizationId, String tenantId, Set<String> siteIds,
                        JsonNode inputs, Request request, String baseUrl, String bearer,
                        Instant expiresAt) {
        public Grant {
            siteIds = Set.copyOf(siteIds);
            if (authorizationId == null || authorizationId.isBlank()
                    || tenantId == null || !tenantId.matches(ID)
                    || inputs == null || !inputs.isObject() || inputs.isEmpty()
                    || request == null || siteIds.isEmpty()
                    || siteIds.size() > 1000 || siteIds.stream().anyMatch(id -> !id.matches(ID))
                    || bearer == null || bearer.isBlank() || bearer.length() > 16384
                    || bearer.chars().anyMatch(c -> c <= 32 || c >= 127) || expiresAt == null) {
                throw refused("invalid_authorization");
            }
            URI uri = URI.create(baseUrl);
            boolean loopback = Set.of("127.0.0.1", "[::1]").contains(uri.getHost());
            if (uri.getHost() == null || uri.getUserInfo() != null || uri.getQuery() != null
                    || uri.getFragment() != null || uri.normalize().compareTo(uri) != 0
                    || !("https".equals(uri.getScheme()) || (loopback && "http".equals(uri.getScheme())))) {
                throw refused("invalid_cpms_endpoint");
            }
            baseUrl = baseUrl.replaceAll("/+$", "");
        }

        /** 输入里的订单号；两个现役工具都需要它，缺失即无效授权。 */
        String orderNo() {
            JsonNode value = inputs.path("order_no");
            if (!value.isTextual() || !value.asText().matches(ID)) throw refused("invalid_authorization");
            return value.asText();
        }

        @Override public String toString() { return "CpmsGrant[redacted]"; }
    }

    public CpmsReadTool(String name, Authorization authorization) {
        if (!OperatorTools.declares(name) || authorization == null) throw refused("unknown_tool");
        this.authorization = authorization;
        // schema 取自**声明**，不是状态工具那个空参数常量：每个工具的模型参数不同，
        // 而发布清单里的 schema 也来自同一份声明。两者必须是同一个字符串，
        // `verifyTools` 比的就是它的 sha256（2026-11-01 站点工具曾因此整轮失败）。
        this.definition = DefaultToolDefinition.builder().name(name)
                .description(OperatorTools.description(name))
                .inputSchema(OperatorTools.inputSchema(name)).build();
    }

    @Override public ToolDefinition getToolDefinition() { return definition; }
    @Override public String call(String input) { throw refused("run_context_required"); }

    @Override public String call(String input, ToolContext context) {
        try {
            JsonNode parameters = JSON.readTree(input == null ? "" : input);
            if (parameters == null || !parameters.isObject()) throw refused("invalid_arguments");
            // 只检查形状：**能不能用**由网关判，因为只有它知道用户原话。
            for (var parameter : parameters.properties()) {
                if (!parameter.getValue().isTextual() || parameter.getValue().asText().length() > 80) {
                    throw refused("invalid_arguments");
                }
            }
            if (context == null) throw refused("run_context_required");
            Object id = context.getContext().get(GovernedToolRuntime.RUN_ID_KEY);
            Object manifest = context.getContext().get("aiops.manifest_id");
            if (!(id instanceof String runId) || !runId.matches("run_[A-Za-z0-9_-]{43}")
                    || !(manifest instanceof String manifestId) || !manifestId.matches("[a-f0-9]{64}")) {
                throw refused("run_context_required");
            }
            String tool = definition.name();
            Grant grant = authorization.require(runId, manifestId, tool, parameters);
            requireLive(grant);
            // 请求目标来自授权，不来自工具名 —— 这正是「新增一条只读路径不必改本文件」。
            JsonNode body = get(grant, grant.request().target());
            // 响应形状由**声明**决定，不由工具名决定：新增一个复用已有形状的工具，本文件不用改。
            String kind = OperatorTools.kind(tool);
            ObjectNode data = "page".equals(kind) ? samples(body, grant, tool) : detail(body, grant, tool);
            ObjectNode evidence = JSON.createObjectNode();
            evidence.put("tool", tool);
            evidence.set("inputs", grant.inputs());
            evidence.put("source", OperatorTools.source(tool));
            evidence.put("observed_at", Instant.now().toString());
            evidence.set("data", data);
            requireLive(grant);
            Grant current = authorization.require(runId, manifestId, tool, parameters);
            requireLive(current);
            if (!grant.equals(current)) throw refused("authorization_changed");
            authorization.record(runId, manifestId, tool, grant, evidence);
            requireLive(grant);
            return JSON.writeValueAsString(evidence);
        } catch (CpmsRefused failure) {
            throw failure;
        } catch (InterruptedException failure) {
            Thread.currentThread().interrupt();
            throw refused("interrupted");
        } catch (Exception failure) {
            // 上游正文、地址与访问令牌都不进入异常，避免模型或框架日志泄露。
            throw refused("cpms_unavailable");
        }
    }

    private JsonNode get(Grant grant, String path) throws Exception {
        long remaining = Duration.between(Instant.now(), grant.expiresAt()).toMillis();
        if (remaining <= 0) throw refused("authorization_expired");
        HttpRequest request = HttpRequest.newBuilder(URI.create(grant.baseUrl() + path))
                .timeout(Duration.ofMillis(Math.min(remaining, 10000)))
                .header("Authorization", "Bearer " + grant.bearer())
                .header("tenant-id", grant.tenantId()).header("client-type", "admin")
                .header("Accept", "application/json").GET().build();
        var pending = http.sendAsync(request, new BoundedJsonBodyHandler(MAX_BYTES));
        HttpResponse<byte[]> response;
        try {
            response = pending.get(Math.min(remaining, 10000), java.util.concurrent.TimeUnit.MILLISECONDS);
        } finally {
            // 总时限覆盖响应体读取；仅 HttpRequest.timeout 不作为完整读取截止的证明。
            if (!pending.isDone()) pending.cancel(true);
        }
        if (response.statusCode() != 200) throw refused("cpms_http_error");
        JsonNode envelope = JSON.readTree(response.body());
        if (envelope == null || !envelope.isObject() || !envelope.path("code").isIntegralNumber()
                || !"0".equals(envelope.path("code").asText()) || !envelope.path("data").isObject()) {
            throw refused("cpms_invalid_response");
        }
        return envelope.get("data");
    }

    private static ObjectNode detail(JsonNode body, Grant grant, String tool) {
        JsonNode row = body.path("chOrderInfoModel");
        identity(row, grant);
        if (!body.path("orderNo").isTextual() || !body.path("siteId").isTextual()
                || !grant.orderNo().equals(body.path("orderNo").asText())
                || !row.path("siteId").asText().equals(body.path("siteId").asText())) {
            throw refused("cpms_scope_mismatch");
        }
        // 字段表与 Gateway 的 `operator_cpms.DETAIL_FIELDS` **必须逐项相同**：
        // `record` 要求节点上报的证据与 Gateway 自己投影的那份相等，多一个少一个都会
        // 让整轮失败。这里补的是金额/额定功率/SOC —— 没有它们，问电费与问电压只能得到
        // 同一份「缺证」报告（41 实测，订单 2113549588485718017）。
        // 取证字段来自**声明**：这张表就是模型能回答什么，过去两侧各写一份，靠人记住同步。
        return select(row, OperatorTools.evidenceFields(tool));
    }

    private static ObjectNode samples(JsonNode body, Grant grant, String tool) {
        JsonNode rows = body.path("records");
        if (!rows.isArray() || rows.size() > 50 || !body.path("total").canConvertToLong()
                || !body.path("total").isIntegralNumber() || body.path("total").asLong() < rows.size()
                || !body.path("current").isIntegralNumber() || !"1".equals(body.path("current").asText())
                || !body.path("size").isIntegralNumber() || !"50".equals(body.path("size").asText())) {
            throw refused("cpms_invalid_page");
        }
        ObjectNode result = JSON.createObjectNode();
        var selected = result.putArray("records");
        List<String> fields = OperatorTools.evidenceFields(tool);
        for (JsonNode row : rows) {
            identity(row, grant);
            selected.add(select(row, fields));
        }
        // 分页标记与「缺样本」的措辞也来自声明：它们是这个工具的证据语义，不是引擎的常识。
        result.put("total", body.path("total").asLong())
                .put("sample_limit", OperatorTools.flagInt(tool, "sample_limit"))
                .put("truncated", body.path("total").asLong() > rows.size())
                .put("ordering_guaranteed", OperatorTools.flagBool(tool, "ordering_guaranteed"))
                .put("complete_timeline", OperatorTools.flagBool(tool, "complete_timeline"))
                // 「没上报过样本」与「这批被截断」是两件事，证据里各占一个字段。
                .put("samples_returned", rows.size())
                .put("absence", rows.isEmpty() ? OperatorTools.flagText(tool, "absence_when_empty") : null);
        return result;
    }

    private static void identity(JsonNode row, Grant grant) {
        if (!row.isObject() || !row.path("orderNo").isTextual() || !row.path("tenantId").isTextual()
                || !row.path("siteId").isTextual() || !grant.orderNo().equals(row.path("orderNo").asText())
                || !grant.tenantId().equals(row.path("tenantId").asText())
                || !grant.siteIds().contains(row.path("siteId").asText())) {
            throw refused("cpms_scope_mismatch");
        }
    }

    private static ObjectNode select(JsonNode row, List<String> fields) {
        ObjectNode result = JSON.createObjectNode();
        for (String field : fields) {
            JsonNode value = row.get(field);
            if (value != null) {
                if (!value.isValueNode() || (value.isTextual() && value.asText().length() > 1000)) {
                    throw refused("cpms_invalid_field");
                }
                result.set(field, value);
            }
        }
        return result;
    }

    private static void requireLive(Grant grant) {
        if (grant == null || !Instant.now().isBefore(grant.expiresAt())) throw refused("authorization_expired");
    }

    static CpmsRefused refused(String code) { return new CpmsRefused(code); }
    static final class CpmsRefused extends RuntimeException {
        CpmsRefused(String code) { super(code); }
    }
}
