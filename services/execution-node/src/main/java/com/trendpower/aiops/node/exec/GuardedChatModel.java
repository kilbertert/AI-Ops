package com.trendpower.aiops.node.exec;

import org.springframework.ai.chat.messages.Message;
import org.springframework.ai.chat.messages.MessageType;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.messages.UserMessage;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.prompt.ChatOptions;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.model.tool.ToolCallingChatOptions;
import org.springframework.ai.openai.OpenAiChatOptions;
import org.springframework.ai.openai.OpenAiChatModel.ResponseFormat;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.definition.ToolDefinition;
import reactor.core.publisher.Flux;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/**
 * 最终调用边界的守卫：包在**真实** ChatModel 外面的装饰器。
 *
 * <p><b>它能验什么，是量出来的，不是设想的。</b>在真实装配下实测（本片私有诊断输出）：
 * <ul>
 *   <li>{@code Prompt.getOptions()} 是 <b>null</b>，{@code Prompt} 上也没有工具回调 ——
 *       框架（{@code DefaultChatClientFactory} 只调用 {@code defaultAdvisors}/{@code defaultTools}/
 *       {@code defaultToolContext}，**从不调用** {@code defaultOptions}）把模型与工具放在
 *       ChatClient/模型侧，不放在 Prompt 上。</li>
 *   <li>所以这一层能可靠判定的是：<b>完整的 system 消息集合</b>与**消息形状**（哪一轮允许出现
 *       assistant/tool、第一轮必须恰好一条纯文本用户消息）。</li>
 * </ul>
 *
 * <p>模型标识与有效选项在**构造出来的模型**上（{@code ChatModel.getOptions()}），
 * 有效工具定义在**解析出来的工具集合**上 —— 两处各自在自己的落点校验，见
 * {@link GuardedChatModelFactory} 与 {@code GovernedToolRuntime}。
 *
 * <p>契约保持：{@code call}/{@code stream}/{@code getOptions}/{@code getDefaultOptions} 语义与
 * 委托对象一致；不共享跨 run 可变状态 —— 每次 run 现造一个守卫。
 */
public final class GuardedChatModel implements ChatModel {

    private final ChatModel delegate;
    private final ExecGuard guard;

    public GuardedChatModel(ChatModel delegate, ExecGuard guard) {
        this.delegate = Objects.requireNonNull(delegate, "delegate");
        this.guard = Objects.requireNonNull(guard, "guard");
    }

    public ExecGuard guard() {
        return guard;
    }

    @Override
    public ChatResponse call(Prompt prompt) {
        guard.verifyPrompt(prompt);
        return delegate.call(outputContract(prompt));
    }

    @Override
    public Flux<ChatResponse> stream(Prompt prompt) {
        // 在**订阅之前**校验：漂移时上游一次都不会被调用（Flux 不创建、不订阅）。
        guard.verifyPrompt(prompt);
        return delegate.stream(outputContract(prompt));
    }

    private Prompt outputContract(Prompt prompt) {
        if (!guard.enabled || !guard.manifest.operatorOutput()) return prompt;
        if (!(prompt.getOptions() instanceof OpenAiChatOptions options)) {
            throw new ExecConfigViolation("OUTPUT", "管家执行模型不支持JSON输出选项");
        }
        // 输出模式属于管家协议，在每轮最终发送点设置，不能被编辑器或advisor降回自由文本。
        return new Prompt(prompt.getInstructions(), options.mutate().responseFormat(
                ResponseFormat.builder().type(ResponseFormat.Type.JSON_OBJECT).build()).build());
    }

    @Override
    public ChatOptions getOptions() {
        return delegate.getOptions();
    }

    @Override
    public ChatOptions getDefaultOptions() {
        return delegate.getDefaultOptions();
    }

    /**
     * 一次 run 的期望值 + 各落点的校验器。除「本 run 已发生的模型调用次数」（只影响消息形状判定）
     * 外没有可变状态，且该计数器由构造它的那一次 run 独占。
     */
    public static final class ExecGuard {

        private final ExecManifest manifest;
        private final boolean enabled;
        private int modelCalls;

        ExecGuard(ExecManifest manifest, boolean enabled) {
            this.manifest = manifest;
            this.enabled = enabled;
        }

        public String manifestId() {
            return enabled ? manifest.id() : null;
        }

        public synchronized int modelCalls() {
            return modelCalls;
        }

        // -------------------------------------------------------------- Prompt 面

