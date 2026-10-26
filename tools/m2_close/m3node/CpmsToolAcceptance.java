package com.trendpower.aiops.node;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.springframework.ai.chat.model.ToolContext;

import java.net.InetSocketAddress;
import java.time.Instant;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;

/** 真 Java 工具跨真实 HTTP；CPMS 与授权源为明确的合成替身。 */
public final class CpmsToolAcceptance {
    static final ObjectMapper JSON = new ObjectMapper();
    static final String RUN = "run_" + "a".repeat(43);
    static final String MANIFEST = "b".repeat(64);
    static final ToolContext CONTEXT = new ToolContext(Map.of(
            GovernedToolRuntime.RUN_ID_KEY, RUN, "aiops.manifest_id", MANIFEST));
    static int passed;
    static String mode = "detail";
    static final AtomicInteger calls = new AtomicInteger();
    static volatile boolean revoked;
    static int recorded;
    static CpmsReadTool.Grant grant;
    static final CpmsReadTool.Authorization AUTH = new CpmsReadTool.Authorization() {
        @Override public CpmsReadTool.Grant require(String run, String manifest, String tool) {
            check(run.equals(RUN) && manifest.equals(MANIFEST), "authorization identities");
            if (revoked) throw CpmsReadTool.refused("revoked");
            return grant;
        }
        @Override public void record(String run, String manifest, String tool,
                                     CpmsReadTool.Grant value, JsonNode evidence) {
            if (revoked) throw CpmsReadTool.refused("revoked");
            check(!evidence.toString().contains("PRIVATE"), "evidence redaction");
            recorded++;
        }
    };

    public static void main(String[] args) throws Exception {
        var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            calls.incrementAndGet();
            try {
                check("GET".equals(exchange.getRequestMethod()), "only GET");
                check("Bearer PRIVATE-TOKEN".equals(exchange.getRequestHeaders().getFirst("Authorization")), "CPMS credential");
                check("T1".equals(exchange.getRequestHeaders().getFirst("tenant-id")), "CPMS tenant");
                check("admin".equals(exchange.getRequestHeaders().getFirst("client-type")), "CPMS company entry");
                String path = exchange.getRequestURI().toString();
                check(path.equals("/chOrderInfo/getOrderInfoDetail/ORDER-1")
                        || path.equals("/iotChargingDevice/getChargingMonitor/ORDER-1?current=1&size=50"), "fixed paths");
                String row = "{\"orderNo\":\"ORDER-1\",\"tenantId\":\"T1\",\"siteId\":\"S1\","
                        + "\"status\":2,\"createdTime\":\"2026-10-25 08:00:00\",\"phone\":\"PRIVATE-PHONE\"}";
                String body = "{\"code\":0,\"data\":{\"orderNo\":\"ORDER-1\",\"siteId\":\"S1\",\"chOrderInfoModel\":" + row + "}}";
                if (mode.equals("samples")) body = "{\"code\":0,\"data\":{\"records\":[" + row + "],\"total\":80,\"current\":1,\"size\":50}}";
                if (mode.equals("wrong-order")) body = body.replace("ORDER-1", "ORDER-2");
                if (mode.equals("wrong-tenant")) body = body.replace("T1", "T2");
                if (mode.equals("wrong-site")) body = body.replace("S1", "S2");
                if (mode.equals("missing-identity")) body = body.replace("\"tenantId\":\"T1\",", "");
                if (mode.equals("envelope-error")) body = body.replace("\"code\":0", "\"code\":1");
                if (mode.equals("oversized")) body = "x".repeat(262145);
                if (mode.equals("invalid-json")) body = "PRIVATE-INVALID";
                if (mode.equals("revoked")) revoked = true;
                if (mode.equals("redirect")) exchange.getResponseHeaders().set("Location", "/credential-leak");
                int status = mode.equals("http-error") ? 500 : mode.equals("redirect") ? 302 : 200;
                byte[] bytes = body.getBytes(java.nio.charset.StandardCharsets.UTF_8);
                exchange.sendResponseHeaders(status, bytes.length);
                exchange.getResponseBody().write(bytes);
            } finally { exchange.close(); }
        });
        server.start();
        try {
            grant = new CpmsReadTool.Grant("grant-1", "T1", Set.of("S1"), "ORDER-1",
                    "http://127.0.0.1:" + server.getAddress().getPort(), "PRIVATE-TOKEN", Instant.now().plusSeconds(60));
            var detail = new CpmsReadTool(CpmsReadTool.DETAIL, AUTH);
            var samples = new CpmsReadTool(CpmsReadTool.SAMPLES, AUTH);
            JsonNode result = JSON.readTree(detail.call("{}", CONTEXT));
            check(result.path("data").path("status").asInt() == 2 && !result.toString().contains("PRIVATE"), "detail result");
            check(recorded == 1 && calls.get() == 1, "one request and receipt");
            mode = "samples";
            result = JSON.readTree(samples.call("{}", CONTEXT));
            check(result.path("data").path("truncated").asBoolean()
                    && !result.path("data").path("complete_timeline").asBoolean(), "honest samples");
            for (String input : new String[]{"{\"orderNo\":\"OTHER\"}", "[]", "null", "", "{\"url\":\"https://example.com\"}"}) {
                int before = calls.get();
                refuse(() -> detail.call(input, CONTEXT), "invalid arguments");
                check(calls.get() == before, "arguments refused before HTTP");
            }
            refuse(() -> detail.call("{}"), "no context");
            for (String test : new String[]{"wrong-order", "wrong-tenant", "wrong-site", "missing-identity",
                    "envelope-error", "oversized", "invalid-json", "http-error", "redirect", "revoked"}) {
                mode = test; revoked = false;
                int before = recorded, beforeCalls = calls.get();
                refuse(() -> detail.call("{}", CONTEXT), test);
                check(recorded == before && calls.get() == beforeCalls + 1, "no retries or rejected evidence: " + test);
            }
            revoked = false;
            grant = new CpmsReadTool.Grant("grant-1", "T1", Set.of("S1"), "ORDER-1", grant.baseUrl(), "PRIVATE-TOKEN", Instant.now().minusSeconds(1));
            int before = calls.get();
            refuse(() -> detail.call("{}", CONTEXT), "expired");
            check(calls.get() == before, "expired before HTTP");
            System.out.println("CPMS_TOOL_ACCEPTANCE PASS assertions=" + passed + " http_calls=" + calls.get());
        } finally { server.stop(0); }
    }

    static void refuse(Runnable action, String label) {
        try { action.run(); throw new AssertionError("accepted: " + label); }
        catch (CpmsReadTool.CpmsRefused expected) {
            check(!expected.getMessage().contains("PRIVATE"), label);
        }
    }
    static void check(boolean value, String label) {
        if (!value) throw new AssertionError(label);
        passed++;
    }
}
