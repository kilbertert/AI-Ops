package m3link;

import com.aizuda.snail.ai.agent.core.executor.client.ChatClientBuildContext;
import com.aizuda.snail.ai.agent.core.executor.client.ChatClientCustomizer;
import com.aizuda.snail.ai.agent.core.runtime.ChatSessionRequest;
import com.aizuda.snail.ai.agent.core.runtime.ChatSessionRuntime;
import com.aizuda.snail.ai.common.dto.agent.ChatDispatchRequest;
import com.aizuda.snail.ai.common.model.ConfigExtAttrsDTO;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpServer;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.ComponentScan;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/**
 * M3-NODE-LINK-R1：**同 JVM** 并发判据，自带替身 Gateway。
 *
 * <p>为什么独立于 `verify_node_link.py`：那一层证明的是「真 Gateway + 真节点」的端到端，
 * 每次派发只起一个节点进程，结构上覆盖不到「同一个 JVM 里两个 run」。把并发判据塞进那套
 * harness 会要求它伪造 run 记录或强加第二个编排路径 —— 两件事都不该发生在端到端验收里。
 * 这里用一个**只回答领取/完成**的替身 Gateway：它故意对任何 run 都回 200（包括同一 run
 * 第二次领取），这正是「靠能不能领到来判断归属」会失效的最坏情形。
 *
 * <p>四个场景：
 * <ul>
 *   <li>{@code distinct} —— 两个不同 run 并发：两者都完成，业务各领一次；</li>
 *   <li>{@code early} —— A 已进入执行、尚未构建 ChatClient 时，相同 run 的 B 进入；</li>
 *   <li>{@code late} —— A 已领取、末轮请求还挂在上游时，相同 run 的 B 进入；</li>
 *   <li>{@code first-error} —— 首轮上游失败：不产生任何领取，且只发一次 failed 终局。</li>
 * </ul>
 * 两个 same-run 场景要求 B 在**本地**被拒（`DuplicateRunRefused`）且不产生任何
 * model/claim/completion/failure 增量；A 释放后按自己的守卫正常成功。
 *
 * <p>协调全部用可观察事件：customizer 到达、末轮请求到达上游、B 的拒绝回调到达。
 * **没有 sleep 用来宣称并发成立。**
 */
// 独立的包子目录：不与 `m3node` 里的其它 Spring 引导类相互扫描。
@SpringBootApplication
@ComponentScan({"com.trendpower.aiops.node"})
public class ConcurrentRunAcceptance {

    static final ObjectMapper JSON = new ObjectMapper();
    static final List<String> RESULTS = new ArrayList<>();
    static int failures;

    static volatile String mode = "distinct";
    static final CountDownLatch aAtBuild = new CountDownLatch(1);
    static final CountDownLatch bRefused = new CountDownLatch(1);
    static final CountDownLatch finalRoundArrived = new CountDownLatch(1);
    static final CountDownLatch releaseFinalRound = new CountDownLatch(1);

    static final List<String> notices = new CopyOnWriteArrayList<>();
    static final List<String> claims = new CopyOnWriteArrayList<>();
    static final AtomicInteger modelCalls = new AtomicInteger();

    /** 在 customizer 里给 A 制造「已进入、未构建」的暂停点。 */
    @org.springframework.context.annotation.Configuration
    static class InterleaveConfig {
        @org.springframework.context.annotation.Bean
        ChatClientCustomizer acceptanceInterleave() {
            return new ChatClientCustomizer() {
                @Override
                public int order() {
                    return -1;
                }

                @Override
                public void customize(ChatClientBuildContext context) {
                    if (!"early".equals(mode)) {
                        return;
                    }
                    if ("acceptance-A".equals(Thread.currentThread().getName())) {
                        aAtBuild.countDown();
                        awaitOrFail(bRefused, "B refused");
                    }
                }
            };
        }
    }