        public synchronized void verifyPrompt(Prompt prompt) {
            if (!enabled) {
                modelCalls++;
                return;
            }
            List<Message> messages = prompt.getInstructions() == null ? List.of() : prompt.getInstructions();
            verifySystemMessages(messages);
            verifyInputShape(messages);
            // **最终消费对象上的有效选项**。这不是早期 DTO 复核：`ChatClient` 会把
            // `defaultRequestOptions`（模型构造时铸出的 OpenAiChatOptions：model/temperature/topP/
            // maxTokens/baseUrl/apiKey/…）合并进本次 Prompt，而排在 `ToolCallingAdvisor` **之后**的
            // advisor 还能继续改写它。实测（本片私有诊断）：真实路径上这里 options 完整存在。
            // 曾经只查消息、把模型与选项留在构造期查一次，于是这些改写全部溜到了上游。
            verifyModelOptions(prompt.getOptions());
            verifyTools(toolCallbacksOf(prompt.getOptions()));
            modelCalls++;
        }

        /** 从有效选项里取出工具回调；非 tool-calling 选项或没有回调时是空集合。 */
        private static List<ToolCallback> toolCallbacksOf(ChatOptions options) {
            if (options instanceof ToolCallingChatOptions toolOptions && toolOptions.getToolCallbacks() != null) {
                return toolOptions.getToolCallbacks();
            }
            return List.of();
        }

        /**
         * 完整 system 消息集合逐条比对 —— **不能**只看 {@code getSystemMessage()}（只返回首条）。
         * 任何一轮都不允许 system 集合变化。
         */
        private void verifySystemMessages(List<Message> messages) {
            List<String> actual = new ArrayList<>();
            for (Message m : messages) {
                if (m.getMessageType() == MessageType.SYSTEM) {
                    actual.add(((SystemMessage) m).getText());
                }
            }
            if (!actual.equals(manifest.systemMessages())) {
                throw new ExecConfigViolation("SYSTEM_PROMPT",
                        "system 消息集合与清单不一致：期望 " + manifest.systemMessages().size()
                                + " 条，实际 " + actual.size() + " 条");
            }
        }

        /**
         * 固定无历史模式：**首次**模型调用只允许「声明的 system 消息 + 恰好一条纯文本用户消息」；
         * 同一 run 的后续轮允许工具循环自己产生的 assistant/tool 消息。带媒体或非文本的 user
         * 消息一律拒绝。
         *
         * <p>首次调用要求用户消息**恰好一条**，是因为历史消息在 {@code DefaultPromptFactory} 里
         * 也会被映射成消息；只数 assistant/tool 会漏掉「历史被拼成额外 user 消息」这条通道。
         * （正常路径上历史/媒体已在 {@code GovernedToolRuntime.resolve} 提前拒绝，这里是不变量兜底。）
         */
        private void verifyInputShape(List<Message> messages) {
            boolean firstCall = modelCalls == 0;
            int userMessages = 0;
            for (Message m : messages) {
                switch (m.getMessageType()) {
                    case SYSTEM -> {
                        // 上面已逐条比对。
                    }
                    case USER -> {
                        UserMessage user = (UserMessage) m;
                        userMessages++;
                        if (user.getMedia() != null && !user.getMedia().isEmpty()) {
                            throw new ExecConfigViolation("INPUT_SHAPE",
                                    "固定无历史模式下不接受带媒体的用户消息");
                        }
                    }
                    case ASSISTANT, TOOL -> {
                        if (firstCall) {
                            throw new ExecConfigViolation("INPUT_SHAPE",
                                    "固定无历史模式下首次模型调用的输入中不允许出现 "
                                            + m.getMessageType() + " 消息");
                        }
                    }
                    default -> throw new ExecConfigViolation("INPUT_SHAPE",
                            "固定无历史模式下不接受的消息类型：" + m.getMessageType());
                }
            }
            if (firstCall && userMessages != 1) {
                throw new ExecConfigViolation("INPUT_SHAPE",
                        "固定无历史模式下首次模型调用必须恰好有一条用户消息，实际 " + userMessages + " 条");
            }
            if (!firstCall && userMessages == 0) {
                throw new ExecConfigViolation("INPUT_SHAPE", "本次调用没有任何用户消息");
            }
        }

        // -------------------------------------------------------------- 模型与选项面

