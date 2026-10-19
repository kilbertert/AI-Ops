package com.trendpower.aiops.node.exec;

import com.aizuda.snail.ai.agent.core.executor.model.ChatModelFactory;
import com.aizuda.snail.ai.common.dto.agent.ChatDispatchRequest;
import com.aizuda.snail.ai.model.chat.ChatModelRuntime;
import com.aizuda.snail.ai.model.chat.ChatModelSpec;
import com.aizuda.snail.ai.agent.core.executor.model.ClientChatModelInput;
import com.aizuda.snail.ai.agent.core.executor.model.ClientChatModelSpecFactory;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Primary;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Objects;

/**
 * 接管真实节点的模型构造（框架的 {@code chatModelFactory} bean 是
 * {@code @ConditionalOnMissingBean}，所以这个 {@link Primary} 组件生效）。
 *
 * <p>两点，缺一不可：
 * <ol>
 *   <li><b>构造输入绑定</b>：启用清单时，先核对 {@code ModelConfig} 的 adapter/provider/model/endpoint
 *       与清单一致，再交给框架构造。这一步覆盖「provider 侧换端点/换模型」这类
 *       <b>Prompt 上看不到</b>的改动。</li>
 *   <li><b>最终调用边界</b>：把构造出来的 ChatModel 包一层 {@link GuardedChatModel}，
 *       真实消费对象（完整 system 集合、有效模型与选项、有效工具定义）在每次 call/stream 前校验。</li>
 * </ol>
 *
 * <p>未配置清单时走**与框架逐字相同**的两行，行为与今天完全一致（本片不改变 M2 对外契约）。
 */
@Component
@Primary
public class GuardedChatModelFactory implements ChatModelFactory {

    private final ClientChatModelSpecFactory specFactory;
    private final ChatModelRuntime runtime;
    private final ExecManifest manifest;

    /**
     * 真实 Spring 装配用的构造器。两个构造器并存时必须显式标注 {@link Autowired}，
     * 否则容器会去找不存在的默认构造器并启动失败。
     */
    @Autowired
    public GuardedChatModelFactory(ClientChatModelSpecFactory specFactory, ChatModelRuntime runtime) {
        this(specFactory, runtime, ExecManifest.loadFromEnv());
    }

    /**
     * 显式注入清单的接缝：真实 Spring 装配走上面的两参构造（清单从环境变量读），
     * 本片验收驱动在一个进程里跑「启用/未启用/错清单」多组场景，用这个构造换清单。
     * 两个构造走的是**同一套**校验与同一层守卫，不存在「测试专用守卫」。
     */
    public GuardedChatModelFactory(ClientChatModelSpecFactory specFactory, ChatModelRuntime runtime,
                                   ExecManifest manifest) {
        this.specFactory = specFactory;
        this.runtime = runtime;
        this.manifest = manifest;
    }

    /** 本进程是否启用守卫。未启用时行为与框架默认实现一致。 */
    public boolean enabled() {
        return manifest != null;
    }

    public ExecManifest manifest() {
        return manifest;
    }

    @Override
    public ChatModel build(ChatDispatchRequest.ModelConfig modelConfig) {
        if (manifest == null) {
            // 与 DefaultChatModelFactory.build 逐字相同：create(spec) → runtime.build(spec)。
            ChatModelSpec spec = specFactory.create(new ClientChatModelInput(modelConfig));
            return runtime.build(spec);
        }
        verifyModelConfig(modelConfig);
        ChatModelSpec spec = specFactory.create(new ClientChatModelInput(modelConfig));
        ChatModel model = runtime.build(spec);
        // 每次 run 一个新守卫：跨 run 不共享可变状态，交错的两个 run 各持自己的期望与计数。
        GuardedChatModel.ExecGuard guard = new GuardedChatModel.ExecGuard(manifest, true);
        // 模型标识与有效选项只能在**构造出来的模型**上看到（实测：Prompt 上 options 为 null）。
        guard.verifyModelOptions(model.getOptions());
        return new GuardedChatModel(model, guard);
    }

    /**
     * 在**发模型之前**核对本次 run 的模型配置。{@link com.trendpower.aiops.node.GovernedToolRuntime}
     * 在解析工具集合时调用它：这一步是同步的，所以失败发生在任何模型与业务调用之前
     * （计数天然为 0），而且发生在工具被交给模型之前 —— 不需要为拒绝路径再造一套
     * 「释放工具资源」的机制。
     */
    public void requireRunConfig(ChatDispatchRequest.ModelConfig modelConfig) {
        if (manifest == null) {
            return;
        }
        verifyModelConfig(modelConfig);
    }

    /**
     * 核对**解析出来的工具集合**：清单声明了受控工具，交付给模型的就必须正好是它。
     * 由 {@link com.trendpower.aiops.node.GovernedToolRuntime} 在自己交付工具的地方调用 ——
     * 校验的是真正交付出去的那个集合，而不是某个早期 DTO。
     */
    public void requireResolvedTools(List<org.springframework.ai.tool.ToolCallback> resolved) {
        if (manifest == null) {
            return;
        }
        new GuardedChatModel.ExecGuard(manifest, true).verifyTools(resolved);
    }

    private void verifyModelConfig(ChatDispatchRequest.ModelConfig config) {
        if (config == null) {
            throw new ExecConfigViolation("MODEL", "派发体没有模型配置，无法核对");
        }
        require("provider", manifest.providerKey(), config.getProviderKey());
        require("adapter", manifest.adapterKey(), config.getAdapterKey());
        require("model", manifest.modelKey(), config.getModelKey());
        require("endpoint", manifest.endpoint(), config.getApiEndpoint());
    }

    private static void require(String what, String expected, String actual) {
        if (!Objects.equals(expected, actual)) {
            throw new ExecConfigViolation("MODEL", what + " 与清单不一致：期望 " + expected + "，实际 " + actual);
        }
    }
}
