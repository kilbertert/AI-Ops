package com.trendpower.aiops.node;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Map;

/**
 * 节点到 Gateway 的**唯一** HTTP 出口：领取与整轮终局通知共用它。
 *
 * <p>为什么是一个类而不是两套调用：两条请求的凭据、超时、重定向策略与错误映射
 * 必须逐字一致 —— 两份实现就是两个慢慢分叉的安全边界。这里只做「发什么、回什么」，
 * 判定（谁可以领、谁可以完成）全在 Gateway。
 *
 * <p>凭据只在 {@code Authorization} 头里；本类不发送任何自称的 node/host/agent 字段。
 * 执行配置身份只在**调用方提供**时附加（启用清单的进程），不让客户端自己读文件。
 */
@Component
public class GatewayClient {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    static final String EXEC_CONFIG_HEADER = "X-AI-Ops-Exec-Config";

    private final String baseUrl;
    private final String nodeToken;
    private final String nodeBuild;
    private final Duration timeout;
    private final HttpClient client = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .followRedirects(HttpClient.Redirect.NEVER)
            .build();

    GatewayClient(
            @Value("${aiops.gateway.base-url}") String gatewayBaseUrl,
            @Value("${aiops.gateway.node-token}") String nodeToken,
            @Value("${aiops.node.build:unknown}") String nodeBuild,
            @Value("${aiops.gateway.timeout-seconds:30}") long timeoutSeconds) {
        this.baseUrl = gatewayBaseUrl.replaceAll("/+$", "");
        this.nodeToken = nodeToken;
        this.nodeBuild = nodeBuild;
        this.timeout = Duration.ofSeconds(timeoutSeconds);
    }

    /** 一次调用要么拿到 2xx 的 JSON 正文，要么抛 {@link GatewayRefused}（不泄露上游正文）。 */
    public JsonNode post(String path, String body, String execConfig) {
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + path))
                .timeout(timeout)
                .header("Authorization", "Bearer " + nodeToken)
                .header("X-AI-Ops-Node-Build", nodeBuild)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body));
        if (execConfig != null && !execConfig.isBlank()) {
            builder.header(EXEC_CONFIG_HEADER, execConfig);
        }
        HttpResponse<String> response;
        try {
            response = client.send(builder.build(), HttpResponse.BodyHandlers.ofString());
        } catch (IOException e) {
            // 连接/超时：不把底层异常文本带出去（可能含地址）。
            throw new GatewayRefused(0, "gateway_unreachable");
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new GatewayRefused(0, "gateway_interrupted");
        }
        if (response.statusCode() / 100 != 2) {
            throw new GatewayRefused(response.statusCode(), errorCode(response.body()));
        }
        try {
            return MAPPER.readTree(response.body());
        } catch (IOException e) {
            throw new GatewayRefused(response.statusCode(), "gateway_invalid_body");
        }
    }

    private static String errorCode(String body) {
        try {
            JsonNode node = MAPPER.readTree(body).path("error").path("code");
            return node.isTextual() ? node.asText() : "unknown";
        } catch (IOException e) {
            return "unknown";
        }
    }

    public String nodeBuild() {
        return nodeBuild;
    }

    /** 只携带状态码与 Gateway 自己的错误码 —— 上游正文可能含内部信息，不外传。 */
    public static final class GatewayRefused extends RuntimeException {

        private final int status;
        private final String code;

        GatewayRefused(int status, String code) {
            super("gateway refused (" + status + "): " + code);
            this.status = status;
            this.code = code;
        }

        public int status() {
            return status;
        }

        public String code() {
            return code;
        }

        /** 能否安全地把这条信息交给模型/日志：只有状态码与我们自己的错误码。 */
        public Map<String, Object> safeDetail() {
            return Map.of("status", status, "code", code);
        }
    }
}
