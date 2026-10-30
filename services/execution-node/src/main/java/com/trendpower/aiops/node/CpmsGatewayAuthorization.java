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
        // 声明契约版本：旧 Gateway 不认识这个头，会回一份没有 `request` 的载荷，那样在
        // 下面被拒 —— 两个方向都落在可预期的位置，而不是等完成回调里缺字段。
        JsonNode data = gateway.post(
                path(run, tool) + "/authorization", "{}", manifest, CpmsReadTool.CONTRACT);
        if (!run.equals(data.path("run_id").asText()) || !tool.equals(data.path("tool").asText())
                || !manifest.equals(data.path("manifest_id").asText()) || !data.path("site_ids").isArray()) {
            throw CpmsReadTool.refused("authorization_identity_mismatch");
        }
        if (!CpmsReadTool.CONTRACT.equals(data.path("contract").asText())) {
            throw CpmsReadTool.refused("authorization_contract_mismatch");
        }
        var sites = new HashSet<String>();
        for (JsonNode site : data.path("site_ids")) {
            if (!site.isTextual() || !sites.add(site.asText())) throw CpmsReadTool.refused("invalid_authorization");
        }
        JsonNode request = data.path("request");
        return new CpmsReadTool.Grant(data.path("authorization_id").asText(), data.path("tenant_id").asText(),
                sites, data.path("inputs"),
                new CpmsReadTool.Request(request.path("method").asText(), request.path("path").asText(),
                        request.path("query").asText()),
                data.path("base_url").asText(),
                data.path("bearer").asText(), Instant.parse(data.path("expires_at").asText()));
    }

    @Override public void record(String run, String manifest, String tool,
                                 CpmsReadTool.Grant grant, JsonNode evidence) {
        var body = new ObjectMapper().createObjectNode();
        body.put("authorization_id", grant.authorizationId()).set("evidence", evidence);
        JsonNode ack = gateway.post(
                path(run, tool) + "/evidence", body.toString(), manifest, CpmsReadTool.CONTRACT);
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
