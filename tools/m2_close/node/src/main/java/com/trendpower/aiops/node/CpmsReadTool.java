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
import java.util.Set;

/** CPMS 正式只读接口；任务身份和授权由装配层提供，不接受模型参数。 */
public final class CpmsReadTool implements ToolCallback {
    public static final String DETAIL = "cpms_order_detail";
    public static final String SAMPLES = "cpms_charging_samples";
    public static final Set<String> NAMES = Set.of(DETAIL, SAMPLES);
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
        Grant require(String runId, String manifestId, String tool);
        void record(String runId, String manifestId, String tool, Grant grant, JsonNode evidence);
    }

    public record Grant(String authorizationId, String tenantId, Set<String> siteIds,
                        String orderNo, String baseUrl, String bearer, Instant expiresAt) {
        public Grant {
            siteIds = Set.copyOf(siteIds);
            if (authorizationId == null || authorizationId.isBlank()
                    || tenantId == null || !tenantId.matches(ID)
                    || orderNo == null || !orderNo.matches(ID) || siteIds.isEmpty()
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

        @Override public String toString() { return "CpmsGrant[redacted]"; }
    }

    public CpmsReadTool(String name, Authorization authorization) {
        if (!NAMES.contains(name) || authorization == null) throw refused("unknown_tool");
        this.authorization = authorization;
        this.definition = DefaultToolDefinition.builder().name(name)
                .description(DETAIL.equals(name)
                        ? "Read diagnostic fields for the authorized order from CPMS. Takes no arguments."
                        : "Read up to 50 CPMS charging samples for the authorized order. Ordering and completeness are not guaranteed. Takes no arguments.")
                .inputSchema(ControlledChargingStatusTool.SCHEMA).build();
    }

    @Override public ToolDefinition getToolDefinition() { return definition; }
    @Override public String call(String input) { throw refused("run_context_required"); }

    @Override public String call(String input, ToolContext context) {
        try {
            JsonNode parameters = JSON.readTree(input == null ? "" : input);
            if (parameters == null || !parameters.isObject() || !parameters.isEmpty()) {
                throw refused("arguments_forbidden");
            }
            if (context == null) throw refused("run_context_required");
            Object id = context.getContext().get(GovernedToolRuntime.RUN_ID_KEY);
            Object manifest = context.getContext().get("aiops.manifest_id");
            if (!(id instanceof String runId) || !runId.matches("run_[A-Za-z0-9_-]{43}")
                    || !(manifest instanceof String manifestId) || !manifestId.matches("[a-f0-9]{64}")) {
                throw refused("run_context_required");
            }
            String tool = definition.name();
            Grant grant = authorization.require(runId, manifestId, tool);
            requireLive(grant);
            String path = DETAIL.equals(tool)
                    ? "/chOrderInfo/getOrderInfoDetail/" + grant.orderNo()
                    : "/iotChargingDevice/getChargingMonitor/" + grant.orderNo() + "?current=1&size=50";
            JsonNode body = get(grant, path);
            ObjectNode data = DETAIL.equals(tool) ? detail(body, grant) : samples(body, grant);
            ObjectNode evidence = JSON.createObjectNode();
            evidence.put("tool", tool).put("order_no", grant.orderNo())
                    .put("source", DETAIL.equals(tool) ? "cpms.orderDetail" : "cpms.chargingMonitor")
                    .put("observed_at", Instant.now().toString()).set("data", data);
            requireLive(grant);
            Grant current = authorization.require(runId, manifestId, tool);
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

    private static ObjectNode detail(JsonNode body, Grant grant) {
        JsonNode row = body.path("chOrderInfoModel");
        identity(row, grant);
        if (!body.path("orderNo").isTextual() || !body.path("siteId").isTextual()
                || !grant.orderNo().equals(body.path("orderNo").asText())
                || !row.path("siteId").asText().equals(body.path("siteId").asText())) {
            throw refused("cpms_scope_mismatch");
        }
        return select(row, "orderNo", "siteId", "deviceId", "childDeviceId", "deviceCode",
                "childDeviceCode", "deviceProtocol", "status", "createdTime", "stopTime",
                "stoppedReasonCode", "stoppedReasonContent", "electricity");
    }

    private static ObjectNode samples(JsonNode body, Grant grant) {
        JsonNode rows = body.path("records");
        if (!rows.isArray() || rows.size() > 50 || !body.path("total").canConvertToLong()
                || !body.path("total").isIntegralNumber() || body.path("total").asLong() < rows.size()
                || !body.path("current").isIntegralNumber() || !"1".equals(body.path("current").asText())
                || !body.path("size").isIntegralNumber() || !"50".equals(body.path("size").asText())) {
            throw refused("cpms_invalid_page");
        }
        ObjectNode result = JSON.createObjectNode();
        var selected = result.putArray("records");
        for (JsonNode row : rows) {
            identity(row, grant);
            selected.add(select(row, "orderNo", "siteId", "deviceId", "childDeviceId",
                    "createdTime", "power", "voltage", "current", "chargingElectricityQuantity",
                    "soc", "temperature", "batteryMaxTemperature", "batteryMinTemperature"));
        }
        result.put("total", body.path("total").asLong()).put("sample_limit", 50)
                .put("truncated", body.path("total").asLong() > rows.size())
                .put("ordering_guaranteed", false).put("complete_timeline", false);
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

    private static ObjectNode select(JsonNode row, String... fields) {
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
