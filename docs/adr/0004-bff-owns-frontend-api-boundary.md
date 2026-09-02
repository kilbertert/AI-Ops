# BFF 负责小程序前端业务接口

Status: Accepted

小程序只调用业务后端或 BFF 提供的订单健康报告与单问诊断接口，不直接调用 AI-Ops Gateway。BFF 验证用户会话、提供车辆展示信息和页面聚合契约，AI-Ops 只接受受认证的服务调用与可验证的用户授权委托，并独立执行订单范围校验；这避免把设备令牌、workspace、provider、fixture、内部事件和证据结构暴露为 C 端产品契约。
