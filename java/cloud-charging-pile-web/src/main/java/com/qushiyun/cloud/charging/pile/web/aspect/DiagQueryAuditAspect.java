package com.qushiyun.cloud.charging.pile.web.aspect;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Aspect;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Component;

import java.util.Arrays;

@Aspect
@Component
public class DiagQueryAuditAspect {

    private static final String INTERNAL_CALLER = "internal:AIOps";
    private static final Logger LOGGER = LoggerFactory.getLogger(DiagQueryAuditAspect.class);
    private final ObjectMapper objectMapper = new ObjectMapper();

    @Around("execution(public * com.qushiyun.cloud.charging.pile.web.controller.DiagQueryController.*(..))")
    public Object audit(ProceedingJoinPoint joinPoint) throws Throwable {
        long startedAt = System.currentTimeMillis();
        try {
            Object result = joinPoint.proceed();
            long elapsedMs = System.currentTimeMillis() - startedAt;
            LOGGER.info(
                    "diag query completed caller={} endpoint={} params={} rows={} elapsedMs={} result={}",
                    INTERNAL_CALLER,
                    joinPoint.getSignature().toShortString(),
                    summarize(queryArgs(joinPoint.getArgs())),
                    rowsOf(result),
                    elapsedMs,
                    "success");
            return result;
        } catch (Throwable failure) {
            long elapsedMs = System.currentTimeMillis() - startedAt;
            LOGGER.warn(
                    "diag query failed caller={} endpoint={} params={} elapsedMs={} result={}",
                    INTERNAL_CALLER,
                    joinPoint.getSignature().toShortString(),
                    summarize(queryArgs(joinPoint.getArgs())),
                    elapsedMs,
                    "failure");
            throw failure;
        }
    }

    private Object[] queryArgs(Object[] args) {
        if (args == null || args.length <= 2) {
            return new Object[0];
        }
        return Arrays.copyOfRange(args, 2, args.length);
    }

    private String summarize(Object[] args) {
        if (args == null) {
            return "[]";
        }
        StringBuilder builder = new StringBuilder("[");
        for (int index = 0; index < args.length; index++) {
            if (index > 0) {
                builder.append(", ");
            }
            builder.append(shortName(args[index]));
        }
        return builder.append("]").toString();
    }

    private String shortName(Object value) {
        if (value == null) {
            return "null";
        }
        String text = value.toString();
        return text.length() <= 128 ? text : text.substring(0, 128);
    }

    private int rowsOf(Object result) {
        if (!(result instanceof ResponseEntity<?> response)) {
            return -1;
        }
        if (!(response.getBody() instanceof com.qushiyun.cloud.common.core.util.R<?> envelope)) {
            return -1;
        }
        try {
            JsonNode body = objectMapper.valueToTree(envelope.getData());
            JsonNode orders = body.path("orders");
            if (orders.isArray()) {
                return orders.size();
            }
            return body.isArray() ? body.size() : 0;
        } catch (Exception exception) {
            return -1;
        }
    }
}
