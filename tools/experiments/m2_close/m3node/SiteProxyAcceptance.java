package com.trendpower.aiops.node;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.ai.chat.model.ToolContext;
import java.util.Map;

/** 实际 Java 授权/取数/证据回调；输入只来自隔离验证器。 */
public final class SiteProxyAcceptance {
    public static void main(String[] args) throws Exception {
        var json = new ObjectMapper();
        var input = json.readTree(System.in);
        var gateway = new GatewayClient(input.path("gateway").asText(),
                input.path("token").asText(), "site-proxy-test", 10);
        String run = input.path("run").asText();
        String manifest = input.path("manifest").asText();
        try {
            var tool = new CpmsReadTool("cpms_site_search", new CpmsGatewayAuthorization(gateway));
            var context = new ToolContext(Map.of(GovernedToolRuntime.RUN_ID_KEY, run,
                    "aiops.manifest_id", manifest));
            System.out.println(tool.call(input.path("arguments").toString(), context));
        } catch (RuntimeException failure) {
            System.err.println(failure.getClass().getSimpleName() + ": " + failure.getMessage());
            System.exit(1);
        }
    }
}
