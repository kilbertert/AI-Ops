import com.fasterxml.jackson.databind.JsonNode;
import com.trendpower.aiops.node.CpmsReadTool;
import com.trendpower.aiops.node.OperatorTools;

/**
 * 节点**实际装配出来的**工具定义，其 schema 必须与发布清单逐字节相同。
 *
 * <p>2026-11-01 生产事故：{@code CpmsReadTool} 对每个工具都回
 * {@code ControlledChargingStatusTool.SCHEMA}（空参数），而声明里 {@code cpms_site_search}
 * 有 name/address，于是 {@code verifyTools} 判漂移、整轮失败，站点问题 100% 不可用。
 *
 * <p>**判据必须读构造出来的 ToolCallback**，不能读 {@code OperatorTools.inputSchema()}：
 * 后者只是声明侧的字符串，构造器不接它的时候两者照样一致，检查就成了摆设。
 * （第一版就是这么写的，回退修复后仍然 PASS —— 与 `verifyTools` 同一条教训。）
 */
public final class ToolSchemaParity {
    /** 只为拿到 definition；本检查不发起任何调用。 */
    private static final CpmsReadTool.Authorization NO_CALLS = new CpmsReadTool.Authorization() {
        @Override public CpmsReadTool.Grant require(String runId, String manifestId, String tool, JsonNode args) {
            throw new IllegalStateException("schema parity must not authorize");
        }
        @Override public void record(String runId, String manifestId, String tool,
                                     CpmsReadTool.Grant grant, JsonNode evidence) {
            throw new IllegalStateException("schema parity must not record");
        }
    };

    public static void main(String[] args) throws Exception {
        int failures = 0;
        for (String pair : args) {
            int eq = pair.indexOf('=');
            String name = pair.substring(0, eq);
            String expected = pair.substring(eq + 1);
            CpmsReadTool tool = new CpmsReadTool(name, NO_CALLS);
            String schema = tool.getToolDefinition().inputSchema();
            String actual = sha256(schema);
            boolean ok = actual.equals(expected);
            System.out.println((ok ? "PASS " : "FAIL ") + name + " actual=" + actual + " expected=" + expected);
            if (!ok) failures++;
        }
        System.out.println("TOOL_SCHEMA_PARITY " + (failures == 0 ? "PASS" : "FAIL") + " assertions=" + args.length);
        if (failures > 0) System.exit(1);
    }

    private static String sha256(String value) throws Exception {
        byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(value.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        StringBuilder hex = new StringBuilder();
        for (byte b : digest) hex.append(String.format("%02x", b));
        return hex.toString();
    }
}
