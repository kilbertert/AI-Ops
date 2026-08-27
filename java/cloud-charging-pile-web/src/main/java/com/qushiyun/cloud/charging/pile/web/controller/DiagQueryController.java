package com.qushiyun.cloud.charging.pile.web.controller;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.qushiyun.cloud.common.auth.component.InternalTokenManager;
import com.qushiyun.cloud.common.core.util.R;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * /diag 系列的诊断查询接口。
 *
 * <p>包含 T1 {@code GET /diag/order} 与 T6 {@code GET /diag/device}，两者沿用
 * 同一模式：控制器自校验内部令牌、参数白名单拦截注入、统一 R&lt;T&gt; 响应、
 * 有界只读查询，以及 {@code DiagQueryAuditAspect} 的调用留痕。</p>
 */
@RestController
@RequestMapping("/diag")
public class DiagQueryController {

    private static final Pattern SAFE_VALUE = Pattern.compile("^[A-Za-z0-9_.:-]{1,128}$");

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
    private final ObjectMapper objectMapper = new ObjectMapper();

    public DiagQueryController(InternalTokenManager internalTokenManager, JdbcTemplate jdbcTemplate) {
        this.internalTokenManager = internalTokenManager;
        this.jdbcTemplate = jdbcTemplate;
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

    private static boolean isSafeValue(String value) {
        return value != null && SAFE_VALUE.matcher(value).matches();
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
