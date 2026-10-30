package com.trendpower.aiops.node.exec;

import com.trendpower.aiops.node.ControlledChargingStatusTool;
import com.trendpower.aiops.node.OperatorTools;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * 这一次运行允许用哪些工具 —— 判据只有一份。
 *
 * <p>这里替代了原先按工具名硬编码的互斥（「状态工具不能与管家 CPMS 工具混用」）。那条互斥
 * 表达的意思其实是对的，只是写成了名字之间的特例；写成特例之后，每加一个工具都要重新想
 * 一遍「它和谁不能共存」。现在问的是另一个问题：**这一类运行允许哪些工具**。
 *
 * <ul>
 *   <li>订单诊断（{@code operator-exec-config/1}）：声明里 {@code applies_to} 含 {@code order}
 *       的 CPMS 工具。状态工具不在这里 —— 它的回答协议与诊断协议不兼容。</li>
 *   <li>无订单回合（{@code operator-conversation/1}）：含 {@code conversation} 的 CPMS 工具。</li>
 *   <li>C 端（{@code m3-exec-config/1}）：状态工具，加上含 {@code conversation} 的 CPMS 工具。
 *       这就是本片要的形状 —— 订单状态与站点查询同清单共存。</li>
 * </ul>
 *
 * <p>拒绝判据没有放松：不在允许集合里的工具名整轮失败，只是判据从常量变成了数据。
 */
public final class ToolSurface {

    public static final String OPERATOR_ORDER = "operator-exec-config/1";
    public static final String OPERATOR_CONVERSATION = "operator-conversation/1";
    public static final String CONSUMER = "m3-exec-config/1";

    private ToolSurface() {
    }

    /** 这一类运行允许的工具名，顺序稳定（声明顺序）。 */
    public static List<String> allowed(String schema) {
        Set<String> names = new LinkedHashSet<>();
        String context = context(schema);
        // C 端**不要求**声明在场：它的清单本来只有状态工具，CPMS 工具是挂了声明才有的增量。
        // 现有 C 端部署没有挂声明，让它们在启动时因为一个用不上的文件而失败是错的。
        // 订单诊断与无订单回合必须拿到声明 —— 它们的清单里就有 CPMS 工具。
        List<String> declared = CONSUMER.equals(schema)
                ? OperatorTools.namesIfAvailable()
                : OperatorTools.names();
        for (String name : declared) {
            if (OperatorTools.appliesTo(name).contains(context)) names.add(name);
        }
        // C 端还要状态工具：它是这一端唯一的订单事实入口。
        if (CONSUMER.equals(schema)) names.add(ControlledChargingStatusTool.TOOL_NAME);
        return new ArrayList<>(names);
    }

    public static boolean allows(String schema, String tool) {
        return allowed(schema).contains(tool);
    }

    private static String context(String schema) {
        if (OPERATOR_ORDER.equals(schema)) return "order";
        if (OPERATOR_CONVERSATION.equals(schema) || CONSUMER.equals(schema)) return "conversation";
        throw new ExecConfigViolation("MANIFEST", "未知的运行类型：" + schema);
    }
}
