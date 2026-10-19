package com.trendpower.aiops.node.exec;

/**
 * 执行配置守卫的显式失败。**不是**返回替身、**不是**返回空工具集合 —— 契约要求漂移必须
 * 让这次调用失败，并以「模型调用 0 次 + 业务调用 0 次」证明它没有到达上游。
 *
 * <p>{@code kind} 只用于诊断分类（MODEL / SYSTEM_PROMPT / TOOLS / OPTIONS / INPUT_SHAPE /
 * MANIFEST），不含凭据、不含业务数据，可以出现在日志与回执里。
 */
public class ExecConfigViolation extends RuntimeException {

    private final String kind;

    public ExecConfigViolation(String kind, String detail) {
        super(kind + ": " + detail);
        this.kind = kind;
    }

    public String kind() {
        return kind;
    }
}
