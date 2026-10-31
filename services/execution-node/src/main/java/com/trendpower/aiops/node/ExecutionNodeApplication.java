package com.trendpower.aiops.node;

import com.aizuda.snail.ai.agent.starter.EnableSnailAiAgent;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * M2-CLOSE-01 的合成执行节点。
 *
 * 它只做两件事：作为 Snail 客户端接收分发，以及把唯一受控工具接到本环境的 Gateway。
 * shell、通用 HTTP、RAG、技能、动态 MCP 都**不在**可解析集合里 —— 这由
 * {@link GovernedToolRuntime} 覆盖默认 {@code ToolRuntime} 实现，而不是靠提示词。
 */
@EnableSnailAiAgent
@EnableScheduling
@SpringBootApplication
@ComponentScan({"com.aizuda.snail.ai.agent.example", "com.trendpower.aiops.node"})
public class ExecutionNodeApplication {

    public static void main(String[] args) {
        SpringApplication.run(ExecutionNodeApplication.class, args);
    }
}
