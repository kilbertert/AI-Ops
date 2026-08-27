package com.qushiyun.cloud.charging.pile.web.aspect;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.reflect.MethodSignature;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Component;
import org.springframework.web.bind.annotation.RequestHeader;

import java.lang.reflect.Parameter;
import java.util.ArrayList;
import java.util.List;

@Aspect
@Component
public class DiagQueryAuditAspect {

    private static final String INTERNAL_CALLER = "internal:AIOps";
    private static final Logger LOGGER = LoggerFactory.getLogger(DiagQueryAuditAspect.class);
    private final ObjectMapper objectMapper = new ObjectMapper();

    @Around("execution(public * com.qushiyun.cloud.charging.pile.web.controller.DiagQueryController.*(..))")
    public Object audit(ProceedingJoinPoint joinPoint) throws Throwable {
        long startedAt = System.currentTimeMillis();
        String endpoint = joinPoint.getSignature().toShortString();
        Object[] params = queryArgs(joinPoint);
        try {
            Object result = joinPoint.proceed();
            long elapsedMs = System.currentTimeMillis() - startedAt;
            LOGGER.info(
                    "diag query completed caller={} endpoint={} params={} rows={} elapsedMs={} result={}",
                    INTERNAL_CALLER,
                    endpoint,
                    summarize(params),
                    rowsOf(result),
                    elapsedMs,
                    outcome(result));
            return result;
        } catch (Throwable failure) {
            long elapsedMs = System.currentTimeMillis() - startedAt;
            LOGGER.warn(
                    "diag query failed caller={} endpoint={} params={} elapsedMs={} result={}",
                    INTERNAL_CALLER,
                    endpoint,
                    summarize(params),
                    elapsedMs,
                    "failure");
            throw failure;
        }
    }

    private Object[] queryArgs(ProceedingJoinPoint joinPoint) {
        MethodSignature signature = (MethodSignature) joinPoint.getSignature();
        Parameter[] parameters = signature.getMethod().getParameters();
        Object[] args = joinPoint.getArgs();
        List<Object> queryParams = new ArrayList<>();
        for (int index = 0; index < parameters.length; index++) {
            if (!parameters[index].isAnnotationPresent(RequestHeader.class)) {
                queryParams.add(args[index]);
            }
        }
        return queryParams.toArray();
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
            if (body.isArray()) {
                return body.size();
            }
            if (body.isObject()) {
                return body.size() > 0 ? 1 : 0;
            }
            return 0;
        } catch (Exception exception) {
            return -1;
        }
    }

    private String outcome(Object result) {
        if (result instanceof ResponseEntity<?> response && response.getStatusCode().isError()) {
            return "failure";
        }
        return "success";
    }
}
