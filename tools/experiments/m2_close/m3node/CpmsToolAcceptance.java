package com.trendpower.aiops.node;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.springframework.ai.chat.model.ToolContext;

import java.net.InetSocketAddress;
import java.time.Instant;
import java.util.List;
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
    static String contractEcho = CpmsReadTool.CONTRACT;
    static final CpmsReadTool.Authorization AUTH = new CpmsReadTool.Authorization() {
        @Override public CpmsReadTool.Grant require(String run, String manifest, String tool,
                                                    JsonNode arguments) {
            check(run.equals(RUN) && manifest.equals(MANIFEST), "authorization identities");
            check(arguments != null && arguments.isObject(), "model arguments reach the gateway");
            lastArguments = arguments;
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

    /** 旧 Gateway 的回执：没有 `contract`、没有 `request`，还带一个 `order_no`。 */
    static String staleAuthorization(String base) {
        return "{\"run_id\":\"" + RUN + "\",\"tool\":\"cpms_order_detail\",\"manifest_id\":\"" + MANIFEST
                + "\",\"authorization_id\":\"g\",\"tenant_id\":\"T1\",\"site_ids\":[\"S1\"],"
                + "\"order_no\":\"ORDER-1\",\"base_url\":\"" + base + "\",\"bearer\":\"PRIVATE-TOKEN\","
                + "\"expires_at\":\"" + Instant.now().plusSeconds(60) + "\"}";
    }

    /** 新 Gateway 的回执，形状与 `operator_cpms.grant()` 一致。 */
    static String currentAuthorization(String base) {
        return "{\"run_id\":\"" + RUN + "\",\"tool\":\"cpms_order_detail\",\"manifest_id\":\"" + MANIFEST
                + "\",\"authorization_id\":\"g\",\"tenant_id\":\"T1\",\"site_ids\":[\"S1\"],"
                + "\"inputs\":{\"order_no\":\"ORDER-1\"},"
                + "\"request\":{\"method\":\"GET\",\"path\":\"/chOrderInfo/getOrderInfoDetail/ORDER-1\",\"query\":\"\"},"
                + "\"contract\":\"" + CpmsReadTool.CONTRACT + "\",\"base_url\":\"" + base + "\","
                + "\"bearer\":\"PRIVATE-TOKEN\",\"expires_at\":\"" + Instant.now().plusSeconds(60) + "\"}";
    }

    /**
     * 新节点 + 旧 Gateway：拒绝必须发生在领授权，而且错误码要稳定。
     *
     * <p>这一向只能在这里证：Python 侧的替身说不了 Java 的话。两个方向都落在显式拒绝上，
     * 混跑才不会以「完成回调里证据缺字段」的形状炸开。
     */
    static void mixedVersionIsRefusedInBothDirections() throws Exception {
        var stale = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        var seen = new AtomicInteger();
        var declared = new java.util.ArrayList<String>();
        stale.createContext("/", exchange -> {
            seen.incrementAndGet();
            declared.add(exchange.getRequestHeaders().getFirst(GatewayClient.TOOL_CONTRACT_HEADER));
            String base = "http://127.0.0.1:" + stale.getAddress().getPort();
            String body = staleMode ? staleAuthorization(base) : currentAuthorization(base);
            byte[] bytes = body.getBytes(java.nio.charset.StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        stale.start();
        try {
            String base = "http://127.0.0.1:" + stale.getAddress().getPort();
            var client = new GatewayClient(base, "NODE-TOKEN", "build", 5);
            var authorization = new CpmsGatewayAuthorization(client);
            staleMode = true;
            refuse(() -> {
                try {
                    authorization.require(RUN, MANIFEST, "cpms_order_detail", null);
                } catch (RuntimeException failure) {
                    throw failure;
                }
            }, "old gateway is refused");
            check(seen.get() == 1, "stale gateway still received the request");
            check(CpmsReadTool.CONTRACT.equals(declared.get(0)), "node declares its contract");
            staleMode = false;
            var grant = authorization.require(RUN, MANIFEST, "cpms_order_detail", null);
            check(grant.orderNo().equals("ORDER-1"), "inputs carry the order number");
            check(grant.request().target().equals("/chOrderInfo/getOrderInfoDetail/ORDER-1"),
                    "request target comes from the gateway");
        } finally {
            stale.stop(0);
        }
    }

    static volatile boolean staleMode;
    /** 最近一次到达「网关」的模型参数，用来断言节点没有把它丢掉。 */
    static JsonNode lastArguments;

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
                        || path.equals("/iotChargingDevice/getChargingMonitor/ORDER-1?current=1&size=50")
                        || path.equals("/chSite/page?current=1&size=50&type=0"),
                        "the granted request target, not a path composed here");
                String row = "{\"orderNo\":\"ORDER-1\",\"tenantId\":\"T1\",\"siteId\":\"S1\","
                        + "\"status\":2,\"createdTime\":\"2026-10-25 08:00:00\",\"phone\":\"PRIVATE-PHONE\"}";
                String body = "{\"code\":0,\"data\":{\"orderNo\":\"ORDER-1\",\"siteId\":\"S1\",\"chOrderInfoModel\":" + row + "}}";
                if (mode.equals("samples")) body = "{\"code\":0,\"data\":{\"records\":[" + row + "],\"total\":80,\"current\":1,\"size\":50}}";
                // 站点页：行身份是**站点**（id / name / address），没有 orderNo。
                if (mode.startsWith("site")) {
                    String site = "{\"id\":\"S1\",\"tenantId\":\"T1\",\"name\":\"Test Site\",\"address\":\"addr\"}";
                    if (mode.equals("site-wrong-tenant")) site = site.replace("\"T1\"", "\"T2\"");
                    if (mode.equals("site-wrong-site")) site = site.replace("\"S1\"", "\"S9\"");
                    body = "{\"code\":0,\"data\":{\"records\":[" + site + "],\"total\":1,\"current\":1,\"size\":50}}";
                }
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
            String base = "http://127.0.0.1:" + server.getAddress().getPort();
            grant = detailGrant(base);
            var detail = new CpmsReadTool("cpms_order_detail", AUTH);
            var samples = new CpmsReadTool("cpms_charging_samples", AUTH);
            JsonNode result = JSON.readTree(detail.call("{}", CONTEXT));
            check(result.path("data").path("status").asInt() == 2 && !result.toString().contains("PRIVATE"), "detail result");
            check(recorded == 1 && calls.get() == 1, "one request and receipt");
            mode = "samples";
            result = JSON.readTree(samples.call("{}", CONTEXT));
            check(result.path("data").path("truncated").asBoolean()
                    && !result.path("data").path("complete_timeline").asBoolean(), "honest samples");
            // 参数现在**可以**非空（模型提议，网关放行），所以判据从「必须为空」换成
            // 「形状不成立就在发请求之前拒」。语义（能不能用）由网关判，节点不知道原话。
            for (String input : new String[]{"[]", "null", "", "{\"name\":123}", "{\"name\":\"" + "x".repeat(81) + "\"}"}) {
                int before = calls.get();
                refuse(() -> detail.call(input, CONTEXT), "invalid argument shape");
                check(calls.get() == before, "malformed arguments refused before HTTP");
            }
            // 形状成立的参数会被原样转给网关 —— 节点不替网关做语义判断。
            lastArguments = null;
            mode = "detail";
            detail.call("{\"name\":\"天河\"}", CONTEXT);
            check(lastArguments != null && "天河".equals(lastArguments.path("name").asText()),
                    "model arguments forwarded to the gateway");
            refuse(() -> detail.call("{}"), "no context");
            for (String test : new String[]{"wrong-order", "wrong-tenant", "wrong-site", "missing-identity",
                    "envelope-error", "oversized", "invalid-json", "http-error", "redirect", "revoked"}) {
                mode = test; revoked = false;
                int before = recorded, beforeCalls = calls.get();
                refuse(() -> detail.call("{}", CONTEXT), test);
                check(recorded == before && calls.get() == beforeCalls + 1, "no retries or rejected evidence: " + test);
            }
            revoked = false;
            grant = detailGrant(grant.baseUrl(), Instant.now().minusSeconds(1));
            int before = calls.get();
            refuse(() -> detail.call("{}", CONTEXT), "expired");
            check(calls.get() == before, "expired before HTTP");
            // 授权给的路径变了，节点就发那条路径 —— 它已经不再按工具名拼路径。
            mode = "detail";
            revoked = false;
            grant = new CpmsReadTool.Grant("grant-1", "T1", Set.of("S1"),
                    JSON.readTree("{\"order_no\":\"ORDER-1\"}"),
                    new CpmsReadTool.Request("GET", "/chOrderInfo/getOrderInfoDetail/ORDER-1", ""),
                    grant.baseUrl(), "PRIVATE-TOKEN", Instant.now().plusSeconds(60));
            check(grant.request().target().equals("/chOrderInfo/getOrderInfoDetail/ORDER-1"), "target with no query");
            var samplesTarget = new CpmsReadTool.Request("GET",
                    "/iotChargingDevice/getChargingMonitor/ORDER-1", "current=1&size=50");
            check(samplesTarget.target().equals(
                    "/iotChargingDevice/getChargingMonitor/ORDER-1?current=1&size=50"), "target with query");
            // 请求目标形状不成立就拒绝：方法、绝对路径、长度、空白。
            for (String[] bad : new String[][]{
                    {"POST", "/x", ""}, {"GET", "x", ""}, {"GET", "/x\n", ""},
                    {"GET", "/x", "a\nb"}, {"GET", "/../etc/passwd", ""}}) {
                refuse(() -> new CpmsReadTool.Request(bad[0], bad[1], bad[2]), "request target: " + bad[1]);
            }
            siteSearchHasNoOrderIdentity();
            mixedVersionIsRefusedInBothDirections();
            toolSurfaceFollowsTheRunType();
            System.out.println("CPMS_TOOL_ACCEPTANCE PASS assertions=" + passed + " http_calls=" + calls.get());
        } finally { server.stop(0); }
    }

    /**
     * 站点工具是一次**无订单**回合：授权里没有 `order_no`，行身份是站点不是订单。
     *
     * <p>2026-11-02 生产事故：`identity()` 对每一行都要求 `orderNo`，站点行没有这个字段，
     * 于是 `cpms_scope_mismatch` 被 catch-all 转成 `cpms_unavailable`——看起来像网络故障，
     * 实际是判据错。这条钉住「无订单授权 + 站点行必须通过」。
     */
    static void siteSearchHasNoOrderIdentity() throws Exception {
        mode = "site";
        // 先取出 baseUrl 再覆盖 grant：右值在赋值前求值，否则会拿到上一条已过期的授权。
        String base = grant.baseUrl();
        grant = new CpmsReadTool.Grant("grant-s", "T1", Set.of("S1"), JSON.readTree("{}"),
                new CpmsReadTool.Request("GET", "/chSite/page", "current=1&size=50&type=0"),
                base, "PRIVATE-TOKEN", Instant.now().plusSeconds(60));
        var site = new CpmsReadTool("cpms_site_search", AUTH);
        JsonNode result = JSON.readTree(site.call("{}", CONTEXT));
        check(result.path("data").path("records").size() == 1
                && "S1".equals(result.path("data").path("records").get(0).path("id").asText()),
                "site row accepted without an order number");
        for (String test : new String[]{"site-wrong-tenant", "site-wrong-site"}) {
            mode = test;
            int before = recorded;
            refuse(() -> site.call("{}", CONTEXT), test);
            check(recorded == before, "no evidence for rejected site page: " + test);
        }
        mode = "detail";
    }

    static CpmsReadTool.Grant detailGrant(String base) throws Exception {
        return detailGrant(base, Instant.now().plusSeconds(60));
    }

    static CpmsReadTool.Grant detailGrant(String base, Instant expiresAt) throws Exception {
        return new CpmsReadTool.Grant("grant-1", "T1", Set.of("S1"),
                JSON.readTree("{\"order_no\":\"ORDER-1\"}"),
                new CpmsReadTool.Request("GET", "/chOrderInfo/getOrderInfoDetail/ORDER-1", ""),
                base, "PRIVATE-TOKEN", expiresAt);
    }

    /** 运行类型决定工具面：判据是清单的 schema，不是工具名之间的特例。 */
    static void toolSurfaceFollowsTheRunType() throws Exception {
        var consumer = com.trendpower.aiops.node.exec.ToolSurface.allowed("m3-exec-config/1");
        // AC1 的装配面：C 端同清单里既有订单状态、又有站点查询。
        check(consumer.contains(ControlledChargingStatusTool.TOOL_NAME), "consumer keeps the status tool");
        check(consumer.contains("cpms_site_search"), "consumer gains the site tool");
        check(!consumer.contains("cpms_order_detail"), "consumer has no order-diagnosis tool");
        // AC2：状态工具与订单诊断工具仍然不能混用 —— 但那是允许集合的结果。
        var order = com.trendpower.aiops.node.exec.ToolSurface.allowed("operator-exec-config/1");
        check(order.contains("cpms_order_detail") && order.contains("cpms_charging_samples"),
                "order run keeps both CPMS tools");
        check(!order.contains(ControlledChargingStatusTool.TOOL_NAME), "order run refuses the status tool");
        check(!order.contains("cpms_site_search"), "order run refuses the conversation-only tool");
        var conversation = com.trendpower.aiops.node.exec.ToolSurface.allowed("operator-conversation/1");
        check(conversation.equals(List.of("cpms_site_search")), "conversation run carries only the site tool");
        // AC3：未声明的工具名在**任何**运行类型下都不成立，拒绝码稳定。
        for (String schema : new String[]{"m3-exec-config/1", "operator-exec-config/1", "operator-conversation/1"}) {
            check(!com.trendpower.aiops.node.exec.ToolSurface.allows(schema, "shell"), "undeclared tool refused: " + schema);
        }
        refuseExec(() -> com.trendpower.aiops.node.exec.ToolSurface.allowed("m3-exec-config/9"),
                "unknown run type");

        // 清单解析与装配用同一条规则：对话回合带站点工具合法，带订单工具不合法。
        String base = "{\"schema\":\"operator-conversation/1\",\"name\":\"n\",\"history_policy\":\"none\","
                + "\"model\":{\"provider_key\":\"p\",\"adapter_key\":\"a\",\"model_key\":\"m\","
                + "\"endpoint\":\"https://e\"},\"model_options\":{},\"system_messages\":[\"S\"],\"tools\":[%s]}";
        String site = "{\"name\":\"cpms_site_search\",\"description\":\"d\",\"input_schema\":\"{\\\"type\\\":\\\"object\\\"}\"}";
        String orderTool = "{\"name\":\"cpms_order_detail\",\"description\":\"d\",\"input_schema\":\"{\\\"type\\\":\\\"object\\\"}\"}";
        String withSite = String.format(base, site);
        var manifest = com.trendpower.aiops.node.exec.ExecManifest.fromText(withSite, sha256(withSite));
        check(manifest.schema().equals("operator-conversation/1") && manifest.tools().size() == 1,
                "conversation manifest accepts the site tool");
        String withOrderTool = String.format(base, orderTool);
        String orderToolDigest = sha256(withOrderTool);
        refuseExec(() -> com.trendpower.aiops.node.exec.ExecManifest.fromText(withOrderTool, orderToolDigest),
                "conversation manifest refuses an order tool");
    }

    /** 清单身份就是原始字节的 sha256 —— `fromText` 的第二个参数是它，不是任意 id。 */
    static String sha256(String text) throws Exception {
        var digest = java.security.MessageDigest.getInstance("SHA-256");
        byte[] bytes = digest.digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        var hex = new StringBuilder();
        for (byte value : bytes) hex.append(String.format("%02x", value));
        return hex.toString();
    }

    static void refuseExec(Runnable action, String label) {
        try {
            action.run();
            throw new AssertionError("accepted: " + label);
        } catch (com.trendpower.aiops.node.exec.ExecConfigViolation expected) {
            check(!expected.getMessage().isBlank(), label);
        }
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
