package com.trendpower.aiops.node;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 受控工具声明 —— 与网关侧 {@code operator_tools.json} 是**同一份文件**。
 *
 * <p>它替代了原先写在这个包里的两张常量表（工具名集合、按工具名分派的 if/else）。加一个
 * 工具只要改那份声明；只要它的响应形状落在 {@code single} 或 {@code page} 里，本目录下
 * 一行都不用动。需要**新的**响应形状时改的是这里的分派，不是每个工具各写一遍 ——
 * 那与「每加一个工具就改一次 Java」是两件事。
 *
 * <p>读取位置：系统属性 {@code aiops.node.tools-file}（部署时随节点配置一起放），否则用
 * classpath 上的同名资源。**读不到就是启动失败**：一份读不到的声明如果被当成「没有工具」，
 * 故障会以「清单里没有那个工具」的形状出现在运营页面上，而不是出现在日志里。
 */
public final class OperatorTools {

    public static final String PATH_PROPERTY = "aiops.node.tools-file";
    public static final String DECLARATION = "operator_tools.json";
    public static final String SCHEMA = "aiops-operator-tools/1";
    public static final List<String> KINDS = List.of("single", "page");

    /** 一个工具的声明视图。Java 只关心装配需要的部分。 */
    record Entry(String name, String kind, String description, String source,
                 List<String> evidenceFields, List<String> appliesTo,
                 Map<String, com.fasterxml.jackson.databind.JsonNode> flags) {
    }

    private static volatile Map<String, Entry> loaded;
    private static String loadedDigest;

    private OperatorTools() {
    }

    public static Map<String, Entry> entries() {
        Map<String, Entry> current = loaded;
        if (current == null) {
            synchronized (OperatorTools.class) {
                current = loaded;
                if (current == null) {
                    String raw = read();
                    current = parse(raw);
                    loadedDigest = digest(raw);
                    loaded = current;
                }
            }
        }
        return current;
    }

    /**
     * 声明**不可用**时返回空表，而不是抛错。
     *
     * <p>只有 C 端运行可以这样退化：它的清单里本来就只有状态工具，CPMS 工具是「挂了声明
     * 才有」的增量。订单诊断与无订单回合必须拿到声明——它们的清单里就有 CPMS 工具，
     * 读不到声明是配置错误，不能当成「没有工具」。
     */
    public static List<String> namesIfAvailable() {
        try {
            return names();
        } catch (ExecToolViolation unavailable) {
            return List.of();
        }
    }

    public static List<String> names() {
        return List.copyOf(entries().keySet());
    }

    public static String declarationDigest() {
        entries();
        return loadedDigest;
    }

