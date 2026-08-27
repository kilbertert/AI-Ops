package com.qushiyun.cloud.charging.pile.web.controller;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.qushiyun.cloud.common.auth.component.InternalTokenManager;
import com.qushiyun.cloud.common.core.util.R;
import org.springframework.data.domain.Range;
import org.springframework.data.redis.connection.DataType;
import org.springframework.data.redis.connection.Limit;
import org.springframework.data.redis.connection.stream.MapRecord;
import org.springframework.data.redis.connection.stream.StreamInfo;
import org.springframework.data.redis.core.StreamOperations;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * /diag 系列的受控只读查询接口。
 *
 * <p>T1 (GET /diag/order) 确立了后续接口共用的模式；T4 (GET /diag/occupy-order)、
 * T5 (GET /diag/redis-stream) 与 T6 (GET /diag/device) 均沿用控制器自校验内部令牌、
 * 参数白名单拦截注入、统一 R&lt;T&gt; 响应、有界只读查询，以及
 * {@code DiagQueryAuditAspect} 的调用留痕。</p>
 */
@RestController
@RequestMapping("/diag")
public class DiagQueryController {

    private static final Pattern SAFE_VALUE = Pattern.compile("^[A-Za-z0-9_.:-]{1,128}$");
    private static final String REDIS_STREAM_QUEUE = "third.order.sync.queue";
    private static final String REDIS_STREAM_NOTIFY_QUEUE = "third.order.sync.notify.queue";
    private static final Set<String> REDIS_STREAM_WHITELIST =
            Set.of(REDIS_STREAM_QUEUE, REDIS_STREAM_NOTIFY_QUEUE);
    private static final int REDIS_STREAM_MAX_MESSAGES = 1000;

    private static final String ORDER_SQL = """
        SELECT id, order_no, tenant_id, status, type, billing_type, launch_type, is_test,
               device_id, device_code, child_device_id, child_device_code, site_id,
               electricity, electricity_fee, service_fee, ds_electric_fee, ds_service_fee,
               tip_electricity, tip_fee, tip_electricity_fee, tip_service_fee,
               peak_electricity, peak_fee, peak_electricity_fee, peak_service_fee,
               flat_electricity, flat_fee, flat_electricity_fee, flat_service_fee,
               valley_electricity, valley_fee, valley_electricity_fee, valley_service_fee,
               ds_tip_electricity, ds_tip_fee, ds_peak_electricity, ds_peak_fee,
               ds_flat_electricity, ds_flat_fee, ds_valley_electricity, ds_valley_fee,
               ds_electricity, ds_fee, has_electricity_loss, fee_template_id,
               launch_fee, park_fee, occupy_fee, appointment_fee, insurance_amount,
               market_amount, platform_amount, pay_amount, total_amount, reduce_balance,
               is_pay, is_receive_tx_data, sync_mall_order, out_trade_no,
               stopped_reason_code, stopped_reason_content, error_time, error_info,
               last_report_amount, transaction_id, meter_start, meter_end,
               white_flag, balance_insufficient_stop, start_soc, end_soc, device_protocol,
               created_time, stop_time, draw_gun_time, tx_data
        FROM ch_order_info
        WHERE order_no = ?
          AND (? IS NULL OR tenant_id = ?)
        ORDER BY created_time DESC
        LIMIT 3
        """;

    private static final String FEE_TEMPLATE_SQL = """
        SELECT order_no, tenant_id, fee_template, occupy_fee_template, period_fee_detail
        FROM ch_fee_template_record
        WHERE order_no = ?
          AND (? IS NULL OR tenant_id = ?)
        ORDER BY created_time DESC
        LIMIT 1
        """;

    private static final String OCCUPY_ORDER_BY_ORDER_NO_SQL = """
        SELECT id, orderId, order_no, device_id, device_code, child_device_id,
               child_device_code, site_id, userId, free_time, timeout, occupy_amount,
               pay_amount, status, out_trade_no, is_pay, is_sync_mall_order, pay_time,
               tenant_id, startTime, endTime, operator_id, refund_status, refund_amount,
               refund_time, refundRemark
        FROM ch_occupy_order_info
        WHERE order_no = ?
          AND (? IS NULL OR tenant_id = ?)
          AND (? IS NULL OR status = ?)
        ORDER BY startTime DESC
        LIMIT 20
        """;