    public static void main(String[] args) throws Exception {
        mode = args.length > 0 ? args[0] : "distinct";
        ModelHandler.state.put("mode", "distinct".equals(mode) ? "normal"
                : "first-error".equals(mode) ? "round1-500" : "normal");
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(Executors.newCachedThreadPool());
        server.createContext("/", ConcurrentRunAcceptance::handle);
        server.start();
        String base = "http://127.0.0.1:" + server.getAddress().getPort();

        Path manifest = Files.createTempFile("concurrent-manifest", ".json");
        Files.writeString(manifest, manifestJson(base + "/v1"));
        System.setProperty("AIOPS_EXEC_MANIFEST", manifest.toString());

        Map<String, Object> props = new HashMap<>();
        props.put("server.port", "0");
        props.put("snail-ai.enabled", "true");
        props.put("snail-ai.server.host", "127.0.0.1");
        int grpcBase = 23000 + (int) (ProcessHandle.current().pid() % 20000);
        props.put("snail-ai.server.port", String.valueOf(grpcBase));
        props.put("snail-ai.port", String.valueOf(grpcBase + 1));
        props.put("snail-ai.host", "127.0.0.1");
        props.put("snail-ai.app-id", "concurrent-acceptance");
        props.put("snail-ai.token", "synthetic");
        props.put("snail-ai.skill-temp-dir", "/tmp/m3concurrent/skills");
        props.put("snail-ai.shell-temp-dir", "/tmp/m3concurrent/script");
        props.put("aiops.node.name", "node-local");
        props.put("aiops.node.build", "m3-node-link-r1");
        props.put("aiops.gateway.base-url", base);
        props.put("aiops.gateway.node-token", "synthetic-node-token");
        props.put("AIOPS_EXEC_MANIFEST", manifest.toString());

        var app = new SpringApplication(ConcurrentRunAcceptance.class, InterleaveConfig.class);
        app.setDefaultProperties(props);
        try (ConfigurableApplicationContext ctx = app.run()) {
            ChatSessionRuntime runtime = ctx.getBean(ChatSessionRuntime.class);
            String runA = "run_concurrentA00000000000000";
            String runB = switch (mode) {
                case "distinct" -> "run_concurrentB00000000000000";
                default -> runA;
            };
            switch (mode) {
                case "distinct" -> distinct(runtime, base + "/v1", runA, runB);
                case "early", "late" -> sameRun(runtime, base + "/v1", runA);
                case "first-error" -> firstError(runtime, base + "/v1", runA);
                default -> {
                    System.out.println("STATUS=FAIL unknown mode " + mode);
                    System.exit(2);
                }
            }
        } finally {
            server.stop(0);
            Files.deleteIfExists(manifest);
        }
        RESULTS.forEach(System.out::println);
        System.out.println("OBSERVED model=" + modelCalls.get()
                + " claims=" + claims.size() + " notices=" + notices);
        System.out.println("failures=" + failures);
        System.out.println("STATUS=" + (failures == 0 ? "PASS" : "FAIL"));
        System.exit(failures == 0 ? 0 : 1);
    }

    // ------------------------------------------------------------------ 场景

    private static void distinct(ChatSessionRuntime runtime, String model, String runA, String runB)
            throws Exception {
        int noticesBefore = notices.size();
        AtomicReference<Result> a = new AtomicReference<>();
        AtomicReference<Result> b = new AtomicReference<>();
        Thread ta = new Thread(() -> a.set(run(runtime, dispatch(model, runA, "a"))), "acceptance-A");
        Thread tb = new Thread(() -> b.set(run(runtime, dispatch(model, runB, "b"))), "acceptance-B");
        ta.start();
        tb.start();
        ta.join(120_000);
        tb.join(120_000);
        check("distinct:A-completed", a.get() != null && a.get().completion != null, describe(a.get()));
        check("distinct:B-completed", b.get() != null && b.get().completion != null, describe(b.get()));
        check("distinct:business-read-once-per-run",
                claims.size() == 2 && claims.contains(runA) && claims.contains(runB),
                String.valueOf(claims));
        check("distinct:each-run-notified-exactly-once",
                notices.size() == noticesBefore + 2
                        && notices.contains(runA + ":completed")
                        && notices.contains(runB + ":completed"),
                String.valueOf(notices));
    }

