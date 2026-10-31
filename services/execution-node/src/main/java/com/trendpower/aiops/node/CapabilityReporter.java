package com.trendpower.aiops.node;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import com.trendpower.aiops.node.exec.ExecManifest;
import java.util.Map;

/** 发布前用实际运行节点的协议声明检查兼容性；不携带客户数据。 */
@Component
public final class CapabilityReporter {
    private final GatewayClient gateway;
    private static final ObjectMapper JSON = new ObjectMapper();

    public CapabilityReporter(GatewayClient gateway) { this.gateway = gateway; }

    @Scheduled(initialDelay = 1000, fixedDelay = 30000)
    public void report() {
        try {
            gateway.post("/v1/internal/execution-node/capabilities", JSON.writeValueAsString(Map.of(
                    "schemas", ExecManifest.SUPPORTED_SCHEMAS,
                    "tool_contract", CpmsReadTool.CONTRACT,
                    "tools_sha256", OperatorTools.declarationDigest())), null);
        } catch (Exception unavailable) {
            // Gateway先升级或尚不可达时等待下次报告，不影响现役运行。
        }
    }
}