    private static final String OCCUPY_ORDER_BY_ORDER_ID_SQL = """
        SELECT id, orderId, order_no, device_id, device_code, child_device_id,
               child_device_code, site_id, userId, free_time, timeout, occupy_amount,
               pay_amount, status, out_trade_no, is_pay, is_sync_mall_order, pay_time,
               tenant_id, startTime, endTime, operator_id, refund_status, refund_amount,
               refund_time, refundRemark
        FROM ch_occupy_order_info
        WHERE orderId = ?
          AND (? IS NULL OR tenant_id = ?)
          AND (? IS NULL OR status = ?)
        ORDER BY startTime DESC
        LIMIT 20
        """;

    private static final String DEVICE_BY_ID_SQL = """
        SELECT id, tenant_id, site_id, device_code, protocol, online_status,
               status, work_status, error_reason, fee_template_id
        FROM iot_charging_device
        WHERE id = ?
          AND (? IS NULL OR tenant_id = ?)
        LIMIT 1
        """;

    private static final String DEVICE_BY_CODE_SQL = """
        SELECT id, tenant_id, site_id, device_code, protocol, online_status,
               status, work_status, error_reason, fee_template_id
        FROM iot_charging_device
        WHERE device_code = ?
          AND (? IS NULL OR tenant_id = ?)
        LIMIT 1
        """;

    private final InternalTokenManager internalTokenManager;
    private final JdbcTemplate jdbcTemplate;
    private final StringRedisTemplate stringRedisTemplate;
    private final ObjectMapper objectMapper = new ObjectMapper();

    public DiagQueryController(
            InternalTokenManager internalTokenManager,
            JdbcTemplate jdbcTemplate,
            StringRedisTemplate stringRedisTemplate) {
        this.internalTokenManager = internalTokenManager;
        this.jdbcTemplate = jdbcTemplate;
        this.stringRedisTemplate = stringRedisTemplate;
    }

