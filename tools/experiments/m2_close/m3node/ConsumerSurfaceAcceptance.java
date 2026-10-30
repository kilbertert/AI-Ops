package com.trendpower.aiops.node;

import com.trendpower.aiops.node.exec.ExecConfigViolation;
import com.trendpower.aiops.node.exec.ExecManifest;
import com.trendpower.aiops.node.exec.ToolSurface;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.List;

/**
 * 现有 C 端部署**没有挂工具声明**。它们必须照旧能起、能解析清单。
 *
 * <p>这一条是 code review 抓到的回归：工具面改成按运行类型判之后，清单解析会无条件读
 * 声明，于是「清单里只有状态工具」的 C 端节点会因为一个它用不上的文件而失败。
 *
 * <p>断言的是**两个方向**：C 端在没有声明时退化成只有状态工具（旧行为，不是失败），
 * 而订单诊断/无订单回合在没有声明时**必须**失败 —— 它们的清单里就有 CPMS 工具，
 * 读不到声明是配置错误，不能当成「没有工具」。
 *
 * <p>必须以**不带** {@code -Daiops.node.tools-file} 的方式运行。
 */
public final class ConsumerSurfaceAcceptance {

    static int passed;

    public static void main(String[] args) throws Exception {
        if (System.getProperty(OperatorTools.PATH_PROPERTY) != null) {
            throw new AssertionError("本验收必须在**没有**工具声明的进程里跑");
        }
        List<String> consumer = ToolSurface.allowed(ToolSurface.CONSUMER);
        check(consumer.equals(List.of(ControlledChargingStatusTool.TOOL_NAME)),
                "C 端在没有声明时只剩状态工具，而不是启动失败");

        String manifest = "{\"schema\":\"m3-exec-config/1\",\"name\":\"n\",\"history_policy\":\"none\","
                + "\"model\":{\"provider_key\":\"p\",\"adapter_key\":\"a\",\"model_key\":\"m\","
                + "\"endpoint\":\"https://e\"},\"model_options\":{},\"system_messages\":[\"S\"],"
                + "\"tools\":[{\"name\":\"charging_status_snapshot\",\"description\":\"d\","
                + "\"input_schema\":\"{}\"}]}";
        var parsed = ExecManifest.fromText(manifest, sha256(manifest));
        check(parsed.tools().size() == 1, "没有声明时 C 端清单照旧可解析");

        // 反方向：operator 的两类运行必须拿到声明。
        for (String schema : new String[]{ToolSurface.OPERATOR_ORDER, ToolSurface.OPERATOR_CONVERSATION}) {
            try {
                ToolSurface.allowed(schema);
                throw new AssertionError("没有声明时 " + schema + " 必须失败");
            } catch (OperatorTools.ExecToolViolation expected) {
                check(expected.getMessage().contains(OperatorTools.PATH_PROPERTY),
                        "失败信息要指出怎么修：" + schema);
            }
        }
        System.out.println("CONSUMER_SURFACE_ACCEPTANCE PASS assertions=" + passed);
    }

    static String sha256(String text) throws Exception {
        byte[] bytes = MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
        var hex = new StringBuilder();
        for (byte value : bytes) hex.append(String.format("%02x", value));
        return hex.toString();
    }

    static void check(boolean value, String label) {
        if (!value) throw new AssertionError(label);
        passed++;
    }
}