    /**
     * 相同 run 重入。两个窗口：
     * <ul>
     *   <li>{@code early}：B 在 A 构建 ChatClient 之前进入；</li>
     *   <li>{@code late}：A 已领取、末轮请求还挂在上游时 B 进入（上游事件驱动）。</li>
     * </ul>
     */
    private static void sameRun(ChatSessionRuntime runtime, String model, String runA)
            throws Exception {
        AtomicReference<Result> a = new AtomicReference<>();
        AtomicReference<Result> b = new AtomicReference<>();
        Thread ta = new Thread(() -> a.set(run(runtime, dispatch(model, runA, "a"))), "acceptance-A");
        ta.start();
        if ("early".equals(mode)) {
            awaitOrFail(aAtBuild, "A reached the customizer");
        } else {
            awaitOrFail(finalRoundArrived, "A's final round reached the upstream");
        }
        Thread tb = new Thread(() -> {
            Result result = run(runtime, dispatch(model, runA, "b"));
            b.set(result);
            bRefused.countDown();
        }, "acceptance-B");
        tb.start();
        // B 在 local 被拒是同步发生的（execute 里直接走 errorConsumer），所以 join 之后
        // 它的拒绝一定已经被观察到；late 窗口随后才放行 A 的末轮。
        tb.join(60_000);
        if ("late".equals(mode)) {
            releaseFinalRound.countDown();
        }
        ta.join(120_000);

        Result bResult = b.get();
        check(mode + ":B-refused-locally",
                bResult != null && bResult.error != null
                        && "DuplicateRunRefused".equals(bResult.error.getClass().getSimpleName()),
                describe(bResult));
        check(mode + ":A-completed", a.get() != null && a.get().completion != null, describe(a.get()));
        // 绝对总量，不拿「B 进入那一刻」的快照当基线（late 窗口里它已经包含 A 的领取）。
        check(mode + ":only-one-run-executed", modelCalls.get() == 2,
                "model=" + modelCalls.get());
        check(mode + ":one-business-read", claims.equals(List.of(runA)), String.valueOf(claims));
        check(mode + ":one-completion-notice",
                notices.equals(List.of(runA + ":completed")), String.valueOf(notices));
        check(mode + ":B-has-no-completion-and-local-error",
                bResult != null && bResult.completion == null && bResult.error != null,
                describe(bResult));
    }

    /** 首轮上游失败：没有领取，且**只**发一次 failed 终局。 */
    private static void firstError(ChatSessionRuntime runtime, String model, String runA)
            throws Exception {
        Result result = run(runtime, dispatch(model, runA, "a"));
        check("first-error:fails", result.error != null, describe(result));
        check("first-error:no-business-read", claims.isEmpty(), String.valueOf(claims));
        check("first-error:one-failed-notice",
                notices.size() == 1 && notices.get(0).endsWith(":failed"), String.valueOf(notices));
    }

    // ------------------------------------------------------------------ 替身 Gateway + 模型上游

    static final class ModelHandler {
        static final Map<String, Object> state = new HashMap<>();
    }

