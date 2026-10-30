package com.trendpower.aiops.node.exec;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermission;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Objects;
import java.util.Set;

/**
 * 受控部署产物预置的**执行配置清单**：期望值的唯一来源。
 *
 * <p>三条设计，都来自本片之前两轮实验的结论：
 * <ul>
 *   <li><b>身份 = 清单原始字节的 sha256</b>。不对解析后的对象做序列化再算摘要 —— 那会把
 *       JSON 归一化差异引入身份，让跨语言侧（Gateway / 节点）算出不同值。</li>
 *   <li><b>不启用就不存在</b>：没有配 {@code AIOPS_EXEC_MANIFEST} 时行为与今天逐字一致；
 *       配了但文件缺失/不可读/权限过宽/格式非法 ⇒ **启动即拒**，不退回未验证模式。</li>
 *   <li><b>无凭据</b>：清单里没有 apiKey / endpoint 之外的任何密钥；endpoint 只用于比对，
 *       不写进日志、回执或任何公开产物。</li>
 * </ul>
 */
public final class ExecManifest {

    /** 受控清单路径的环境变量。空 = 本片守卫未启用。 */
    public static final String PATH_ENV = "AIOPS_EXEC_MANIFEST";

    private static final String SUPPORTED_SCHEMA = "m3-exec-config/1";
    private static final ObjectMapper JSON = new ObjectMapper();

    private final String id;
    private final String schema;
    private final String name;
    private final String providerKey;
    private final String adapterKey;
    private final String modelKey;
    private final String endpoint;
    private final ChatTuning tuning;
    private final List<String> systemMessages;
    private final List<ToolDef> tools;
    private final String historyPolicy;
    private final boolean operatorOutput;

    private ExecManifest(String id, String schema, String name, String providerKey, String adapterKey,
                         String modelKey, String endpoint, ChatTuning tuning, List<String> systemMessages,
                         List<ToolDef> tools, String historyPolicy, boolean operatorOutput) {
        this.id = id;
        this.schema = schema;
        this.name = name;
        this.providerKey = providerKey;
        this.adapterKey = adapterKey;
        this.modelKey = modelKey;
        this.endpoint = endpoint;
        this.tuning = tuning;
        this.systemMessages = List.copyOf(systemMessages);
        this.tools = List.copyOf(tools);
        this.historyPolicy = historyPolicy;
        this.operatorOutput = operatorOutput;
    }

    /** 版本身份：清单**原始字节**的 sha256（十六进制，小写）。 */
    public String id() {
        return id;
    }

    /** 运行类型就是清单的 schema。工具面按它判，不按工具名猜。 */
    public String schema() {
        return schema;
    }

    public String name() {
        return name;
    }

    public String providerKey() {
        return providerKey;
    }

    public String adapterKey() {
        return adapterKey;
    }

    public String modelKey() {
        return modelKey;
    }

    public String endpoint() {
        return endpoint;
    }

    public ChatTuning tuning() {
        return tuning;
    }

    public List<String> systemMessages() {
        return systemMessages;
    }

    public List<ToolDef> tools() {
        return tools;
    }

    public String historyPolicy() {
        return historyPolicy;
    }

    public boolean operatorOutput() {
        return operatorOutput;
    }

    /**
     * 读清单。未配置返回 {@code null}（守卫不启用）；配置了但任何一项不合格 ⇒
     * {@link ExecConfigViolation}，**不降级**。
     *
     * <p>路径取自 {@code AIOPS_EXEC_MANIFEST}：JVM 系统属性优先，其次环境变量。生产用环境变量
     * （部署配置）；系统属性存在只是为了能在同一个 JVM 里驱动多组「启用/未启用/错清单」场景，
     * 让验收走**同一段真实装配**，而不是另造一个测试专用工厂。两者都不是放宽：能设置其中
     * 任何一个的进程本来就已在本进程内执行代码。
     */
    public static ExecManifest loadFromEnv() {
        String configured = System.getProperty(PATH_ENV);
        if (configured == null || configured.isBlank()) {
            configured = System.getenv(PATH_ENV);
        }
        if (configured == null || configured.isBlank()) {
            return null;
        }
        return load(Path.of(configured.trim()));
    }

