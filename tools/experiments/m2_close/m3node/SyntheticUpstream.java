package m3node;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 本地合成上游：一个 OpenAI-compatible 聊天端点，加一个业务领取替身。
 *
 * <p>它只监听 loopback，不发起任何出站连接，也不需要凭据。它**记录真实收到的字节**
 * （路径、model、tools、messages、选项、Authorization 是否存在），因为本片要证的正是
 * 「节点到底把什么发给了上游」—— 而不是「我们自己以为发了什么」。
 *
 * <p>流式与非流式两种回应都实现：节点真实使用的路径是 stream，非流式用于在同一套装配上
 * 做更窄的对照。第一次请求返回一个真实的 tool call，第二次返回 final，
 * 这样真实 {@code ToolCallingAdvisor} + {@code DefaultToolCallingManager} 会跑出一轮工具循环。
 */
final class SyntheticUpstream implements AutoCloseable {

    static final String CHAT_PATH = "/v1/chat/completions";

    /** 每次聊天请求收到的完整请求体，按到达顺序。 */
    final List<JsonNode> chatRequests = new CopyOnWriteArrayList<>();
    /** 上游实际被请求的路径（用于证明适配器打的确实是 {@link #CHAT_PATH}）。 */
    final List<String> chatPaths = new CopyOnWriteArrayList<>();
    /** 是否带了 Authorization 头（不记录其内容）。 */
    final List<Boolean> chatAuthorized = new CopyOnWriteArrayList<>();
    final AtomicInteger chatCalls = new AtomicInteger();
    final AtomicInteger streamRequests = new AtomicInteger();
    /** 每次记录到一次 chat 请求后调用（验收用来在确定的时点放行另一个 run）。 */
    volatile java.util.function.IntConsumer chatObserver = n -> { };
    final AtomicInteger claimCalls = new AtomicInteger();
    final List<String> claimPaths = new CopyOnWriteArrayList<>();
    /** 整轮终局通知：与业务领取分开计数（两件事，两个数）。 */
    final AtomicInteger completionCalls = new AtomicInteger();
    final List<String> completionPaths = new CopyOnWriteArrayList<>();

    private final HttpServer chatServer;
    private final HttpServer claimServer;
    private final ExecutorService pool = Executors.newCachedThreadPool();
    private final ObjectMapper mapper = new ObjectMapper();
    private volatile int chatPort;
    private volatile int claimPort;

    SyntheticUpstream(String toolName, boolean installed) throws IOException {
        chatServer = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        chatServer.setExecutor(pool);
        chatServer.createContext("/", exchange -> handleChat(exchange, toolName));
        chatServer.start();
        chatPort = chatServer.getAddress().getPort();

        claimServer = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        claimServer.setExecutor(pool);
        claimServer.createContext("/", this::handleClaim);
        claimServer.start();
        claimPort = claimServer.getAddress().getPort();
    }

    /** 验收断言一次性读取；场景之间必须重置，否则上一个场景的调用会串进下一个场景的计数。 */
    void resetCounters() {
        chatRequests.clear();
        chatPaths.clear();
        chatAuthorized.clear();
        chatCalls.set(0);
        claimCalls.set(0);
        claimPaths.clear();
        completionCalls.set(0);
        completionPaths.clear();
        streamRequests.set(0);
    }

    String baseUrl() {
        return "http://127.0.0.1:" + chatPort + "/v1";
    }

    String claimBaseUrl() {
        return "http://127.0.0.1:" + claimPort;
    }

    @Override
    public void close() {
        chatServer.stop(0);
        claimServer.stop(0);
        pool.shutdownNow();
    }

    // ------------------------------------------------------------------ 断言用只读视图

    String lastModel() {
        return chatRequests.isEmpty() ? "<none>" : chatRequests.get(chatRequests.size() - 1).path("model").asText("<absent>");
    }

    int lastToolCount() {
        return chatRequests.isEmpty() ? -1 : chatRequests.get(chatRequests.size() - 1).path("tools").size();
    }

    String lastTemperature() {
        return chatRequests.isEmpty() ? "<none>"
                : chatRequests.get(chatRequests.size() - 1).path("temperature").asText("<absent>");
    }