    private static String digest(String raw) {
        try {
            return java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256")
                    .digest(raw.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException impossible) {
            throw new IllegalStateException(impossible);
        }
    }

    public static boolean declares(String name) {
        return entries().containsKey(name);
    }

    public static String description(String name) {
        Entry entry = entries().get(name);
        if (entry == null) throw new ExecToolViolation("声明里没有这个工具: " + name);
        return entry.description();
    }

    public static String source(String name) {
        Entry entry = entries().get(name);
        if (entry == null) throw new ExecToolViolation("声明里没有这个工具: " + name);
        return entry.source();
    }

    public static List<String> appliesTo(String name) {
        Entry entry = entries().get(name);
        if (entry == null) throw new ExecToolViolation("声明里没有这个工具: " + name);
        return entry.appliesTo();
    }

    public static List<String> evidenceFields(String name) {
        Entry entry = entries().get(name);
        if (entry == null) throw new ExecToolViolation("声明里没有这个工具: " + name);
        return entry.evidenceFields();
    }

    private static JsonNode flag(String name, String key) {
        Entry entry = entries().get(name);
        if (entry == null) throw new ExecToolViolation("声明里没有这个工具: " + name);
        JsonNode value = entry.flags().get(key);
        if (value == null) throw new ExecToolViolation("声明缺少标记: " + name + "." + key);
        return value;
    }

    public static long flagInt(String name, String key) {
        return flag(name, key).asLong();
    }

    public static boolean flagBool(String name, String key) {
        return flag(name, key).asBoolean();
    }

    public static String flagText(String name, String key) {
        return flag(name, key).asText();
    }

    public static String kind(String name) {
        Entry entry = entries().get(name);
        if (entry == null) throw new ExecToolViolation("声明里没有这个工具: " + name);
        return entry.kind();
    }

    private static String read() {
        String configured = System.getProperty(PATH_PROPERTY);
        try {
            if (configured != null && !configured.isBlank()) {
                return Files.readString(Path.of(configured));
            }
            try (InputStream stream = OperatorTools.class.getResourceAsStream("/" + DECLARATION)) {
                if (stream == null) {
                    throw new ExecToolViolation("找不到工具声明: " + DECLARATION
                            + "（用 -D" + PATH_PROPERTY + " 指定文件，或把它放进 classpath）");
                }
                return new String(stream.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
            }
        } catch (IOException failure) {
            throw new ExecToolViolation("工具声明不可读");
        }
    }

    private static Map<String, Entry> parse(String raw) {
        JsonNode body;
        try {
            body = new ObjectMapper().readTree(raw);
        } catch (IOException failure) {
            throw new ExecToolViolation("工具声明不是合法 JSON");
        }
        if (body == null || !body.isObject() || !SCHEMA.equals(body.path("schema").asText())) {
            throw new ExecToolViolation("工具声明的 schema 不受支持");
        }
        JsonNode tools = body.path("tools");
        if (!tools.isArray() || tools.isEmpty()) {
            throw new ExecToolViolation("工具声明里没有任何工具");
        }
        Map<String, Entry> result = new LinkedHashMap<>();
        for (JsonNode tool : tools) {
            String name = tool.path("name").asText("");
            String kind = tool.path("response").path("kind").asText("");
            String method = tool.path("request").path("method").asText("");
            if (name.isBlank() || !name.matches("[a-z][a-z0-9_]{2,63}")) {
                throw new ExecToolViolation("工具名不合法: " + name);
            }
            if (!KINDS.contains(kind)) throw new ExecToolViolation("未知的响应形状: " + kind);
            // 只读面只允许 GET。这不是风格：写不在第一版的边界里。
            if (!"GET".equals(method)) throw new ExecToolViolation("只允许 GET: " + name);
            String description = tool.path("description").asText("");
            String source = tool.path("source").asText("");
            if (description.isBlank() || source.isBlank()) {
                throw new ExecToolViolation("工具声明缺描述或来源: " + name);
            }
            JsonNode declaredFields = tool.path("evidence_fields");
            if (!declaredFields.isArray() || declaredFields.isEmpty()) {
                throw new ExecToolViolation("工具声明缺 evidence_fields: " + name);
            }
            List<String> fields = new java.util.ArrayList<>();
            for (JsonNode field : declaredFields) {
                if (!field.isTextual() || field.asText().isBlank()) {
                    throw new ExecToolViolation("证据字段不合法: " + name);
                }
                fields.add(field.asText());
            }
            JsonNode declaredContexts = tool.path("applies_to");
            if (!declaredContexts.isArray() || declaredContexts.isEmpty()) {
                throw new ExecToolViolation("工具声明缺 applies_to: " + name);
            }
            List<String> contexts = new java.util.ArrayList<>();
            for (JsonNode context : declaredContexts) {
                if (!context.isTextual()) throw new ExecToolViolation("applies_to 不合法: " + name);
                contexts.add(context.asText());
            }
            Map<String, com.fasterxml.jackson.databind.JsonNode> flags = new LinkedHashMap<>();
            tool.path("response").path("flags").fields()
                    .forEachRemaining(f -> flags.put(f.getKey(), f.getValue()));
            if (result.put(name, new Entry(name, kind, description, source,
                    List.copyOf(fields), List.copyOf(contexts), Map.copyOf(flags))) != null) {
                throw new ExecToolViolation("重复的工具: " + name);
            }
        }
        return Map.copyOf(result);
    }

    /** 声明不合格或引用了未声明的工具。调用方把它变成整轮失败，不降级。 */
    static final class ExecToolViolation extends RuntimeException {
        ExecToolViolation(String message) {
            super(message);
        }
    }
}