        /**
         * 有效模型标识与选项：在**构造出来的模型**上校验。模型 key 归一化后比较 ——
         * 平台派发体可能是 {@code provider/model}，而清单声明的只是模型名本身。
         *
         * <p>{@code topK} 刻意不参与：适配器会写进 options，但 OpenAI-compatible 上游不消费它，
         * 断言它会变成假象。
         */
        public void verifyModelOptions(ChatOptions options) {
            if (!enabled) {
                return;
            }
            if (options == null) {
                throw new ExecConfigViolation("MODEL", "构造出来的模型没有携带有效选项");
            }
            String expected = manifest.modelKey();
            String actual = options.getModel();
            // 按**完整**标识比较：真正发给供应商的就是这个字符串，不许做任何有损归一化。
            // （此前会把最后一个 `/` 之前的内容截掉，于是 `evil/synthetic-model` 被当成
            //  `synthetic-model` 放行 —— 上游实际收到的是完整的不同模型名。）
            if (!Objects.equals(expected, actual)) {
                throw new ExecConfigViolation("MODEL",
                        "有效模型标识与清单不一致：期望 " + expected + "，实际 " + actual);
            }
            // 请求扩展：本片是固定配置模式，清单没有批准任何扩展字段。`extraBody` 会按原样
            // 合并进上行请求体，实测能覆盖已经查过的 `model` 与 `messages`（即绕过整条检查）。
            // 目前没有扩展字段需求，所以**拒绝任何非空 extraBody**，而不是只查 typed getter
            // 之后继续放行覆盖层。
            Object extraBody = invoke(options, "getExtraBody");
            if (extraBody instanceof java.util.Map<?, ?> map && !map.isEmpty()) {
                throw new ExecConfigViolation("EXTRA_BODY",
                        "固定配置模式下不接受请求扩展（extraBody），实际 " + map.size() + " 个键：" + map.keySet());
            }
            ExecManifest.ChatTuning want = manifest.tuning();
            ExecManifest.ChatTuning got = new ExecManifest.ChatTuning(options.getTemperature(),
                    options.getMaxTokens(), options.getTopP(), options.getFrequencyPenalty(),
                    options.getPresencePenalty(), seedOf(options), stopOf(options));
            if (!want.sameAs(got)) {
                throw new ExecConfigViolation("OPTIONS",
                        "有效模型选项与清单不一致：期望 " + want.normalized() + "，实际 " + got.normalized());
            }
            // endpoint：有效选项携带 baseUrl 时必须与清单一致。构造期已用派发体的 apiEndpoint 核过，
            // 但排在 ToolCallingAdvisor 之后的 advisor 仍能改写这个字段；实测「改写 baseUrl」不会
            // 把请求改道（模型自带 baseUrl 优先），所以这是纵深防御，不是已知绕过。
            Object baseUrl = invoke(options, "getBaseUrl");
            if (baseUrl != null && !manifest.endpoint().equals(String.valueOf(baseUrl))) {
                throw new ExecConfigViolation("ENDPOINT",
                        "有效选项的 baseUrl 与清单不一致：期望 " + manifest.endpoint() + "，实际 " + baseUrl);
            }
        }

        private static Integer seedOf(ChatOptions options) {
            Object seed = invoke(options, "getSeed");
            return seed instanceof Integer i ? i : null;
        }

        private static List<String> stopOf(ChatOptions options) {
            Object stop = invoke(options, "getStopSequences");
            if (stop instanceof List<?> list) {
                List<String> out = new ArrayList<>();
                for (Object s : list) {
                    out.add(String.valueOf(s));
                }
                return List.copyOf(out);
            }
            return List.of();
        }

        /**
         * 反射读可选访问器（本片只依赖 spring-ai 公共接口，具体实现类由清单之外的因素决定）。
         * 读不到就是 {@code null}；期望值来自同一个清单，两边一致即通过 —— 这里**不**声称
         * 读到了不存在的字段。
         */
        private static Object invoke(Object target, String method) {
            try {
                return target.getClass().getMethod(method).invoke(target);
            } catch (ReflectiveOperationException e) {
                return null;
            }
        }

        // -------------------------------------------------------------- 工具面

        /**
         * 有效工具定义三元组（name + description + schema）逐项比对。空集合**不是**合法期望，
         * 也不是拒绝手段：清单声明了受控工具，实际必须正好是它。
         */
        public void verifyTools(List<ToolCallback> callbacks) {
            if (!enabled) {
                return;
            }
            List<String> actual = new ArrayList<>();
            if (callbacks != null) {
                for (ToolCallback callback : callbacks) {
                    ToolDefinition definition = callback.getToolDefinition();
                    actual.add(ExecManifest.ToolDef.of(definition.name(), definition.description(),
                            definition.inputSchema()).normalized());
                }
            }
            actual.sort(String::compareTo);
            List<String> expected = new ArrayList<>();
            for (ExecManifest.ToolDef d : manifest.tools()) {
                expected.add(d.normalized());
            }
            expected.sort(String::compareTo);
            if (!expected.equals(actual)) {
                throw new ExecConfigViolation("TOOLS",
                        "有效工具定义与清单不一致：期望 " + expected + "，实际 " + actual);
            }
        }

    }
}
