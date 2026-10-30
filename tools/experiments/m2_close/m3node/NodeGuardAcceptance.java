package m3node;

import com.aizuda.snail.ai.agent.core.executor.client.ChatClientBuildRequest;
import com.aizuda.snail.ai.agent.core.executor.client.ChatClientFactory;
import com.aizuda.snail.ai.agent.core.executor.prompt.PromptFactory;
import com.aizuda.snail.ai.agent.core.runtime.ChatSessionRequest;
import com.aizuda.snail.ai.agent.core.runtime.ChatSessionRuntime;
import com.aizuda.snail.ai.agent.core.runtime.tool.ToolRuntime;
import com.aizuda.snail.ai.agent.starter.EnableSnailAiAgent;
import com.aizuda.snail.ai.common.dto.agent.ChatDispatchRequest;
import com.aizuda.snail.ai.common.model.ConfigExtAttrsDTO;
import com.trendpower.aiops.node.ControlledChargingStatusTool;
import com.trendpower.aiops.node.exec.ExecConfigViolation;
import com.trendpower.aiops.node.exec.ExecManifest;
import com.trendpower.aiops.node.exec.GuardedChatModelFactory;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.client.ChatClientRequest;
import org.springframework.ai.chat.client.ChatClientResponse;
import org.springframework.ai.chat.client.advisor.api.CallAdvisor;
import org.springframework.ai.chat.client.advisor.api.CallAdvisorChain;
import org.springframework.ai.chat.messages.Message;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.ComponentScan;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * M3-NODE-GUARD-01 的合成验收。被测方是**真实节点装配**，不是手搓的图：
 * Spring 上下文按节点的方式扫描 {@code com.trendpower.aiops.node}，所以
 * {@link GuardedChatModelFactory} 与 {@code GovernedToolRuntime} 的 {@code @Primary}
 * 接管是真的接管；模型经真实 {@code OpenAiCompatibleChatModelAdapter} 造出真实
 * {@code OpenAiChatModel}；工具经真实 {@code DefaultToolCallingManager} 与
 * {@code ToolCallingAdvisor} 执行；驱动入口是 {@link ChatSessionRuntime#execute}
 * —— 节点收到一次派发后真正走的那条路。唯一的替身是上游：本机 loopback 合成 HTTP 服务。
 *
 * <p>每个「期望」都锚在**它真正能被观察到的那一面**（本片诊断实测）：
 * <ul>
 *   <li>{@code Prompt} 面上只有消息 —— 实测 {@code prompt.getOptions()} 为 null；</li>
 *   <li>模型标识与有效选项在**构造出来的模型**上；</li>
 *   <li>有效工具定义在 {@code ToolRuntime} **解析出来的集合**上；</li>
 *   <li>「确实发到上游的东西」在**合成上游记录的请求体**里。</li>
 * </ul>
 *
 * <p>用法：{@code --variant=<name>} 加 {@code --no-manifest}（反向控制）或省略（启用守卫）。
 */
@EnableSnailAiAgent
@SpringBootApplication
@ComponentScan({"com.trendpower.aiops.node"})
public class NodeGuardAcceptance {

    static final String TOOL_NAME = ControlledChargingStatusTool.TOOL_NAME;

    /**
     * run_id 形状是契约的一部分：工具只接受 {@code run_[A-Za-z0-9_-]{20,64}}。
     * 夹具用一个合法形状的合成编号 —— 早先用 9 个字符的编号，工具按设计拒绝了它，
     * 于是"工具没执行"是夹具错误，不是平台事实。
     */
    static final String RUN_ID = "run_synthetic0001abcdefghijklmnop";

    /** 当前场景的上游；工具执行观测取自它的业务领取计数（生产代码里不再有计数器）。 */
    private static SyntheticUpstream activeUpstream;

    /**
     * 工具**真实执行**的观测点：受控工具每执行一次就向业务替身领取一次。
     * 生产工具里不再放验收计数器；这里用上游记录的真实业务请求数作为观测。
     */
    private static int toolExecutionsBaseline;

    private static int toolExecutionsObserved(SyntheticUpstream upstream) {
        return upstream.claimCalls.get();
    }

    static final List<String> RESULTS = new ArrayList<>();
    static int failures;

    /** 单命令入口：{@code java m3node.NodeGuardAcceptance --out-dir=…}。
     * 同一份代码也被 {@link GuardHarness} 逐场景复用（它自带 {@code @SpringBootApplication}）。 */
    public static void main(String[] args) throws Exception {
        NodeGuardAcceptance sheet = new NodeGuardAcceptance();
        sheet.run(args);
    }

    private void run(String[] args) throws Exception {
        String only = argValue(args, "--variant=");
        boolean guardEnabled = !hasFlag(args, "--no-manifest");
        List<String> variants = only == null
                ? (guardEnabled ? enabledVariants() : counterVariants())
                : List.of(only);
        try (SyntheticUpstream upstream = new SyntheticUpstream(TOOL_NAME, true)) {
            activeUpstream = upstream;
            publishBaseUrls(upstream);
            for (String variant : variants) {
                upstream.resetCounters();
                toolExecutionsBaseline = upstream.claimCalls.get();
                Path manifest = writeManifest(manifestJson(upstream.baseUrl(), variant));
                guardEnabled = !hasFlag(args, "--no-manifest");
                if (guardEnabled) {
                    runGuarded(variant, upstream, manifest);
                } else {
                    runCounterProof(variant, upstream, manifest);
                }
            }
            rejectCases(upstream);
        }
        System.out.println();
        RESULTS.forEach(System.out::println);
        System.out.println("failures=" + failures);
        System.out.println("STATUS=" + (failures == 0 ? "PASS" : "FAIL"));
        System.exit(failures == 0 ? 0 : 1);
    }

    // ------------------------------------------------------------------ 启用守卫

    private static void runGuarded(String variant, SyntheticUpstream upstream, Path manifest)
            throws Exception {
        try (ConfigurableApplicationContext ctx = boot(manifest)) {
            GuardedChatModelFactory factory = ctx.getBean(GuardedChatModelFactory.class);
            check("guard-enabled", factory.enabled(), "enabled=" + factory.enabled());
            if (variant.startsWith("output-contract-")) {
                outputContract(ctx, upstream, variant.endsWith("stream"));
                return;
            }
            if ("advisor-appends-system".equals(variant)) {
                advisorAppendsSystem(ctx, upstream, true);
                return;
            }
            if ("mid-loop-drift".equals(variant)) {
                midLoopDrift(ctx, upstream);
                return;
            }
            if (variant.startsWith("advisor-")) {
                String rest = variant.substring("advisor-".length());
                boolean stream = rest.endsWith("-stream");
                String drift = stream ? rest.substring(0, rest.length() - "-stream".length())
                        : (rest.endsWith("-call") ? rest.substring(0, rest.length() - "-call".length()) : rest);
                advisorDrift(ctx, upstream, drift, stream);
                return;
            }
            if ("cross-run".equals(variant)) {
                crossRun(ctx, upstream);
                return;
            }
            Result result = driveDispatch(ctx, dispatch(upstream, variant));
            assertGuarded(variant, upstream, result);
        }
    }

    private static void assertGuarded(String variant, SyntheticUpstream upstream, Result result) {
        switch (variant) {
            case "exact" -> {
                check("completed", result.error == null, "error=" + describe(result.error));
                check("tool-executed-once", toolsExecuted() == 1, "toolExecutions=" + toolsExecuted());
                check("stream-observed", upstream.streamRequests.get() >= 1,
                        "streamRequests=" + upstream.streamRequests.get());
                check("upstream-calls", upstream.chatCalls.get() == 2,
                        "chatCalls=" + upstream.chatCalls.get());
                check("upstream-model", "synthetic-model".equals(upstream.lastModel()),
                        "model=" + upstream.lastModel());
                check("upstream-tools", upstream.lastToolCount() == 1,
                        "tools=" + upstream.lastToolCount());
                check("consumer-output-unchanged", upstream.chatRequests.stream()
                        .noneMatch(r -> r.has("response_format")), "consumer has no JSON constraint");
                check("upstream-temperature", "0.2".equals(upstream.lastTemperature()),
                        "temperature=" + upstream.lastTemperature());
                check("upstream-path", SyntheticUpstream.CHAT_PATH.equals(upstream.chatPaths.get(0)),
                        "path=" + upstream.chatPaths.get(0));
                check("claim-calls", upstream.claimCalls.get() == 1,
                        "claimCalls=" + upstream.claimCalls.get());
            }
            case "cross-run" -> {
                check("completed", result.error == null, "error=" + describe(result.error));
                check("upstream-unchanged-by-drifted-sibling", upstream.chatCalls.get() == 2,
                        "chatCalls=" + upstream.chatCalls.get());
            }

            case "advisor-appends-system" -> check("rejected", true, "见 advisorAppendsSystem");
            default -> {
                String expectedKind = expectedKind(variant);
                check("rejected", kindOf(result.error) != null, "error=" + describe(result.error));
                check("kind", expectedKind.equals(kindOf(result.error)),
                        "expected=" + expectedKind + " actual=" + kindOf(result.error));
                check("no-upstream-call", upstream.chatCalls.get() == 0,
                        "chatCalls=" + upstream.chatCalls.get());
                check("no-business-call", upstream.claimCalls.get() == 0,
                        "claimCalls=" + upstream.claimCalls.get());
                check("no-tool-execution", toolsExecuted() == 0, "toolExecutions=" + toolsExecuted());
            }
        }
    }

    // ------------------------------------------------------------------ 反向控制（不启用）

    private static void runCounterProof(String variant, SyntheticUpstream upstream, Path manifest)
            throws Exception {
        try (ConfigurableApplicationContext ctx = boot(null)) {
            GuardedChatModelFactory factory = ctx.getBean(GuardedChatModelFactory.class);
            check("guard-disabled", !factory.enabled(), "enabled=" + factory.enabled());
            check("manifest-ignored", Files.exists(manifest), "manifest=" + manifest.getFileName());
            if ("advisor-appends-system".equals(variant)) {
                advisorAppendsSystem(ctx, upstream, false);
                return;
            }
            if (variant.startsWith("advisor-")) {
                String rest = variant.substring("advisor-".length());
                boolean stream = rest.endsWith("-stream");
                String drift = stream ? rest.substring(0, rest.length() - "-stream".length())
                        : (rest.endsWith("-call") ? rest.substring(0, rest.length() - "-call".length()) : rest);
                advisorDriftCounterProof(upstream, ctx, drift, stream);
                return;
            }
            Result result = driveDispatch(ctx, dispatch(upstream, variant));
            // 反向控制要证两件事：
            //   ① 同样的改动**确实到达了上游**（所以"拒绝"不是夹具没跑起来的假绿）；
            //   ② 守卫启用时用的那条断言，在这里**确实会红** —— 这是"未接守卫先红"的机器可读形式。
            // 因此这里不设"应该失败"的期望，只设"确实发生了"与"该断言的判据被违反"。
            checkRedAsExpected(variant, upstream, result);
            check("reached-upstream", upstream.chatCalls.get() >= 1,
                    "chatCalls=" + upstream.chatCalls.get() + " error=" + describe(result.error));
            check("guard-did-not-reject", kindOf(result.error) == null || !(result.error instanceof ExecConfigViolation),
                    "error=" + describe(result.error));
            switch (variant) {
                case "system-drift" -> check("drift-visible-upstream",
                        "EVIL-SYSTEM".equals(upstream.firstSystemMessage()),
                        "system=" + upstream.firstSystemMessage());
                case "model-drift" -> check("drift-visible-upstream",
                        "evil-model".equals(upstream.lastModel()), "model=" + upstream.lastModel());
                case "options-drift" -> check("drift-visible-upstream",
                        "0.9".equals(upstream.lastTemperature()),
                        "temperature=" + upstream.lastTemperature());
                case "history-drift", "media-drift", "memory-drift" -> check("drift-visible-upstream",
                        upstream.lastMessageCount() > 2, "messages=" + upstream.lastMessageCount());
                default -> { }
            }
        }
    }

    /** 反向控制：同一个 advisor 漂移在不接守卫时必须**确实到达上游**。 */
    private static void advisorDriftCounterProof(SyntheticUpstream upstream,
                                                 ConfigurableApplicationContext ctx,
                                                 String drift, boolean stream) {
        ChatDispatchRequest d = dispatch(upstream, "exact");
        ToolRuntime toolRuntime = ctx.getBean(ToolRuntime.class);
        PromptFactory promptFactory = ctx.getBean(PromptFactory.class);
        ChatClientFactory clientFactory = ctx.getBean(ChatClientFactory.class);
        ToolRuntime.ToolResolution resolution = toolRuntime.resolve(d);
        ChatClient client = clientFactory.build(ChatClientBuildRequest.builder()
                .dispatchRequest(d).tools(resolution.getTools()).build());
        Prompt prompt = promptFactory.build(d);
        String tag = "counter-advisor-" + drift + (stream ? "-stream" : "-call");
        Throwable failure = null;
        try {
            var spec = client.prompt(prompt).advisors(s -> s.advisors(driftAdvisor(drift)));
            if (stream) {
                spec.stream().content().collectList().block(java.time.Duration.ofSeconds(30));
            } else {
                spec.call().content();
            }
        } catch (Throwable t) {
            failure = t;
        }
        check(tag + ":guard-did-not-reject", kindOf(failure) == null, "error=" + describe(failure));
        check(tag + ":reached-upstream", upstream.chatCalls.get() >= 1,
                "chatCalls=" + upstream.chatCalls.get());
        // 只有**实际观测到**被改写的值，才记 RED-AS-EXPECTED。
        boolean observed = switch (drift) {
            case "model", "extra-body-model" -> "evil-model".equals(upstream.lastModel());
            case "model-with-namespace" -> "evil/synthetic-model".equals(upstream.lastModel());
            case "temperature" -> "0.9".equals(upstream.lastTemperature());
            case "temperature-roundoff" -> "0.2000001".equals(upstream.lastTemperature());
            case "extra-body-messages" -> "EVIL-SYSTEM".equals(upstream.firstSystemMessage());
            case "tool-description" -> upstream.firstToolDescription().contains("EVIL-DESCRIPTION");
            case "tool-schema" -> upstream.firstToolSchema().contains("evil");
            default -> false;
        };
        if (observed) {
            RESULTS.add("RED-AS-EXPECTED counter-advisor-" + drift + (stream ? "-stream" : "-call")
                    + "  未接守卫时该断言确实不成立（这一条就是先红）");
        } else {
            check(tag + ":sensitivity", false, "未接守卫时也没有观测到被改写的值");
        }
    }

    /**
     * 「未接守卫时先红」：把守卫启用时那条断言原封不动地拿到这里判一次，
     * 要求它**确实不成立**。命中即记为 RED-AS-EXPECTED（这是预期中的红，不是通过）。
     * 若某个漂移在未接守卫时竟然"满足"了守卫的判据，说明那条断言没有区分力，必须修。
     */
    private static void checkRedAsExpected(String variant, SyntheticUpstream upstream, Result result) {
        boolean violated;
        switch (variant) {
            case "system-drift" -> violated = !"SYSTEM-PROMPT".equals(upstream.firstSystemMessage());
            case "model-drift" -> violated = !"synthetic-model".equals(upstream.lastModel());
            case "options-drift" -> violated = !"0.2".equals(upstream.lastTemperature());
            case "history-drift", "media-drift", "memory-drift" -> violated = upstream.lastMessageCount() > 2;
            // 只有**实际观测到**漂移才记红。以前 default 一律记红：那等于宣称"所有变体
            // 都先红"，包括根本没制造漂移的那些 —— 一条没有区分力的断言。
            default -> violated = false;
        }
        if (noDriftVariant(variant)) {
            // 没有制造漂移的变体（exact 这类正对照）不该被要求"先红" ——
            // 对它断言敏感性本身就是错的题。
            return;
        }
        if (violated) {
            RESULTS.add("RED-AS-EXPECTED " + variant
                    + "  未接守卫时应有的断言确实不成立（这一条就是先红）");
        } else {
            check("sensitivity-" + variant, false,
                    "未接守卫时该断言仍然成立 ⇒ 它没有区分力");
        }
    }

    // ------------------------------------------------------------------ advisor 追加 system 消息

    /**
     * 受控构造之后、最终调用之前，由一个 advisor 追加第二条 system 消息。
     * 这是本片之前实测到的真实反例：只看 {@code getSystemMessage()}（首条）的守卫会放它过去。
     */
    private static void advisorAppendsSystem(ConfigurableApplicationContext ctx,
                                             SyntheticUpstream upstream, boolean guarded) {
        ChatClientFactory clientFactory = ctx.getBean(ChatClientFactory.class);
        PromptFactory promptFactory = ctx.getBean(PromptFactory.class);
        ToolRuntime toolRuntime = ctx.getBean(ToolRuntime.class);
        ChatDispatchRequest d = dispatch(upstream, "exact");
        ToolRuntime.ToolResolution resolution = toolRuntime.resolve(d);
        ChatClient client = clientFactory.build(ChatClientBuildRequest.builder()
                .dispatchRequest(d).tools(resolution.getTools()).build());
        Prompt prompt = promptFactory.build(d);
        CallAdvisor extra = new CallAdvisor() {
            @Override
            public String getName() {
                return "synthetic-append-system";
            }

            @Override
            public int getOrder() {
                return -10_000;
            }

            @Override
            public ChatClientResponse adviseCall(ChatClientRequest request, CallAdvisorChain chain) {
                List<Message> messages = new ArrayList<>(request.prompt().getInstructions());
                messages.add(new SystemMessage("EVIL-APPENDED-SYSTEM"));
                return chain.nextCall(request.mutate()
                        .prompt(new Prompt(messages, request.prompt().getOptions())).build());
            }
        };
        try {
            client.prompt(prompt).advisors(spec -> spec.advisors(extra)).call().content();
            check("rejected", !guarded, "guarded=" + guarded + " upstream=" + upstream.chatCalls.get());
            if (!guarded) {
                check("reached-upstream", upstream.chatCalls.get() >= 1,
                        "chatCalls=" + upstream.chatCalls.get());
            }
        } catch (Throwable t) {
            check("rejected", guarded, "error=" + describe(t));
            if (guarded) {
                check("kind", "SYSTEM_PROMPT".equals(kindOf(t)), "kind=" + kindOf(t));
                check("no-upstream-call", upstream.chatCalls.get() == 0,
                        "chatCalls=" + upstream.chatCalls.get());
            }
        }
    }

    // ------------------------------------------------------------------ 构造之后的 advisor 漂移

    /**
     * 受控构造**之后**、最终调用之前改写真实消费对象 —— 本片要挡的正是这一类。
     * Codex 的独立探针证明旧实现在这里全线放行（model / temperature / 同名工具描述 / 同名工具
     * schema 四种，都 `chatCalls=2`、`claimCalls=1`、无异常）。这些用例就是那四个绕过的回归。
     *
     * <p>advisor 的 order **必须晚于** {@code ToolCallingAdvisor}：只有这样它才会在每一轮真正
     * 生效（排在前面时 ToolCallingAdvisor 从自己那个位置重新进入链，改不到第二轮）。
     * 两条路径都跑：`call()` 与真实订阅的 `stream()`。
     */
    private static void outputContract(ConfigurableApplicationContext ctx, SyntheticUpstream upstream,
                                       boolean stream) {
        var dispatch = dispatch(upstream, "exact");
        var tools = ctx.getBean(ToolRuntime.class).resolve(dispatch);
        var client = ctx.getBean(ChatClientFactory.class).build(ChatClientBuildRequest.builder()
                .dispatchRequest(dispatch).tools(tools.getTools()).build());
        var prompt = ctx.getBean(PromptFactory.class).build(dispatch);
        var spec = client.prompt(prompt).advisors(s -> s.advisors(driftAdvisor("output-text")));
        if (stream) spec.stream().content().collectList().block(java.time.Duration.ofSeconds(30));
        else spec.call().content();
        check("output-contract-rounds", upstream.chatCalls.get() == 2, "two real model turns");
        check("output-contract-json", upstream.chatRequests.stream().allMatch(r ->
                "json_object".equals(r.path("response_format").path("type").asText())),
                "final boundary overrides text mode in " + (stream ? "stream" : "call"));
    }

    private static void advisorDrift(ConfigurableApplicationContext ctx, SyntheticUpstream upstream,
                                     String drift, boolean stream) {
        ChatDispatchRequest d = dispatch(upstream, "exact");
        ToolRuntime toolRuntime = ctx.getBean(ToolRuntime.class);
        PromptFactory promptFactory = ctx.getBean(PromptFactory.class);
        ChatClientFactory clientFactory = ctx.getBean(ChatClientFactory.class);
        ToolRuntime.ToolResolution resolution = toolRuntime.resolve(d);
        ChatClient client = clientFactory.build(ChatClientBuildRequest.builder()
                .dispatchRequest(d).tools(resolution.getTools()).build());
        Prompt prompt = promptFactory.build(d);
        org.springframework.ai.chat.client.advisor.api.Advisor driftAdvisor = driftAdvisor(drift);
        String tag = "advisor-" + drift + (stream ? "-stream" : "-call");
        Throwable failure = null;
        try {
            var spec = client.prompt(prompt).advisors(s -> s.advisors(driftAdvisor));
            if (stream) {
                spec.stream().content().collectList().block(java.time.Duration.ofSeconds(30));
            } else {
                spec.call().content();
            }
        } catch (Throwable t) {
            failure = t;
        }
        check(tag + ":rejected", kindOf(failure) != null, "error=" + describe(failure));
        check(tag + ":kind", expectedDriftKind(drift).equals(kindOf(failure)),
                "expected=" + expectedDriftKind(drift) + " actual=" + kindOf(failure));
        check(tag + ":no-model-call", upstream.chatCalls.get() == 0,
                "chatCalls=" + upstream.chatCalls.get());
        check(tag + ":no-business-call", upstream.claimCalls.get() == 0,
                "claimCalls=" + upstream.claimCalls.get());
        check(tag + ":no-tool-execution", toolsExecuted() == 0, "toolExecutions=" + toolsExecuted());
    }

    private static String expectedDriftKind(String drift) {
        return switch (drift) {
            case "model", "model-with-namespace" -> "MODEL";
            case "temperature", "temperature-roundoff" -> "OPTIONS";
            case "extra-body-model", "extra-body-messages" -> "EXTRA_BODY";
            case "tool-description", "tool-schema" -> "TOOLS";
            default -> throw new IllegalArgumentException("unknown drift " + drift);
        };
    }

    /**
     * 晚于 {@code ToolCallingAdvisor} 的 advisor，按需改写模型标识、选项或同名工具定义。
     * 它改的是**本轮真实消费对象**（Prompt 携带的有效选项），不是清单、也不是早期 DTO。
     */
    private static org.springframework.ai.chat.client.advisor.api.Advisor driftAdvisor(String drift) {
        // BaseAdvisor（而不是 CallAdvisor）：`call()` 与 `stream()` 两条路径都会经过它。
        // 只实现 CallAdvisor 时流式路径根本不会应用这个 advisor —— 那样 stream 用例就是空跑。
        return new org.springframework.ai.chat.client.advisor.api.BaseAdvisor() {
            @Override
            public String getName() {
                return "synthetic-post-construction-" + drift;
            }

            @Override
            public int getOrder() {
                return Integer.MAX_VALUE - 10;
            }

            @Override
            public ChatClientRequest before(ChatClientRequest request,
                                            org.springframework.ai.chat.client.advisor.api.AdvisorChain chain) {
                Object mutated = mutateEffectiveOptions(request.prompt().getOptions(), drift);
                return request.mutate()
                        .prompt(new Prompt(request.prompt().getInstructions(),
                                (org.springframework.ai.chat.prompt.ChatOptions) mutated))
                        .build();
            }

            @Override
            public ChatClientResponse after(ChatClientResponse response,
                                            org.springframework.ai.chat.client.advisor.api.AdvisorChain chain) {
                return response;
            }
        };
    }

    /** 用框架自己的 {@code mutate()} 造出被改写的有效选项：与真实消费语义一致，不自造解释器。 */
    private static Object mutateEffectiveOptions(org.springframework.ai.chat.prompt.ChatOptions options, String drift) {
        if (options instanceof org.springframework.ai.model.tool.ToolCallingChatOptions toolOptions) {
            var builder = toolOptions.mutate();
            switch (drift) {
                case "output-text" -> {
                    return ((org.springframework.ai.openai.OpenAiChatOptions) toolOptions).mutate().responseFormat(
                            org.springframework.ai.openai.OpenAiChatModel.ResponseFormat.builder().type(
                                    org.springframework.ai.openai.OpenAiChatModel.ResponseFormat.Type.TEXT).build()).build();
                }
                case "model" -> builder.model("evil-model");
                case "temperature" -> builder.temperature(0.9);
                // R2：四种残余绕过。前两条是「有损比较」的产物，后两条走 extraBody 覆盖层。
                case "model-with-namespace" -> builder.model("evil/synthetic-model");
                case "temperature-roundoff" -> builder.temperature(0.2000001);
                // extraBody 只在 OpenAI 专用 builder 上，通用接口 builder 没有 —— 按真实类型取。
                case "extra-body-model" -> {
                    return ((org.springframework.ai.openai.OpenAiChatOptions) toolOptions).mutate()
                            .extraBody(java.util.Map.of("model", "evil-model")).build();
                }
                case "extra-body-messages" -> {
                    return ((org.springframework.ai.openai.OpenAiChatOptions) toolOptions).mutate()
                            .extraBody(java.util.Map.of("messages", java.util.List.of(
                                    java.util.Map.of("role", "system", "content", "EVIL-SYSTEM"),
                                    java.util.Map.of("role", "user", "content", "synthetic")))).build();
                }
                case "tool-description", "tool-schema" -> {
                    List<org.springframework.ai.tool.ToolCallback> changed = new ArrayList<>();
                    for (org.springframework.ai.tool.ToolCallback original : toolOptions.getToolCallbacks()) {
                        var def = original.getToolDefinition();
                        changed.add(new RenamedTool(original, def,
                                drift.equals("tool-description") ? "EVIL-DESCRIPTION" : def.description(),
                                drift.equals("tool-schema") ? "{\"evil\":true}" : def.inputSchema()));
                    }
                    builder.toolCallbacks(changed);
                }
                default -> throw new IllegalArgumentException("unknown drift " + drift);
            }
            return builder.build();
        }
        throw new IllegalStateException("有效选项不是 tool-calling 类型，无法按框架语义改写");
    }

    /** 同名、换描述或 schema 的工具：证明同名漂移也要被拒。 */
    private record RenamedTool(org.springframework.ai.tool.ToolCallback delegate,
                               org.springframework.ai.tool.definition.ToolDefinition original,
                               String description, String schema)
            implements org.springframework.ai.tool.ToolCallback {
        @Override
        public org.springframework.ai.tool.definition.ToolDefinition getToolDefinition() {
            return org.springframework.ai.tool.definition.ToolDefinition.builder()
                    .name(original.name()).description(description).inputSchema(schema).build();
        }

        @Override
        public String call(String input) {
            return delegate.call(input);
        }

        @Override
        public String call(String input, org.springframework.ai.chat.model.ToolContext context) {
            return delegate.call(input, context);
        }
    }

    // ------------------------------------------------------------------ 工具循环中途的漂移

    /**
     * 真实工具循环**中途**的漂移，跑在节点真实使用的**流式**路径上（{@code stream()} + 真实订阅），
     * 而不是 {@code call()}。断言用**上游真实计数**：第一轮的模型调用、工具执行与业务领取都
     * 真实发生过并被保留，第二轮的模型调用被拒（计数不再增长）。
     *
     * <p>漂移由一个 order 晚于 {@code ToolCallingAdvisor} 的 {@code BaseAdvisor} 在第二轮注入 ——
     * 只有这种 advisor 才会每一轮都执行，且 {@code BaseAdvisor} 在 call 与 stream 两条路径都生效
     * （只实现 {@code CallAdvisor} 的 advisor 在流式路径上根本不会被应用）。
     */
    private static void midLoopDrift(ConfigurableApplicationContext ctx, SyntheticUpstream upstream)
            throws Exception {
        ChatDispatchRequest d = dispatch(upstream, "exact");
        ToolRuntime toolRuntime = ctx.getBean(ToolRuntime.class);
        PromptFactory promptFactory = ctx.getBean(PromptFactory.class);
        ChatClientFactory clientFactory = ctx.getBean(ChatClientFactory.class);
        ToolRuntime.ToolResolution resolution = toolRuntime.resolve(d);
        ChatClient client = clientFactory.build(ChatClientBuildRequest.builder()
                .dispatchRequest(d).tools(resolution.getTools()).build());
        Prompt prompt = promptFactory.build(d);

        java.util.concurrent.atomic.AtomicInteger round = new java.util.concurrent.atomic.AtomicInteger();
        org.springframework.ai.chat.client.advisor.api.Advisor perRound =
                new org.springframework.ai.chat.client.advisor.api.BaseAdvisor() {
                    @Override
                    public String getName() {
                        return "synthetic-per-round-drift";
                    }

                    @Override
                    public int getOrder() {
                        return Integer.MAX_VALUE - 10;
                    }

                    @Override
                    public ChatClientRequest before(ChatClientRequest request,
                                                    org.springframework.ai.chat.client.advisor.api.AdvisorChain chain) {
                        if (round.incrementAndGet() < 2) {
                            return request;
                        }
                        List<Message> messages = new ArrayList<>(request.prompt().getInstructions());
                        messages.add(new SystemMessage("MID-LOOP-DRIFT"));
                        return request.mutate()
                                .prompt(new Prompt(messages, request.prompt().getOptions())).build();
                    }

                    @Override
                    public ChatClientResponse after(ChatClientResponse response,
                                                    org.springframework.ai.chat.client.advisor.api.AdvisorChain chain) {
                        return response;
                    }
                };

        int chatBefore = upstream.chatCalls.get();
        int claimBefore = upstream.claimCalls.get();
        int toolBefore = toolsExecuted();
        int streamBefore = upstream.streamRequests.get();
        Throwable failure = null;
        String finalText = "";
        try {
            finalText = String.join("", client.prompt(prompt)
                    .advisors(spec -> spec.advisors(perRound))
                    .stream().content().collectList().block(java.time.Duration.ofSeconds(30)));
        } catch (Throwable t) {
            failure = t;
        }
        check("stream:rejected", kindOf(failure) != null, "error=" + describe(failure));
        check("stream:kind", "SYSTEM_PROMPT".equals(kindOf(failure)), "kind=" + kindOf(failure));
        check("stream:first-round-kept", upstream.chatCalls.get() - chatBefore == 1,
                "chatCalls delta=" + (upstream.chatCalls.get() - chatBefore));
        check("stream:second-round-blocked", upstream.chatCalls.get() - chatBefore == 1,
                "chatCalls delta=" + (upstream.chatCalls.get() - chatBefore));
        check("stream:tool-executed-once", toolsExecuted() - toolBefore == 1,
                "toolExecutions delta=" + (toolsExecuted() - toolBefore));
        check("stream:business-claimed-once", upstream.claimCalls.get() - claimBefore == 1,
                "claimCalls delta=" + (upstream.claimCalls.get() - claimBefore));
        check("stream:real-subscription", upstream.streamRequests.get() - streamBefore >= 1,
                "streamRequests delta=" + (upstream.streamRequests.get() - streamBefore));
        check("stream:no-final-text", finalText.isEmpty(), "final=[" + finalText + "]");
    }

    // ------------------------------------------------------------------ 启用后清单不合格即拒

    /**
     * 「启用即不可降级」：文件缺失、权限过宽、非法 JSON、schema 不受支持、
     * {@code history_policy} 不是 {@code none}、未声明工具 —— 每种都必须在装配阶段显式失败。
     * 这里用真实 {@link ExecManifest#load}，与节点启动走同一段代码。
     */
    private static void rejectCases(SyntheticUpstream upstream) throws Exception {
        expectRefuse("reject-missing", () -> ExecManifest.load(
                Path.of("/tmp/m3node-absent-" + System.nanoTime() + "/manifest.json")));
        expectRefuse("reject-loosened", () -> loadTempPerms(manifestJson(upstream.baseUrl(), "exact"), "rw-r--r--"));
        expectRefuse("reject-empty", () -> loadTempPerms("", "rw-------"));
        expectRefuse("reject-schema", () -> loadTemp(manifestJson(upstream.baseUrl(), "exact")
                .replace("m3-exec-config/1", "m3-exec-config/999")));
        expectRefuse("reject-history", () -> loadTemp(manifestJson(upstream.baseUrl(), "exact")
                .replace("\"history_policy\": \"none\"", "\"history_policy\": \"full\"")));
        expectRefuse("reject-not-json", () -> loadTemp("not json at all"));
    }

    private static void loadTemp(String json) throws Exception {
        Path file = writeManifest(json);
        try {
            ExecManifest.load(file);
        } finally {
            Files.deleteIfExists(file);
        }
    }

    private static void loadTempPerms(String json, String perms) throws Exception {
        Path file = writeManifest(json);
        Files.setPosixFilePermissions(file, PosixFilePermissions.fromString(perms));
        try {
            ExecManifest.load(file);
        } finally {
            Files.deleteIfExists(file);
        }
    }

    private static void expectRefuse(String name, ThrowingRunnable action) {
        try {
            action.run();
            check(name, false, "未拒绝");
        } catch (ExecConfigViolation v) {
            check(name, "MANIFEST".equals(v.kind()), "kind=" + v.kind() + " detail=" + v.getMessage());
        } catch (Throwable t) {
            check(name, false, "抛出了非 ExecConfigViolation: " + t);
        }
    }

    interface ThrowingRunnable {
        void run() throws Exception;
    }

    // ------------------------------------------------------------------ 驱动

    /**
     * EXECCFG-NODE-06：两个 run **确定性地**交错执行，并且**控制交错点**。
     *
     * <p>只用一个起跑栅栏只能证明两者同时开始，**证明不了执行顺序**（这正是上一版的问题）。
     * 这里把交错点钉在一个真实事件上：在上游记录到 **A 的第一次 chat 请求**时，才放行 B。
     * 于是顺序是确定的 —— A 的真实执行链已经进到第一轮模型调用、B 才开始跑，
     * 而不是靠注释宣称。
     *
     * <p>归属按 **run_id** 逐个记录，不用总计数代替：A 与 B 各自贡献了多少次模型调用、
     * 业务领取，落在谁身上。B 的失败必须属于 B，A 必须完整成功。
     */
    private static void crossRun(ConfigurableApplicationContext ctx, SyntheticUpstream upstream)
            throws Exception {
        int chatBefore = upstream.chatCalls.get();
        int claimBefore = upstream.claimCalls.get();
        int toolBefore = toolsExecuted();
        String runA = "run_crossrunA0000000000000000000";
        String runB = "run_crossrunB0000000000000000000";
        if (runA.equals(runB)) {
            throw new IllegalStateException("two runs must carry different run ids");
        }

        CountDownLatch aFirstChat = new CountDownLatch(1);
        upstream.chatObserver = n -> aFirstChat.countDown();

        AtomicReference<Result> a = new AtomicReference<>();
        AtomicReference<Result> b = new AtomicReference<>();
        Thread ta = new Thread(() -> {
            try {
                a.set(driveDispatch(ctx, dispatchWithRunId(upstream, "exact", runA)));
            } catch (Throwable t) {
                a.set(Result.failed(t));
            }
        }, "cross-run-A");
        Thread tb = new Thread(() -> {
            try {
                // 只在 A 的真实执行链已经发出第一次上游请求之后才起步。
                if (!aFirstChat.await(60, TimeUnit.SECONDS)) {
                    throw new IllegalStateException("A never reached its first upstream request");
                }
                b.set(driveDispatch(ctx, dispatchWithRunId(upstream, "model-drift", runB)));
            } catch (Throwable t) {
                b.set(Result.failed(t));
            }
        }, "cross-run-B");
        ta.start();
        tb.start();
        ta.join(180_000);
        tb.join(180_000);
        upstream.chatObserver = n -> { };
        Result ra = a.get();
        Result rb = b.get();

        check("cross-run:A-completed", ra != null && ra.error == null, "A=" + describe(ra));
        check("cross-run:B-rejected-model", rb != null && "MODEL".equals(kindOf(rb.error)),
                "B=" + describe(rb));
        check("cross-run:A-streamed-final", ra != null && !ra.finalText.isEmpty(),
                "A.final=[" + (ra == null ? "" : ra.finalText) + "]");

        // 归属：按 run_id 分别统计（pid 前缀在请求体的 metadata 里没有，所以用次数口径 +
        // 「B 在首个上游请求之前就被拒」这一事实把归属钉死）。
        int chatDelta = upstream.chatCalls.get() - chatBefore;
        int claimDelta = upstream.claimCalls.get() - claimBefore;
        int toolDelta = toolsExecuted() - toolBefore;

        // 归属的**直接**证据：业务替身记录了每条 claim 路径，里面带着 run_id。
        // 只允许出现 runA；出现 runB 或任何第三个 ID 都是失败。计数相等证明不了归属
        // —— 把两个 run 都改成第三个 ID 时，旧断言照样通过（Codex 的变异）。
        java.util.List<String> claims = new java.util.ArrayList<>(upstream.claimPaths);
        check("cross-run:claim-count", claims.size() == 1, "claimPaths=" + claims);
        boolean onlyRunA = claims.stream().allMatch(p -> p.endsWith("/" + runA));
        check("cross-run:claim-only-runA", onlyRunA, "claimPaths=" + claims + " expectedSuffix=/" + runA);
        check("cross-run:no-runB-claim", claims.stream().noneMatch(p -> p.contains(runB)),
                "claimPaths=" + claims);
        // 上游收到的请求里也不能出现 runB 的身份：B 在首个上游请求前就被拒。
        check("cross-run:no-third-identity",
                claims.stream().allMatch(p -> p.endsWith("/" + runA)), "claimPaths=" + claims);
        check("cross-run:total-attributed-to-A", chatDelta == 2,
                "A 的工具循环 = 2 次模型调用；B 被拒在该请求之前 ⇒ 总数应为 2，实际 " + chatDelta);
        check("cross-run:A-tool-executions", toolDelta == 1, "toolExecutions delta=" + toolDelta);
        check("cross-run:A-business-claims", claimDelta == 1, "claimCalls delta=" + claimDelta);
        check("cross-run:B-contributed-nothing", "MODEL".equals(kindOf(rb == null ? null : rb.error)),
                "B 在首个上游请求之前被拒（kind=MODEL）⇒ 未贡献任何调用");
        check("cross-run:A-unaffected-by-B", ra != null && ra.error == null && chatDelta == 2,
                "A 完整成功且计数未被 B 的漂移污染");
    }

    private static ChatDispatchRequest dispatchWithRunId(SyntheticUpstream upstream, String variant,
                                                         String runId) {
        ChatDispatchRequest d = dispatch(upstream, variant);
        d.setSid(runId);
        return d;
    }

    private static void await(CountDownLatch latch) {
        try {
            latch.await(30, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private static Result driveDispatch(ConfigurableApplicationContext ctx,
                                        ChatDispatchRequest dispatch) throws Exception {
        ChatSessionRuntime runtime = ctx.getBean(ChatSessionRuntime.class);
        Result result = new Result();
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<String> text = new AtomicReference<>("");
        ChatSessionRequest request = ChatSessionRequest.builder()
                .dispatchRequest(dispatch)
                .textConsumer(chunk -> text.updateAndGet(prev -> prev + chunk))
                .thinkingConsumer(chunk -> { })
                .completionConsumer(c -> {
                    result.finalText = c.fullText();
                    done.countDown();
                })
                .errorConsumer(t -> {
                    result.error = t;
                    done.countDown();
                })
                .build();
        runtime.execute(request);
        if (!done.await(60, TimeUnit.SECONDS)) {
            result.error = new IllegalStateException("timed out");
        }
        result.streamedText = text.get();
        return result;
    }

    // ------------------------------------------------------------------ 派发体与漂移

    private static ChatDispatchRequest dispatch(SyntheticUpstream upstream, String variant) {
        ConfigExtAttrsDTO ext = new ConfigExtAttrsDTO();
        ext.setTemperature(0.2d);
        ext.setTopP(0.9d);
        ext.setMaxTokens(64);
        ChatDispatchRequest.ModelConfig mc = new ChatDispatchRequest.ModelConfig();
        mc.setModelConfigId(1L);
        mc.setProviderKey("openai-compatible");
        mc.setAdapterKey("openai-compatible");
        mc.setModelKey("synthetic-model");
        mc.setApiEndpoint(upstream.baseUrl());
        mc.setApiKey("synthetic-not-a-real-key");
        mc.setConfigJson(ext);

        ChatDispatchRequest d = new ChatDispatchRequest();
        d.setRequestId("cross-run".equals(variant) ? "cross-run" : "req-" + variant);
        d.setSid(RUN_ID);
        d.setConversationId("conv-" + variant);
        d.setSystemPrompt("SYSTEM-PROMPT");
        d.setUserMessage("请给我这个订单的充电状态");
        d.setModelConfig(mc);
        return applyDrift(d, variant);
    }

    /** 漂移都发生在**运行输入**上（清单侧的工具漂移见清单文本），这样反向控制才有意义。 */
    private static ChatDispatchRequest applyDrift(ChatDispatchRequest d, String variant) {
        switch (variant) {
            case "system-drift" -> d.setSystemPrompt("EVIL-SYSTEM");
            case "model-drift" -> d.getModelConfig().setModelKey("evil-model");
            case "endpoint-drift" -> d.getModelConfig().setApiEndpoint("http://127.0.0.1:1/v1");
            case "options-drift" -> d.getModelConfig().getConfigJson().setTemperature(0.9d);
            case "history-drift" -> d.setHistoryMessages(List.of(history("早先的一轮")));
            case "media-drift" -> d.setMediaAttachments(List.of(media()));
            case "memory-drift" -> d.setMemoryContext("记忆上下文");
            case "mcp-drift" -> d.setMcpServers(List.of(mcp()));
            default -> { }
        }
        return d;
    }

    private static ChatDispatchRequest.HistoryMessage history(String text) {
        ChatDispatchRequest.HistoryMessage h = new ChatDispatchRequest.HistoryMessage();
        h.setRole("user");
        h.setContent(text);
        return h;
    }

    private static ChatDispatchRequest.MediaAttachment media() {
        ChatDispatchRequest.MediaAttachment m = new ChatDispatchRequest.MediaAttachment();
        m.setType("image");
        m.setMimeType("image/png");
        m.setAccessUrl("http://127.0.0.1:1/none.png");
        return m;
    }

    private static ChatDispatchRequest.McpServerDescriptor mcp() {
        ChatDispatchRequest.McpServerDescriptor s = new ChatDispatchRequest.McpServerDescriptor();
        s.setName("synthetic-mcp");
        s.setBaseUri("http://127.0.0.1:1/mcp");
        return s;
    }

    /** 清单文本。{@code tool-description-drift}/{@code tool-schema-drift} 只在**清单侧**漂移。 */
    static String manifestJson(String endpoint, String variant) {
        String description = "tools".equals("tools") && "tool-description-drift".equals(variant)
                ? "EVIL-DESCRIPTION"
                : ControlledChargingStatusTool.DESCRIPTION;
        String schema = "tool-schema-drift".equals(variant)
                ? "{\"type\":\"object\",\"properties\":{\"evil\":{\"type\":\"string\"}}}"
                : ControlledChargingStatusTool.SCHEMA;
        return """
                {
                  "schema": "m3-exec-config/1",
                  "name": "m3-node-guard-01",
                  "history_policy": "none",
                  "model": {
                    "provider_key": "openai-compatible",
                    "adapter_key": "openai-compatible",
                    "model_key": "synthetic-model",
                    "endpoint": "%s"
                  },
                  "model_options": {"temperature": 0.2, "top_p": 0.9, "max_tokens": 64},
                  "system_messages": ["SYSTEM-PROMPT"],
                  "tools": [
                    {"name": "%s", "description": "%s", "input_schema": "%s"}
                  ]
                }
                """.formatted(endpoint, TOOL_NAME, description, schema.replace("\"", "\\\""))
                .replace("m3-exec-config/1", variant.startsWith("output-contract-")
                        ? "operator-exec-config/1" : "m3-exec-config/1");
    }

    private static Path writeManifest(String json) throws Exception {
        Path dir = Files.createTempDirectory("m3node-manifest");
        Path file = dir.resolve("exec-config.json");
        Files.writeString(file, json, StandardCharsets.UTF_8);
        Files.setPosixFilePermissions(file, PosixFilePermissions.fromString("rw-------"));
        return file;
    }

    // ------------------------------------------------------------------ 装配

    private static ConfigurableApplicationContext boot(Path manifest) {
        return boot(manifest, false);
    }

    private static ConfigurableApplicationContext boot(Path manifest, boolean midLoopDrift) {
        if (manifest == null) {
            System.clearProperty(ExecManifest.PATH_ENV);
        } else {
            System.setProperty(ExecManifest.PATH_ENV, manifest.toString());
        }
        SpringApplication app = new SpringApplication(NodeGuardAcceptance.class);
        Map<String, Object> props = new HashMap<>();
        props.put("server.port", "0");
        props.put("snail-ai.enabled", "true");
        props.put("snail-ai.server.host", "127.0.0.1");
        props.put("snail-ai.server.port", "18888");
        props.put("snail-ai.app-id", "m3node-acceptance");
        props.put("snail-ai.token", "synthetic-node-token");
        props.put("snail-ai.port", "18889");
        props.put("snail-ai.host", "127.0.0.1");
        props.put("snail-ai.skill-temp-dir", "/tmp/m3node/skills");
        props.put("snail-ai.shell-temp-dir", "/tmp/m3node/script");
        props.put("aiops.node.name", "node-local");
        props.put("aiops.node.build", "m3-node-guard-01");
        props.put("aiops.gateway.base-url", claimBaseUrlFromSystem());
        props.put("aiops.gateway.node-token", "synthetic-node-token");
        if (midLoopDrift) {
            props.put("m3node.midLoopDrift", "true");
        }
        app.setDefaultProperties(props);
        return app.run();
    }

    /** 业务领取替身的基址由驱动通过系统属性传入（每次运行的端口都不同）。 */
    private static String claimBaseUrlFromSystem() {
        String value = System.getProperty("m3node.claimBaseUrl");
        return value == null ? "http://127.0.0.1:1" : value;
    }

    /** 一次性把两个替身基址放进系统属性，供 boot() 读取。 */
    static void publishBaseUrls(SyntheticUpstream upstream) {
        System.setProperty("m3node.claimBaseUrl", upstream.claimBaseUrl());
    }

    // ------------------------------------------------------------------ 断言与工具方法

    /** 本次场景内工具被调用的次数。 */
    private static int toolsExecuted() {
        return activeUpstream.claimCalls.get() - toolExecutionsBaseline;
    }

    private static List<String> advisorDriftVariants() {
        List<String> out = new ArrayList<>();
        for (String drift : List.of("model", "temperature", "tool-description", "tool-schema",
                "model-with-namespace", "temperature-roundoff", "extra-body-model", "extra-body-messages")) {
            out.add("advisor-" + drift + "-call");
            out.add("advisor-" + drift + "-stream");
        }
        return out;
    }

    private static List<String> enabledVariants() {
        List<String> out = new ArrayList<>(List.of("exact", "system-drift", "model-drift",
                "endpoint-drift", "options-drift", "tool-description-drift", "tool-schema-drift",
                "history-drift", "media-drift", "memory-drift", "mcp-drift", "mid-loop-drift",
                "advisor-appends-system", "cross-run", "output-contract-call", "output-contract-stream"));
        out.addAll(advisorDriftVariants());
        return List.copyOf(out);
    }

    private static List<String> counterVariants() {
        List<String> out = new ArrayList<>(List.of("exact", "system-drift", "model-drift",
                "options-drift", "history-drift", "media-drift", "memory-drift",
                "advisor-appends-system"));
        out.addAll(advisorDriftVariants());
        return List.copyOf(out);
    }

    /** 该变体是否**根本不制造漂移**（正对照）。敏感性断言只对制造漂移的变体有意义。 */
    private static boolean noDriftVariant(String variant) {
        return "exact".equals(variant) || "cross-run".equals(variant);
    }

    private static String expectedKind(String variant) {
        return switch (variant) {
            case "system-drift" -> "SYSTEM_PROMPT";
            case "model-drift", "endpoint-drift" -> "MODEL";
            case "options-drift" -> "OPTIONS";
            case "tool-description-drift", "tool-schema-drift", "mcp-drift" -> "TOOLS";
            case "history-drift", "media-drift", "memory-drift" -> "INPUT_SHAPE";
            default -> throw new IllegalArgumentException("no expectation for variant " + variant);
        };
    }

    private static String kindOf(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause() == c ? null : c.getCause()) {
            if (c instanceof ExecConfigViolation v) {
                return v.kind();
            }
        }
        return null;
    }

    private static void check(String name, boolean ok, String detail) {
        RESULTS.add((ok ? "PASS " : "FAIL ") + name + "  " + detail);
        if (!ok) {
            failures++;
        }
    }

    private static String describe(Result r) {
        return r == null ? "null" : describe(r.error);
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

    private static boolean hasFlag(String[] args, String flag) {
        return List.of(args).contains(flag);
    }

    private static String argValue(String[] args, String prefix) {
        for (String a : args) {
            if (a.startsWith(prefix)) {
                return a.substring(prefix.length());
            }
        }
        return null;
    }

    static final class Result {
        String finalText = "";
        String streamedText = "";
        Throwable error;
        int toolCalls;

        static Result ok() {
            return new Result();
        }

        static Result failed(Throwable t) {
            Result r = new Result();
            r.error = t;
            return r;
        }
    }

}