    int lastMessageCount() {
        return chatRequests.isEmpty() ? -1 : chatRequests.get(chatRequests.size() - 1).path("messages").size();
    }

    /** 上游收到的最新的工具定义描述（用于证明同名 description 漂移确实到达了上游）。 */
    String firstToolDescription() {
        if (chatRequests.isEmpty()) {
            return "<none>";
        }
        var tools = chatRequests.get(chatRequests.size() - 1).path("tools");
        return tools.isEmpty() ? "<no-tools>" : tools.get(0).path("function").path("description").asText();
    }

    /** 上游收到的最新的工具输入 schema（用于证明同名 schema 漂移确实到达了上游）。 */
    String firstToolSchema() {
        if (chatRequests.isEmpty()) {
            return "<none>";
        }
        var tools = chatRequests.get(chatRequests.size() - 1).path("tools");
        return tools.isEmpty() ? "<no-tools>"
                : tools.get(0).path("function").path("parameters").toString();
    }

    /** 上游收到的最新一条 tool 消息内容（诊断工具调用是否真的被执行）。 */
    String lastToolMessage() {
        for (int i = chatRequests.size() - 1; i >= 0; i--) {
            for (JsonNode m : chatRequests.get(i).path("messages")) {
                if ("tool".equals(m.path("role").asText())) {
                    return m.path("content").asText();
                }
            }
        }
        return "<no-tool-message>";
    }

    /** 上游收到的第一条 system 消息文本（比对"漂移确实到达了上游"）。 */
    String firstSystemMessage() {
        if (chatRequests.isEmpty()) {
            return "<none>";
        }
        for (JsonNode m : chatRequests.get(0).path("messages")) {
            if ("system".equals(m.path("role").asText())) {
                return m.path("content").asText();
            }
        }
        return "<no-system>";
    }

    // ------------------------------------------------------------------ chat

    private void handleChat(HttpExchange exchange, String toolName) throws IOException {
        byte[] raw = exchange.getRequestBody().readAllBytes();
        String path = exchange.getRequestURI().getPath();
        boolean authorized = exchange.getRequestHeaders().getFirst("Authorization") != null;
        int n = chatCalls.incrementAndGet();
        chatPaths.add(path);
        chatAuthorized.add(authorized);
        JsonNode body = mapper.readTree(raw);
        chatRequests.add(body);
        chatObserver.accept(n);
        boolean streaming = body.path("stream").asBoolean(false);
        if (streaming) {
            streamRequests.incrementAndGet();
        }
        if (streaming) {
            sendStreaming(exchange, toolName, n);
        } else {
            sendJson(exchange, toolName, n);
        }
    }

    private void sendJson(HttpExchange exchange, String toolName, int call) throws IOException {
        ObjectNode root = mapper.createObjectNode();
        root.put("id", "chatcmpl-" + call);
        root.put("object", "chat.completion");
        root.put("created", 0);
        root.put("model", "synthetic");
        ObjectNode choice = mapper.createObjectNode();
        choice.put("index", 0);
        choice.set("message", messageFor(toolName, call));
        choice.put("finish_reason", call == 1 ? "tool_calls" : "stop");
        root.set("choices", mapper.createArrayNode().add(choice));
        root.set("usage", usage());
        send(exchange, 200, "application/json", mapper.writeValueAsBytes(root));
    }

    /** 第 1 次：真实的 tool call；之后：final 文本。 */
    private ObjectNode messageFor(String toolName, int call) {
        ObjectNode message = mapper.createObjectNode();
        if (call == 1) {
            message.putNull("content");
            ObjectNode call_ = mapper.createObjectNode();
            call_.put("id", "call_1");
            call_.put("type", "function");
            ObjectNode function = mapper.createObjectNode();
            function.put("name", toolName);
            function.put("arguments", "{}");
            call_.set("function", function);
            message.set("tool_calls", mapper.createArrayNode().add(call_));
        } else {
            message.put("content", "final-answer");
        }
        message.put("role", "assistant");
        return message;
    }