    public static ExecManifest load(Path path) {
        Path declared = path.toAbsolutePath();
        if (Files.isSymbolicLink(declared)) {
            throw new ExecConfigViolation("MANIFEST", "清单路径是符号链接，拒绝：" + declared);
        }
        Path real = realPath(declared);
        byte[] raw;
        try {
            requirePrivateFile(real);
            raw = Files.readAllBytes(real);
        } catch (IOException e) {
            throw new ExecConfigViolation("MANIFEST", "清单不可读：" + real + " (" + e.getMessage() + ")");
        }
        if (raw.length == 0) {
            throw new ExecConfigViolation("MANIFEST", "清单为空：" + real);
        }
        JsonNode root;
        try {
            root = JSON.readTree(raw);
        } catch (IOException e) {
            throw new ExecConfigViolation("MANIFEST", "清单不是合法 JSON：" + e.getMessage());
        }
        return parse(root, sha256(raw), real);
    }

    /** 可信 Gateway 发来的冻结清单；身份仍按原始 UTF-8 字节计算。 */
    public static ExecManifest fromText(String text, String expected) {
        byte[] raw = text.getBytes(java.nio.charset.StandardCharsets.UTF_8);
        if (raw.length == 0 || raw.length > 262144 || !sha256(raw).equals(expected)) {
            throw new ExecConfigViolation("MANIFEST", "发布清单摘要不一致");
        }
        try {
            return parse(JSON.readTree(raw), expected, null);
        } catch (IOException e) {
            throw new ExecConfigViolation("MANIFEST", "发布清单不可解析");
        }
    }

    private static Path realPath(Path declared) {
        try {
            return declared.toRealPath(LinkOption.NOFOLLOW_LINKS);
        } catch (IOException e) {
            throw new ExecConfigViolation("MANIFEST", "清单路径不可解析：" + declared
                    + " (" + e.getMessage() + ")");
        }
    }

    private static void requirePrivateFile(Path real) throws IOException {
        if (!Files.isRegularFile(real, LinkOption.NOFOLLOW_LINKS)) {
            throw new ExecConfigViolation("MANIFEST", "清单不是普通文件：" + real);
        }
        PosixFilePermission[] wide = {
                PosixFilePermission.GROUP_WRITE, PosixFilePermission.GROUP_EXECUTE,
                PosixFilePermission.OTHERS_WRITE, PosixFilePermission.OTHERS_EXECUTE,
                PosixFilePermission.OTHERS_READ};
        try {
            Set<PosixFilePermission> perms = Files.getPosixFilePermissions(real, LinkOption.NOFOLLOW_LINKS);
            for (PosixFilePermission p : wide) {
                if (perms.contains(p)) {
                    throw new ExecConfigViolation("MANIFEST",
                            "清单权限过宽（" + p + "），拒绝：" + real);
                }
            }
        } catch (UnsupportedOperationException e) {
            // 非 POSIX 文件系统上不声称做过这项检查 —— 但也不静默放行：调用方要看到它。
            throw new ExecConfigViolation("MANIFEST", "无法校验清单权限（非 POSIX 文件系统）：" + real);
        }
    }

    private static ExecManifest parse(JsonNode root, String id, Path real) {
        String schema = text(root, "schema");
        boolean conversation = "operator-conversation/1".equals(schema);
        if (!SUPPORTED_SCHEMA.equals(schema) && !"operator-exec-config/1".equals(schema) && !conversation) {
            throw new ExecConfigViolation("MANIFEST",
                    "不支持的 schema（" + schema + "），只接受 " + SUPPORTED_SCHEMA);
        }
        String history = text(root, "history_policy");
        // M2 是固定无历史模式。取值不被支持时**拒绝清单**，而不是忽略这个字段继续跑。
        if (!"none".equals(history)) {
            throw new ExecConfigViolation("MANIFEST", "不支持的 history_policy：" + history);
        }
        JsonNode model = required(root, "model");
        JsonNode options = required(root, "model_options");
        JsonNode sys = required(root, "system_messages");
        if (!sys.isArray() || sys.isEmpty()) {
            throw new ExecConfigViolation("MANIFEST", "system_messages 必须是非空数组");
        }
        List<String> system = new ArrayList<>();
        for (JsonNode m : sys) {
            system.add(m.asText());
        }
        List<ToolDef> tools = new ArrayList<>();
        JsonNode toolsNode = root.get("tools");
        if (("operator-exec-config/1".equals(schema) || conversation) && (toolsNode == null || !toolsNode.isArray())) {
            throw new ExecConfigViolation("MANIFEST", "管家工具清单必须显式声明数组");
        }
        if (toolsNode != null && !toolsNode.isNull()) {
            if (!toolsNode.isArray()) {
                throw new ExecConfigViolation("MANIFEST", "tools 必须是数组");
            }
            for (JsonNode t : toolsNode) {
                tools.add(ToolDef.of(text(t, "name"), text(t, "description"), text(t, "input_schema")));
            }
        }
        // 无订单回合**可以**带工具（ADR-0183），但只能是声明允许这类运行用的那些。
        // 这里与 `GovernedToolRuntime.resolvedTools` 用同一条规则，判据只有一份。
        for (ToolDef tool : tools) {
            if (!ToolSurface.allows(schema, tool.name())) {
                throw new ExecConfigViolation("TOOLS", "该运行类型不允许这个工具：" + tool.name());
            }
        }
        if (tools.isEmpty() && !"operator-exec-config/1".equals(schema) && !conversation) {
            // 本片固定「唯一受控工具」。空工具集合**不是**拒绝手段，也不是合法期望值。
            throw new ExecConfigViolation("MANIFEST", "tools 不得为空：本片要求显式声明受控工具");
        }
        return new ExecManifest(id, schema, text(root, "name"), text(model, "provider_key"),
                text(model, "adapter_key"), text(model, "model_key"), text(model, "endpoint"),
                ChatTuning.of(options), system, tools, history, "operator-exec-config/1".equals(schema) || conversation);
    }

