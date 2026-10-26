package com.trendpower.aiops.node;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.util.HashSet;

/** 节点只通过自己的凭据领取本次任务授权，不接受调度参数中的业务令牌。 */
@Component
final class CpmsGatewayAuthorization implements CpmsReadTool.Authorization {
    private final GatewayClient gateway;
    CpmsGatewayAuthorization(GatewayClient gateway) { this.gateway = gateway; }

    @Override public CpmsReadTool.Grant require(String run, String manifest, String tool) {
        JsonNode data = gateway.post(path(run, tool) + "/authorization", "{}", manifest);
        if (!run.equals(data.path("run_id").asText()) || !tool.equals(data.path("tool").asText())
                || !manifest.equals(data.path("manifest_id").asText()) || !data.path("site_ids").isArray()) {
            throw CpmsReadTool.refused("authorization_identity_mismatch");
        }
        var sites = new HashSet<String>();
        for (JsonNode site : data.path("site_ids")) {
            if (!site.isTextual() || !sites.add(site.asText())) throw CpmsReadTool.refused("invalid_authorization");
        }
        return new CpmsReadTool.Grant(data.path("authorization_id").asText(), data.path("tenant_id").asText(),
                sites, data.path("order_no").asText(), data.path("base_url").asText(),
                data.path("bearer").asText(), Instant.parse(data.path("expires_at").asText()));
    }

    @Override public void record(String run, String manifest, String tool,
                                 CpmsReadTool.Grant grant, JsonNode evidence) {
        var body = new ObjectMapper().createObjectNode();
        body.put("authorization_id", grant.authorizationId()).set("evidence", evidence);
        JsonNode ack = gateway.post(path(run, tool) + "/evidence", body.toString(), manifest);
        if (!run.equals(ack.path("run_id").asText()) || !tool.equals(ack.path("tool").asText())
                || !grant.authorizationId().equals(ack.path("authorization_id").asText())
                || !ack.path("accepted").isBoolean() || !ack.path("accepted").booleanValue()) {
            throw CpmsReadTool.refused("evidence_not_accepted");
        }
    }

    private static String path(String run, String tool) {
        if (!run.matches("run_[A-Za-z0-9_-]{43}") || !CpmsReadTool.NAMES.contains(tool)) {
            throw CpmsReadTool.refused("invalid_tool_identity");
        }
        return "/v1/internal/operator-runs/" + run + "/tools/" + tool;
    }
}