    private static void handle(com.sun.net.httpserver.HttpExchange exchange) throws IOException {
        String path = exchange.getRequestURI().getPath();
        String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        if (path.contains("/claims/")) {
            // 故意对任何 run 都回 200（包括同一 run 的第二次领取）：
            // 这正是「能领到 ≠ 拥有这条 run」的最坏情形，本地拒重入必须不依赖它。
            claims.add(path.substring(path.lastIndexOf('/') + 1));
            send(exchange, 200, "application/json",
                    "{\"order_no\":\"ORDER-A\",\"status_code\":\"1\",\"status_role\":\"finished\"}");
            return;
        }
        if (path.contains("/completions/")) {
            String runId = path.substring(path.lastIndexOf('/') + 1);
            String status = JSON.readTree(body).path("status").asText();
            notices.add(runId + ":" + status);
            send(exchange, 200, "application/json",
                    "{\"run_id\":\"" + runId + "\",\"state\":\""
                            + ("completed".equals(status) ? "succeeded" : "failed") + "\"}");
            return;
        }
        int call = modelCalls.incrementAndGet();
        boolean firstRound = true;
        for (var message : JSON.readTree(body).path("messages")) {
            if ("tool".equals(message.path("role").asText())) {
                firstRound = false;
            }
        }
        if (firstRound && "round1-500".equals(ModelHandler.state.get("mode"))) {
            exchange.sendResponseHeaders(500, 0);
            exchange.getResponseBody().close();
            return;
        }
        if (!firstRound && "late".equals(mode)) {
            // 「末轮已经到达上游」是真实可观察事件：B 的进入时机由它决定，而不是 sleep。
            finalRoundArrived.countDown();
            try {
                releaseFinalRound.await(60, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
        ObjectNode delta = JSON.createObjectNode().put("role", "assistant");
        if (firstRound) {
            var function = JSON.createObjectNode()
                    .put("name", "charging_status_snapshot").put("arguments", "{}");
            var toolCall = JSON.createObjectNode().put("index", 0)
                    .put("id", "call_1").put("type", "function");
            toolCall.set("function", function);
            delta.set("tool_calls", JSON.createArrayNode().add(toolCall));
        } else {
            delta.put("content", "complete");
        }
        var choice = JSON.createObjectNode().put("index", 0)
                .put("finish_reason", firstRound ? "tool_calls" : "stop");
        choice.set("delta", delta);
        ObjectNode chunk = JSON.createObjectNode().put("id", "chat-probe")
                .put("object", "chat.completion.chunk").put("created", 0)
                .put("model", "synthetic-model");
        chunk.set("choices", JSON.createArrayNode().add(choice));
        send(exchange, 200, "text/event-stream",
                "data: " + JSON.writeValueAsString(chunk) + "\n\ndata: [DONE]\n\n");
    }

    private static void send(com.sun.net.httpserver.HttpExchange exchange, int status,
                             String contentType, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().add("Content-Type", contentType);
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(bytes);
        }
    }

    // ------------------------------------------------------------------ 驱动

    record Result(String completion, Throwable error) {
    }

    private static Result run(ChatSessionRuntime runtime, ChatDispatchRequest dispatch) {
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<String> completion = new AtomicReference<>();
        AtomicReference<Throwable> error = new AtomicReference<>();
        try {
            runtime.execute(ChatSessionRequest.builder()
                    .dispatchRequest(dispatch)
                    .textConsumer(chunk -> { })
                    .thinkingConsumer(chunk -> { })
                    .completionConsumer(result -> {
                        completion.set(result.fullText());
                        done.countDown();
                    })
                    .errorConsumer(failure -> {
                        error.set(failure);
                        done.countDown();
                    })
                    .build());
            if (!done.await(120, TimeUnit.SECONDS)) {
                return new Result(null, new IllegalStateException("timeout"));
            }
            return new Result(completion.get(), error.get());
        } catch (RuntimeException e) {
            return new Result(null, e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return new Result(null, e);
        }
    }

    private static ChatDispatchRequest dispatch(String model, String runId, String tag) {
        ChatDispatchRequest d = new ChatDispatchRequest();
        d.setRequestId("concurrent-" + tag);
        d.setSid(runId);
        d.setConversationId("conv-" + tag);
        d.setSystemPrompt("SYSTEM-PROMPT");
        d.setUserMessage("请给我这个订单的充电状态");
        ChatDispatchRequest.ModelConfig mc = new ChatDispatchRequest.ModelConfig();
        mc.setModelConfigId(1L);
        mc.setProviderKey("openai-compatible");
        mc.setAdapterKey("openai-compatible");
        mc.setModelKey("synthetic-model");
        mc.setApiEndpoint(model);
        mc.setApiKey("synthetic-not-a-real-key");
        ConfigExtAttrsDTO ext = new ConfigExtAttrsDTO();
        ext.setTemperature(0.2d);
        ext.setTopP(0.9d);
        ext.setMaxTokens(64);
        mc.setConfigJson(ext);
        d.setModelConfig(mc);
        return d;
    }

    /** 清单与节点真实声明的工具三元组逐字一致（在这里生成，不手抄常量）。 */
    static String manifestJson(String modelBaseUrl) {
        String description = com.trendpower.aiops.node.ControlledChargingStatusTool.DESCRIPTION
                .replace("\\", "\\\\").replace("\"", "\\\"");
        String schema = com.trendpower.aiops.node.ControlledChargingStatusTool.SCHEMA
                .replace("\\", "\\\\").replace("\"", "\\\"");
        return """
                {"schema":"m3-exec-config/1","name":"m3-node-link-r1","history_policy":"none",
                 "model":{"provider_key":"openai-compatible","adapter_key":"openai-compatible",
                          "model_key":"synthetic-model","endpoint":"%s"},
                 "model_options":{"temperature":0.2,"top_p":0.9,"max_tokens":64},
                 "system_messages":["SYSTEM-PROMPT"],
                 "tools":[{"name":"%s","description":"%s","input_schema":"%s"}]}
                """.formatted(modelBaseUrl,
                com.trendpower.aiops.node.ControlledChargingStatusTool.TOOL_NAME,
                description, schema);
    }

    private static void awaitOrFail(CountDownLatch latch, String label) {
        try {
            if (!latch.await(60, TimeUnit.SECONDS)) {
                throw new IllegalStateException(label + " (timed out)");
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(label, e);
        }
    }

    private static void check(String name, boolean ok, String detail) {
        RESULTS.add((ok ? "PASS " : "FAIL ") + name + "  " + detail);
        if (!ok) {
            failures++;
        }
    }

    private static String describe(Result result) {
        if (result == null) {
            return "null";
        }
        return "completion=" + result.completion
                + " error=" + (result.error == null ? "none"
                : result.error.getClass().getSimpleName() + ": " + result.error.getMessage());
    }
}