    private static JsonNode required(JsonNode parent, String field) {
        JsonNode node = parent.get(field);
        if (node == null || node.isNull()) {
            throw new ExecConfigViolation("MANIFEST", "清单缺少字段：" + field);
        }
        return node;
    }

    private static String text(JsonNode parent, String field) {
        String value = required(parent, field).asText(null);
        if (value == null || value.isBlank()) {
            throw new ExecConfigViolation("MANIFEST", "清单字段为空：" + field);
        }
        return value;
    }

    static String sha256(byte[] raw) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }

    static String bytesDigest(String value) {
        return value == null ? null : sha256(value.getBytes(StandardCharsets.UTF_8));
    }

    /** 模型侧的可比量。只有对**实际构造出的模型**也读得到的项才在这里。 */
    public record ChatTuning(Double temperature, Integer maxTokens, Double topP, Double frequencyPenalty,
                             Double presencePenalty, Integer seed, List<String> stopSequences) {

        static ChatTuning of(JsonNode options) {
            return new ChatTuning(dbl(options, "temperature"), integer(options, "max_tokens"),
                    dbl(options, "top_p"), dbl(options, "frequency_penalty"),
                    dbl(options, "presence_penalty"), integer(options, "seed"),
                    strings(options, "stop_sequences"));
        }

        private static Double dbl(JsonNode node, String field) {
            JsonNode v = node.get(field);
            return v == null || v.isNull() ? null : v.asDouble();
        }

        private static Integer integer(JsonNode node, String field) {
            JsonNode v = node.get(field);
            return v == null || v.isNull() ? null : v.asInt();
        }

        private static List<String> strings(JsonNode node, String field) {
            JsonNode v = node.get(field);
            if (v == null || v.isNull()) {
                return List.of();
            }
            List<String> out = new ArrayList<>();
            for (JsonNode s : v) {
                out.add(s.asText());
            }
            return List.copyOf(out);
        }

        /** 归一化成可读串，**只用于日志**；相等判定一律走 {@link #sameAs}。 */
        public String normalized() {
            return "temperature=" + temperature
                    + ",max_tokens=" + maxTokens
                    + ",top_p=" + topP
                    + ",frequency_penalty=" + frequencyPenalty
                    + ",presence_penalty=" + presencePenalty
                    + ",seed=" + seed
                    + ",stop=" + stopSequences;
        }

        /**
         * 逐字段比较：**数值按数值比，不先格式化成字符串**。
         *
         * <p>此前把两侧都格式化成六位小数再比字符串，于是 `0.2` 与 `0.2000001` 的差别被
         * 抹掉（四舍五入到 6 位后相同），上游收到的是后者。`0.2` 与 `0.20` 是同一个浮点值，
         * 按 {@code Double.equals} 自然等价，不需要格式化来达成。
         */
        public boolean sameAs(ChatTuning other) {
            return Objects.equals(temperature, other.temperature)
                    && Objects.equals(maxTokens, other.maxTokens)
                    && Objects.equals(topP, other.topP)
                    && Objects.equals(frequencyPenalty, other.frequencyPenalty)
                    && Objects.equals(presencePenalty, other.presencePenalty)
                    && Objects.equals(seed, other.seed)
                    && Objects.equals(stopSequences, other.stopSequences);
        }
    }

    /** 工具的有效定义三元组：同名不同描述/不同 schema 也算漂移。 */
    public record ToolDef(String name, String description, String inputSchema, String digest) {

        static ToolDef of(String name, String description, String inputSchema) {
            return new ToolDef(name, description, inputSchema, bytesDigest(inputSchema));
        }

        public String normalized() {
            return name + "/d=" + description + "/s=" + digest;
        }
    }
}