    private void sendStreaming(HttpExchange exchange, String toolName, int call) throws IOException {
        exchange.getResponseHeaders().add("Content-Type", "text/event-stream");
        exchange.sendResponseHeaders(200, 0);
        StringBuilder sse = new StringBuilder();
        ObjectNode first = chunkSkeleton(call);
        ObjectNode delta = mapper.createObjectNode();
        delta.put("role", "assistant");
        if (call == 1) {
            ObjectNode call_ = mapper.createObjectNode();
            call_.put("index", 0);
            call_.put("id", "call_1");
            call_.put("type", "function");
            ObjectNode function = mapper.createObjectNode();
            function.put("name", toolName);
            function.put("arguments", "{}");
            call_.set("function", function);
            delta.set("tool_calls", mapper.createArrayNode().add(call_));
        } else {
            delta.put("content", "final-answer");
        }
        // 合法终止：每一轮都带真实的 finish_reason（第一轮 tool_calls，之后 stop）。
        // 节点侧现在按它判定「最后一轮是否说完整」（M3-NODE-LINK-01）；本替身若继续
        // 发 null，正常场景会被正确地判成不完整 —— 那是夹具的问题，不是被测方的问题。
        sse.append("data: ").append(mapper.writeValueAsString(
                withDelta(first, delta, call == 1 ? "tool_calls" : "stop"))).append("\n\n");
        sse.append("data: [DONE]\n\n");
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(sse.toString().getBytes(StandardCharsets.UTF_8));
        }
    }

    private ObjectNode chunkSkeleton(int call) {
        ObjectNode root = mapper.createObjectNode();
        root.put("id", "chatcmpl-" + call);
        root.put("object", "chat.completion.chunk");
        root.put("created", 0);
        root.put("model", "synthetic");
        return root;
    }

    private ObjectNode withDelta(ObjectNode skeleton, ObjectNode delta) {
        return withDelta(skeleton, delta, null);
    }

    private ObjectNode withDelta(ObjectNode skeleton, ObjectNode delta, String finishReason) {
        ObjectNode choice = mapper.createObjectNode();
        choice.put("index", 0);
        choice.set("delta", delta);
        if (finishReason == null) {
            choice.putNull("finish_reason");
        } else {
            choice.put("finish_reason", finishReason);
        }
        ArrayNode choices = mapper.createArrayNode().add(choice);
        skeleton.set("choices", choices);
        return skeleton;
    }

    private ObjectNode usage() {
        ObjectNode usage = mapper.createObjectNode();
        usage.put("prompt_tokens", 1);
        usage.put("completion_tokens", 1);
        usage.put("total_tokens", 2);
        return usage;
    }

    // ------------------------------------------------------------------ claim

    private void handleClaim(HttpExchange exchange) throws IOException {
        String path = exchange.getRequestURI().getPath();
        exchange.getRequestBody().readAllBytes();
        if (path.startsWith("/v1/internal/charging-status/completions/")) {
            // 整轮终局通知：**不是**一次业务领取，不进入 claim 计数
            // （否则「业务调用计数」会随通知变化，把两件事混成一个数）。
            completionCalls.incrementAndGet();
            completionPaths.add(path);
            ObjectNode ack = mapper.createObjectNode();
            ack.put("run_id", path.substring(path.lastIndexOf('/') + 1));
            ack.put("state", "succeeded");
            send(exchange, 200, "application/json", mapper.writeValueAsBytes(ack));
            return;
        }
        claimCalls.incrementAndGet();
        claimPaths.add(path);
        ObjectNode body = mapper.createObjectNode();
        body.put("order_no", "SYNTHETIC-ORDER-0001");
        body.put("status_code", "CHARGING");
        body.put("status_role", "consumer");
        body.put("observed_at", "2026-01-01T00:00:00Z");
        body.put("source", "synthetic");
        body.put("environment", "local");
        body.put("gateway_version", "synthetic");
        body.put("actual_node_id", "node-local");
        send(exchange, 200, "application/json", mapper.writeValueAsBytes(body));
    }

    // ------------------------------------------------------------------ plumbing

    private static void send(HttpExchange exchange, int status, String contentType, byte[] body)
            throws IOException {
        exchange.getResponseHeaders().add("Content-Type", contentType);
        exchange.sendResponseHeaders(status, body.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(body);
        }
    }
}
