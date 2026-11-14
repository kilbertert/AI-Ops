package com.trendpower.aiops;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.Signature;
import java.time.Clock;
import java.util.Base64;
import java.util.HexFormat;
import java.util.List;
import java.util.UUID;

/** 公司受管网关完成本环境会话核验、最终路径/头改写后调用；不提供用户认证旁路。 */
public final class GatewayAssertion {
    private GatewayAssertion() {}

    public static String sign(PrivateKey key, String kid, String issuer, String environment,
            String method, String rawPath, String rawQuery, byte[] body,
            String authorization, String thirdSession, String entry, String sourceKey, String contentType, Clock clock)
            throws GeneralSecurityException {
        if (!kid.matches("[a-z0-9-]{1,64}") || !method.matches("[A-Z]+")
                || !rawPath.startsWith("/") || rawPath.contains("?")) {
            throw new IllegalArgumentException("invalid gateway assertion input");
        }
        String request = "[" + String.join(",", List.of(
                json(method), json(rawPath), json(rawQuery), json(hash(body)),
                json(authorization), json(thirdSession), json(entry), json(sourceKey), json(contentType))) + "]";
        long now = clock.instant().getEpochSecond();
        String header = "{\"alg\":\"RS256\",\"typ\":\"aiops-gateway+jwt\",\"kid\":" + json(kid) + "}";
        String claims = "{\"iss\":" + json(issuer) + ",\"aud\":\"aiops-gateway\",\"environment\":"
                + json(environment) + ",\"iat\":" + now + ",\"exp\":" + (now + 120)
                + ",\"jti\":" + json(UUID.randomUUID().toString().replace("-", ""))
                + ",\"request_sha256\":" + json(hash(request.getBytes(StandardCharsets.UTF_8))) + "}";
        String content = encode(header.getBytes(StandardCharsets.UTF_8)) + "."
                + encode(claims.getBytes(StandardCharsets.UTF_8));
        Signature signature = Signature.getInstance("SHA256withRSA");
        signature.initSign(key);
        signature.update(content.getBytes(StandardCharsets.US_ASCII));
        return content + "." + encode(signature.sign());
    }

    private static String hash(byte[] value) throws GeneralSecurityException {
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value));
    }

    private static String encode(byte[] value) {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(value);
    }

    private static String json(String value) {
        if (value == null) value = "";
        StringBuilder out = new StringBuilder("\"");
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\b' -> out.append("\\b");
                case '\f' -> out.append("\\f");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                default -> {
                    if (c < 32 || c >= 127) out.append(String.format("\\u%04x", (int) c));
                    else out.append(c);
                }
            }
        }
        return out.append('"').toString();
    }
}
