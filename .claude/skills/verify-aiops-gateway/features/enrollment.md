# 设备注册（enrollment）

## Sub-features

- 管理员本地签发一次性注册码（`issue-enrollment`）
- 注册码兑换为设备令牌（`POST /v1/enroll`）
- 工作区级注册（不传 `--tenant-id`）vs 租户绑定注册（传 `--tenant-id`）

## How to get to it (user POV)

运维/管理员在 Gateway 所在服务器上签发注册码，客户端用它换取设备令牌。这是
**设备获得访问权的唯一入口**——没有别的途径能拿到可用令牌。

```bash
uv run aiops-gateway issue-enrollment --workspace ops
# {"enrollment_code":"enr_…","workspace_id":"ops","tenant_id":null,"expires_in_seconds":600}
```

## Driving it

```bash
curl -s -X POST http://127.0.0.1:8787/v1/enroll \
  -H 'Content-Type: application/json' \
  -d '{"code":"enr_…","device_name":"verify","platform":"linux"}'
# 201 {"device_id":"dev_…","workspace_id":"ops","tenant_id":null,"token":"aops_…"}
```

## Gotchas

- **注册码是一次性的。** 同一个码第二次兑换失败。需要两个设备令牌就签两个码 ——
  复用会得到一个看起来像"鉴权坏了"的失败。
- **三个字段全必填，且模型 `extra="forbid"`。** 漏 `device_name` 或 `platform`
  返回 `INVALID_REQUEST: request validation failed`，**不指明是哪个字段**。
  看到这个错误先核对三个字段是否齐全，再去怀疑代码。
- 注册码 `expires_in_seconds` 默认 600；验证过程拖太久会过期，届时重新签一个。
- `--tenant-id` 不传 = 工作区级（可诊断任意订单）；只在需要把设备限制到单一租户时才传。
  验证时留意你签的是哪一种 —— 两者的后续可见范围不同。
- **令牌是凭据。** 不要把它写进证据文件、提交或日志。`observe.py` 只记录
  `device_id` 与状态码，不记录 token。