    @GetMapping("/order")
    @Transactional(readOnly = true, timeout = 5)
    public ResponseEntity<R<DiagQueryController.DiagOrderResponse>> order(
            @RequestHeader(value = "X-Internal-Token", required = false) String token,
            @RequestHeader(value = "X-Request-Timestamp", required = false) Long requestTimestamp,
            @RequestParam(value = "order_no") String orderNo,
            @RequestParam(value = "tenant_id", required = false) String tenantId,
            @RequestParam(value = "include_fee_template", defaultValue = "true") boolean includeFeeTemplate) {
        if (!internalTokenManager.validateToken(token, requestTimestamp)) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED).body(R.failed(401, "令牌无效或过期"));
        }
        String queryTenantId = blankToNull(tenantId);
        if (!isSafeValue(orderNo) || (queryTenantId != null && !isSafeValue(queryTenantId))) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(R.failed(400, "非法参数"));
        }

        List<Map<String, Object>> orders = jdbcTemplate.queryForList(ORDER_SQL, orderNo, queryTenantId, queryTenantId);
        DiagOrderResponse.FeeTemplateSnapshot feeTemplate = includeFeeTemplate
                ? queryFeeTemplate(orderNo, queryTenantId)
                : null;

        return ResponseEntity.ok(R.ok(new DiagOrderResponse(orders, feeTemplate)));
    }

    @GetMapping("/occupy-order")
    @Transactional(readOnly = true, timeout = 5)
    public ResponseEntity<R<List<Map<String, Object>>>> occupyOrder(
            @RequestHeader(value = "X-Internal-Token", required = false) String token,
            @RequestHeader(value = "X-Request-Timestamp", required = false) Long requestTimestamp,
            @RequestParam(value = "order_no", required = false) String orderNo,
            @RequestParam(value = "order_id", required = false) String orderId,
            @RequestParam(value = "tenant_id", required = false) String tenantId,
            @RequestParam(value = "status", required = false) String status) {
        if (!internalTokenManager.validateToken(token, requestTimestamp)) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED).body(R.failed(401, "令牌无效或过期"));
        }
        String queryTenantId = blankToNull(tenantId);
        String queryStatus = blankToNull(status);
        boolean hasOrderNo = orderNo != null && !orderNo.isBlank();
        boolean hasOrderId = orderId != null && !orderId.isBlank();
        if (!hasExactlyOneLookupKey(hasOrderNo, hasOrderId)
                || !isSafeValueOrBlank(orderNo)
                || !isSafeValueOrBlank(orderId)
                || !isSafeValueOrBlank(queryTenantId)
                || !isSafeValueOrBlank(queryStatus)) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(R.failed(400, "非法参数"));
        }

        String lookupValue = hasOrderNo ? orderNo : orderId;
        String occupyOrderSql = hasOrderNo
                ? OCCUPY_ORDER_BY_ORDER_NO_SQL
                : OCCUPY_ORDER_BY_ORDER_ID_SQL;
        List<Map<String, Object>> orders = jdbcTemplate.queryForList(
                occupyOrderSql,
                lookupValue,
                queryTenantId,
                queryTenantId,
                queryStatus,
                queryStatus);

        return ResponseEntity.ok(R.ok(orders));
    }

    @GetMapping("/redis-stream")
    public ResponseEntity<R<List<DiagQueryController.DiagRedisStreamResponse>>> redisStream(
            @RequestHeader(value = "X-Internal-Token", required = false) String token,
            @RequestHeader(value = "X-Request-Timestamp", required = false) Long requestTimestamp,
            @RequestParam(value = "stream", required = false) String stream,
            @RequestParam(value = "order_no", required = false) String orderNo,
            @RequestParam(value = "max_messages", defaultValue = "1000") int maxMessages) {
        if (!internalTokenManager.validateToken(token, requestTimestamp)) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED).body(R.failed(401, "令牌无效或过期"));
        }
        if (stream != null && !isAllowedStream(stream)) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(R.failed(400, "非白名单 Stream"));
        }
        String queryOrderNo = blankToNull(orderNo);
        if (queryOrderNo != null && !isSafeValue(queryOrderNo)) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(R.failed(400, "非法参数"));
        }
        List<String> streams = stream == null
                ? List.of(REDIS_STREAM_QUEUE, REDIS_STREAM_NOTIFY_QUEUE)
                : List.of(stream);
        int limit = Math.max(1, Math.min(maxMessages, REDIS_STREAM_MAX_MESSAGES));

        StreamOperations<String, String, String> streamOperations = stringRedisTemplate.opsForStream();
        List<DiagRedisStreamResponse> responses = new ArrayList<>();
        for (String streamName : streams) {
            responses.add(inspectStream(streamOperations, streamName, queryOrderNo, limit));
        }
        return ResponseEntity.ok(R.ok(responses));
    }

    private DiagRedisStreamResponse inspectStream(
            StreamOperations<String, String, String> streamOperations,
            String streamName,
            String orderNo,
            int limit) {
        DataType dataType = stringRedisTemplate.type(streamName);
        String type = dataType == null ? DataType.NONE.code() : dataType.code();
        if (dataType != DataType.STREAM) {
            return new DiagRedisStreamResponse(streamName, type, 0L, List.of(), 0, 0);
        }
        Long rawLength = streamOperations.size(streamName);
        long length = rawLength == null ? 0L : rawLength;
        List<DiagRedisStreamGroupResponse> groups = readGroups(streamOperations, streamName, length);
        List<MapRecord<String, String, String>> messages = streamOperations.reverseRange(
                streamName, Range.<String>unbounded(), Limit.limit().count(limit));
        int inspectedMessages = messages == null ? 0 : messages.size();
        int matches = countOrderMatches(messages, orderNo);
        return new DiagRedisStreamResponse(
                streamName, type, length, groups, inspectedMessages, matches);
    }

    private List<DiagRedisStreamGroupResponse> readGroups(
            StreamOperations<String, String, String> streamOperations,
            String streamName,
            long length) {
        StreamInfo.XInfoGroups groupInfos = streamOperations.groups(streamName);
        if (groupInfos == null || groupInfos.isEmpty()) {
            return List.of();
        }
        List<DiagRedisStreamGroupResponse> groups = new ArrayList<>();
        for (StreamInfo.XInfoGroup group : groupInfos) {
            groups.add(new DiagRedisStreamGroupResponse(
                    group.groupName(),
                    group.consumerCount(),
                    group.pendingCount(),
                    streamLag(length, group.lastDeliveredId())));
        }
        return groups;
    }

    private static int countOrderMatches(
            List<MapRecord<String, String, String>> messages,
            String orderNo) {
        if (orderNo == null || orderNo.isBlank() || messages == null || messages.isEmpty()) {
            return 0;
        }
        int matches = 0;
        for (MapRecord<String, String, String> message : messages) {
            Map<String, String> fields = message.getValue();
            if (fields == null) {
                continue;
            }
            for (Map.Entry<String, String> field : fields.entrySet()) {
                if ((field.getKey() != null && field.getKey().contains(orderNo))
                        || (field.getValue() != null && field.getValue().contains(orderNo))) {
                    matches += 1;
                    break;
                }
            }
        }
        return matches;
    }

    private static long streamLag(long length, String lastDeliveredId) {
        if (length <= 0 || lastDeliveredId == null || lastDeliveredId.isBlank()
                || "0-0".equals(lastDeliveredId)) {
            return length;
        }
        int separator = lastDeliveredId.lastIndexOf('-');
        if (separator < 0 || separator == lastDeliveredId.length() - 1) {
            return length;
        }
        try {
            long deliveredSequence = Long.parseLong(lastDeliveredId.substring(separator + 1));
            return Math.max(0L, length - deliveredSequence);
        } catch (NumberFormatException ignored) {
            return length;
        }
    }

    @GetMapping("/device")
    @Transactional(readOnly = true, timeout = 5)
    public ResponseEntity<R<Map<String, Object>>> device(
            @RequestHeader(value = "X-Internal-Token", required = false) String token,
            @RequestHeader(value = "X-Request-Timestamp", required = false) Long requestTimestamp,
            @RequestParam(value = "device_id", required = false) String deviceId,
            @RequestParam(value = "device_code", required = false) String deviceCode,
            @RequestParam(value = "tenant_id", required = false) String tenantId) {
        if (!internalTokenManager.validateToken(token, requestTimestamp)) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED).body(R.failed(401, "令牌无效或过期"));
        }
        String normalizedDeviceId = blankToNull(deviceId);
        String normalizedDeviceCode = blankToNull(deviceCode);
        String queryTenantId = blankToNull(tenantId);
        boolean hasDeviceId = normalizedDeviceId != null;
        boolean hasDeviceCode = normalizedDeviceCode != null;
        if (hasDeviceId == hasDeviceCode) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(R.failed(400, "非法参数"));
        }
        String deviceValue = hasDeviceId ? normalizedDeviceId : normalizedDeviceCode;
        if (!isSafeValue(deviceValue)
                || (queryTenantId != null && !isSafeValue(queryTenantId))) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(R.failed(400, "非法参数"));
        }
        String deviceSql = hasDeviceId ? DEVICE_BY_ID_SQL : DEVICE_BY_CODE_SQL;
        List<Map<String, Object>> devices = jdbcTemplate.queryForList(deviceSql, deviceValue, queryTenantId, queryTenantId);
        return ResponseEntity.ok(R.ok(devices.isEmpty() ? null : devices.get(0)));
    }

    private DiagOrderResponse.FeeTemplateSnapshot queryFeeTemplate(String orderNo, String tenantId) {
        return jdbcTemplate.query(
                FEE_TEMPLATE_SQL,
                resultSet -> {
                    if (!resultSet.next()) {
                        return null;
                    }
                    return new DiagOrderResponse.FeeTemplateSnapshot(
                            resultSet.getString("order_no"),
                            resultSet.getString("tenant_id"),
                            readJsonMap(resultSet.getString("fee_template")),
                            readJsonMap(resultSet.getString("occupy_fee_template")),
                            readJsonMap(resultSet.getString("period_fee_detail")));
                },
                orderNo,
                tenantId,
                tenantId);
    }

    private Map<String, Object> readJsonMap(String value) {
        if (value == null || value.isBlank()) {
            return Map.of();
        }
        try {
            return objectMapper.readValue(value, new TypeReference<Map<String, Object>>() {});
        } catch (Exception exception) {
            return Map.of("_raw", value);
        }
    }

    private static boolean hasExactlyOneLookupKey(boolean hasOrderNo, boolean hasOrderId) {
        return hasOrderNo != hasOrderId;
    }

    private static boolean isSafeValueOrBlank(String value) {
        return value == null || value.isBlank() || SAFE_VALUE.matcher(value).matches();
    }

    private static boolean isSafeValue(String value) {
        return value != null && SAFE_VALUE.matcher(value).matches();
    }

    private static boolean isAllowedStream(String stream) {
        return stream != null && REDIS_STREAM_WHITELIST.contains(stream);
    }

    private static String blankToNull(String value) {
        return value == null || value.isBlank() ? null : value;
    }

    public static class DiagOrderResponse {
        @JsonProperty("orders")
        private final List<Map<String, Object>> orders;

        @JsonProperty("fee_template")
        private final FeeTemplateSnapshot feeTemplate;

        public DiagOrderResponse(List<Map<String, Object>> orders, FeeTemplateSnapshot feeTemplate) {
            this.orders = orders;
            this.feeTemplate = feeTemplate;
        }

        public List<Map<String, Object>> getOrders() {
            return orders;
        }

        public FeeTemplateSnapshot getFeeTemplate() {
            return feeTemplate;
        }
    }

    public static class DiagRedisStreamResponse {
        @JsonProperty("stream")
        private final String stream;

        @JsonProperty("type")
        private final String type;

        @JsonProperty("length")
        private final long length;

        @JsonProperty("groups")
        private final List<DiagRedisStreamGroupResponse> groups;

        @JsonProperty("inspected_messages")
        private final int inspectedMessages;

        @JsonProperty("matches")
        private final int matches;

        public DiagRedisStreamResponse(
                String stream,
                String type,
                long length,
                List<DiagRedisStreamGroupResponse> groups,
                int inspectedMessages,
                int matches) {
            this.stream = stream;
            this.type = type;
            this.length = length;
            this.groups = groups;
            this.inspectedMessages = inspectedMessages;
            this.matches = matches;
        }

        public String getStream() {
            return stream;
        }

        public String getType() {
            return type;
        }

        public long getLength() {
            return length;
        }

        public List<DiagRedisStreamGroupResponse> getGroups() {
            return groups;
        }

        public int getInspectedMessages() {
            return inspectedMessages;
        }

        public int getMatches() {
            return matches;
        }
    }

    public static class DiagRedisStreamGroupResponse {
        @JsonProperty("name")
        private final String name;

        @JsonProperty("consumers")
        private final Long consumers;

        @JsonProperty("pending")
        private final Long pending;

        @JsonProperty("lag")
        private final long lag;

        public DiagRedisStreamGroupResponse(
                String name,
                Long consumers,
                Long pending,
                long lag) {
            this.name = name;
            this.consumers = consumers;
            this.pending = pending;
            this.lag = lag;
        }

        public String getName() {
            return name;
        }

        public Long getConsumers() {
            return consumers;
        }

        public Long getPending() {
            return pending;
        }

        public long getLag() {
            return lag;
        }
    }

    public static class FeeTemplateSnapshot {
        @JsonProperty("order_no")
        private final String orderNo;

        @JsonProperty("tenant_id")
        private final String tenantId;

        @JsonProperty("fee_template")
        private final Map<String, Object> feeTemplate;

        @JsonProperty("occupy_fee_template")
        private final Map<String, Object> occupyFeeTemplate;

        @JsonProperty("period_fee_detail")
        private final Map<String, Object> periodFeeDetail;

        public FeeTemplateSnapshot(
                String orderNo,
                String tenantId,
                Map<String, Object> feeTemplate,
                Map<String, Object> occupyFeeTemplate,
                Map<String, Object> periodFeeDetail) {
            this.orderNo = orderNo;
            this.tenantId = tenantId;
            this.feeTemplate = feeTemplate;
            this.occupyFeeTemplate = occupyFeeTemplate;
            this.periodFeeDetail = periodFeeDetail;
        }

        public String getOrderNo() {
            return orderNo;
        }

        public String getTenantId() {
            return tenantId;
        }

        public Map<String, Object> getFeeTemplate() {
            return feeTemplate;
        }

        public Map<String, Object> getOccupyFeeTemplate() {
            return occupyFeeTemplate;
        }

        public Map<String, Object> getPeriodFeeDetail() {
            return periodFeeDetail;
        }
    }
}
