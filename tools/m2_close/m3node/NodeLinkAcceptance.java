package m3node;

import com.aizuda.snail.ai.agent.starter.EnableSnailAiAgent;
import com.aizuda.snail.ai.common.dto.agent.ChatDispatchRequest;
import com.aizuda.snail.ai.common.model.ConfigExtAttrsDTO;
import com.aizuda.snail.ai.agent.core.runtime.ChatSessionRequest;
import com.aizuda.snail.ai.agent.core.runtime.ChatSessionRuntime;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.ComponentScan;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * M3-NODE-LINK-01 的节点侧驱动：在**真实 Spring 装配**上跑一次派发，
 * 领取与整轮通知都由节点自己的类发出（{@code ControlledChargingStatusTool}、
 * {@code ReportingChatSessionRuntime}），打的是**真的 Gateway HTTP**。
 *
 * <p>本类只做一件事：造一个 {@link ChatDispatchRequest} 并调用
 * {@link ChatSessionRuntime#execute} —— 与节点收到一次 gRPC 派发后走的那条路相同。
 * 平台那一跳由 Python 侧替身承担（允许），节点代码不被替换。
 *
 * <p>用法：
 * <pre>
 *   java m3node.NodeLinkAcceptance --run-id=run_... --gateway=http://127.0.0.1:PORT \
 *        --model=http://127.0.0.1:PORT/v1 --scenario=normal --node-token=... \
 *        [--manifest=/path/to/exec-manifest.json] [--second-run-id=run_...]
 * </pre>
 */
@EnableSnailAiAgent
@SpringBootApplication
@ComponentScan({"com.trendpower.aiops.node"})
public class NodeLinkAcceptance {

    static final List<String> RESULTS = new ArrayList<>();
    static int failures;

    public static void main(String[] args) throws Exception {
        String writeManifest = arg(args, "--write-manifest=");
        if (writeManifest != null) {
            // 清单必须与节点**真实**声明的工具三元组逐字一致；让节点自己写，
            // 避免 Python 侧手抄常量（那会变成第二个真相来源）。
            String model = arg(args, "--model=");
            java.nio.file.Files.writeString(
                    java.nio.file.Path.of(writeManifest),
                    manifestJson(model),
                    java.nio.charset.StandardCharsets.UTF_8);
            System.out.println("STATUS=PASS");
            return;
        }
        String runId = arg(args, "--run-id=");
        String secondRunId = arg(args, "--second-run-id=");
        String gateway = arg(args, "--gateway=");
        String model = arg(args, "--model=");
        String scenario = arg(args, "--scenario=");
        String nodeToken = arg(args, "--node-token=");
        String manifest = arg(args, "--manifest=");
        if (runId == null || gateway == null || model == null || scenario == null || nodeToken == null) {
            System.out.println("STATUS=FAIL missing arguments");
            System.exit(2);
        }
        if (manifest != null) {
            System.setProperty("AIOPS_EXEC_MANIFEST", manifest);
        }
        try (ConfigurableApplicationContext ctx = boot(gateway, nodeToken, manifest)) {
            ChatSessionRuntime runtime = ctx.getBean(ChatSessionRuntime.class);
            check("runtime-is-reporting",
                    runtime.getClass().getSimpleName().equals("ReportingChatSessionRuntime"),
                    "class=" + runtime.getClass().getSimpleName());
            switch (scenario) {
                case "normal" -> drive(runtime, dispatch(model, runId, "normal"), true);
                case "round2-500" -> drive(runtime, dispatch(model, runId, "round2-500"), false);
                case "truncated" -> drive(runtime, dispatch(model, runId, "truncated"), false);
                case "claim-refused" -> drive(runtime, dispatch(model, runId, "normal"), false);
                case "cancel" -> cancel(runtime, dispatch(model, runId, "slow"));
                case "two-runs" -> twoRuns(runtime, model, runId, secondRunId);
                case "retry" -> retry(runtime, model, runId);
                default -> {
                    System.out.println("STATUS=FAIL unknown scenario " + scenario);
                    System.exit(2);
                }
            }
        }
        RESULTS.forEach(System.out::println);
        System.out.println("failures=" + failures);
        System.out.println("STATUS=" + (failures == 0 ? "PASS" : "FAIL"));
        System.exit(failures == 0 ? 0 : 1);
    }

    private static ConfigurableApplicationContext boot(String gateway, String nodeToken, String manifest) {
        SpringApplication app = new SpringApplication(NodeLinkAcceptance.class);
        Map<String, Object> props = new HashMap<>();
        props.put("server.port", "0");
        props.put("snail-ai.enabled", "true");
        props.put("snail-ai.server.host", "127.0.0.1");
        // gRPC 端口必须每个进程不同：两 run 交错场景会同时起两个节点，
        // 固定端口会让第二个绑定失败（实测 "Address already in use"）。
        int base = 20000 + (int) (ProcessHandle.current().pid() % 20000);
        props.put("snail-ai.server.port", String.valueOf(base));
        props.put("snail-ai.port", String.valueOf(base + 1));
        props.put("snail-ai.host", "127.0.0.1");
        props.put("snail-ai.app-id", "node-link-acceptance");
        props.put("snail-ai.token", "synthetic");
        props.put("snail-ai.skill-temp-dir", "/tmp/m3link/skills");
        props.put("snail-ai.shell-temp-dir", "/tmp/m3link/script");
        props.put("aiops.node.name", "node-local");
        props.put("aiops.node.build", "m3-node-link-01");
        props.put("aiops.gateway.base-url", gateway);
        props.put("aiops.gateway.node-token", nodeToken);
        if (manifest != null) {
            props.put("AIOPS_EXEC_MANIFEST", manifest);
        }
        app.setDefaultProperties(props);
        return app.run();
    }

    // ------------------------------------------------------------------ 场景

    private static void drive(ChatSessionRuntime runtime, ChatDispatchRequest dispatch,
                              boolean expectCompletion) throws Exception {
        Outcome outcome = run(runtime, dispatch);
        check("completion-expected", expectCompletion == (outcome.completion != null),
                "completion=" + (outcome.completion != null) + " error=" + describe(outcome.error));
    }

    /**
     * 进程中断：**不是**手工返回一个假的 Outcome。
     *
     * <p>这里如实做三件事：发起一次真实派发、等到上游已经吐出第一段文本（说明整轮已经
     * 进了第二段）、然后由本进程自己 `Runtime.halt` 终止 —— 模拟部署重启/OOM kill。
     * 进程退出后没有任何 completion/error 回调（实测），记录停在 running，
     * 由 Python 侧核对「没有完成通知」与「TTL 收敛为 expired」。
     *
     * <p>因为 `halt` 会杀掉 JVM，这个场景**不打印 STATUS**：它返回的退出码本身就是信号，
     * 判据全部落在 Python 侧读库里。
     */
    private static void cancel(ChatSessionRuntime runtime, ChatDispatchRequest dispatch) throws Exception {
        CountDownLatch firstChunk = new CountDownLatch(1);
        ChatSessionRequest request = ChatSessionRequest.builder()
                .dispatchRequest(dispatch)
                .textConsumer(chunk -> firstChunk.countDown())
                .thinkingConsumer(chunk -> { })
                .completionConsumer(result -> {
                    System.out.println("UNEXPECTED_COMPLETION " + result.fullText());
                })
                .errorConsumer(failure -> {
                    System.out.println("UNEXPECTED_ERROR " + failure.getClass().getSimpleName());
                })
                .build();
        runtime.execute(request);
        if (!firstChunk.await(60, TimeUnit.SECONDS)) {
            System.out.println("STATUS=FAIL no first chunk before interruption");
            System.out.flush();
            Runtime.getRuntime().halt(3);
        }
        System.out.println("INTERRUPTING_NODE_PROCESS");
        System.out.flush();
        // 模拟进程被杀：不执行任何关闭逻辑，也就不可能有完成通知。
        Runtime.getRuntime().halt(9);
    }

    private static void twoRuns(ChatSessionRuntime runtime, String model, String runA, String runB)
            throws Exception {
        AtomicReference<Outcome> a = new AtomicReference<>();
        AtomicReference<Outcome> b = new AtomicReference<>();
        Thread ta = new Thread(() -> a.set(runQuiet(runtime, dispatch(model, runA, "normal"))));
        Thread tb = new Thread(() -> b.set(runQuiet(runtime, dispatch(model, runB, "normal"))));
        ta.start();
        tb.start();
        ta.join(120_000);
        tb.join(120_000);
        check("two-runs:a-completed", a.get() != null && a.get().completion != null,
                "a=" + describe(a.get() == null ? null : a.get().error));
        check("two-runs:b-completed", b.get() != null && b.get().completion != null,
                "b=" + describe(b.get() == null ? null : b.get().error));
    }

    private static void retry(ChatSessionRuntime runtime, String model, String runId) throws Exception {
        Outcome first = run(runtime, dispatch(model, runId, "normal"));
        check("retry:first-completed", first.completion != null, describe(first.error));
        // 同一个 run 再跑一次：Gateway 已经 succeeded，工具领取必须交回**同一份**快照。
        Outcome second = run(runtime, dispatch(model, runId, "normal"));
        check("retry:second-completed", second.completion != null, describe(second.error));
    }

    // ------------------------------------------------------------------ 驱动

    record Outcome(String completion, Throwable error) {
    }

    private static Outcome runQuiet(ChatSessionRuntime runtime, ChatDispatchRequest dispatch) {
        try {
            return run(runtime, dispatch);
        } catch (Exception e) {
            return new Outcome(null, e);
        }
    }

    private static Outcome run(ChatSessionRuntime runtime, ChatDispatchRequest dispatch)
            throws Exception {
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<String> text = new AtomicReference<>("");
        AtomicReference<String> completion = new AtomicReference<>();
        AtomicReference<Throwable> error = new AtomicReference<>();
        ChatSessionRequest request = ChatSessionRequest.builder()
                .dispatchRequest(dispatch)
                .textConsumer(chunk -> text.updateAndGet(prev -> prev + chunk))
                .thinkingConsumer(chunk -> { })
                .completionConsumer(result -> {
                    completion.set(result.fullText());
                    done.countDown();
                })
                .errorConsumer(failure -> {
                    error.set(failure);
                    done.countDown();
                })
                .build();
        runtime.execute(request);
        if (!done.await(120, TimeUnit.SECONDS)) {
            return new Outcome(null, new IllegalStateException("timeout"));
        }
        return new Outcome(completion.get(), error.get());
    }

    private static String manifestJson(String modelBaseUrl) {
        String description = com.trendpower.aiops.node.ControlledChargingStatusTool.DESCRIPTION
                .replace("\\", "\\\\").replace("\"", "\\\"");
        String schema = com.trendpower.aiops.node.ControlledChargingStatusTool.SCHEMA
                .replace("\\", "\\\\").replace("\"", "\\\"");
        return """
                {"schema":"m3-exec-config/1","name":"m3-node-link-01","history_policy":"none",
                 "model":{"provider_key":"openai-compatible","adapter_key":"openai-compatible",
                          "model_key":"synthetic-model","endpoint":"%s"},
                 "model_options":{"temperature":0.2,"top_p":0.9,"max_tokens":64},
                 "system_messages":["SYSTEM-PROMPT"],
                 "tools":[{"name":"%s","description":"%s","input_schema":"%s"}]}
                """.formatted(modelBaseUrl,
                com.trendpower.aiops.node.ControlledChargingStatusTool.TOOL_NAME,
                description, schema);
    }

    private static ChatDispatchRequest dispatch(String modelBaseUrl, String runId, String mode) {
        ChatDispatchRequest d = new ChatDispatchRequest();
        d.setRequestId("m3link-" + mode);
        d.setSid(runId);
        d.setConversationId("conv-" + mode);
        d.setSystemPrompt("SYSTEM-PROMPT");
        d.setUserMessage("请给我这个订单的充电状态");
        ChatDispatchRequest.ModelConfig mc = new ChatDispatchRequest.ModelConfig();
        mc.setModelConfigId(1L);
        mc.setProviderKey("openai-compatible");
        mc.setAdapterKey("openai-compatible");
        mc.setModelKey("synthetic-model");
        mc.setApiEndpoint(modelBaseUrl);
        mc.setApiKey("synthetic-not-a-real-key");
        ConfigExtAttrsDTO ext = new ConfigExtAttrsDTO();
        ext.setTemperature(0.2d);
        ext.setTopP(0.9d);
        ext.setMaxTokens(64);
        mc.setConfigJson(ext);
        d.setModelConfig(mc);
        return d;
    }

    // ------------------------------------------------------------------ 工具方法

    private static void check(String name, boolean ok, String detail) {
        RESULTS.add((ok ? "PASS " : "FAIL ") + name + "  " + detail);
        if (!ok) {
            failures++;
        }
    }

    private static String describe(Throwable t) {
        if (t == null) {
            return "none";
        }
        Throwable root = t;
        while (root.getCause() != null && root.getCause() != root) {
            root = root.getCause();
        }
        return t.getClass().getSimpleName() + "<" + root.getClass().getSimpleName() + ": "
                + root.getMessage() + ">";
    }

    private static String arg(String[] args, String prefix) {
        for (String a : args) {
            if (a.startsWith(prefix)) {
                return a.substring(prefix.length());
            }
        }
        return null;
    }
}
