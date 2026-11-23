"""Dify工作空间、登录、发布、工作流与知识协议适配。

本包同时承载三类东西，按文件名区分：
- 独立进程：``dify_console_publish``（控制台桥HTTP服务）与
  ``dify_login_*``（登录代理）有自己的 ``main()``，由 ``ops/`` 下
  部署资产拉起，不经 ``aiops-gateway`` 入口；
- 环境接入的 Dify 供给侧：``environment_*`` 模块与 ``gateway/``
  包内的同名前缀分属同一流程的两半（见 ``gateway/__init__.py``）；
- 挂载进网关应用的路由适配：``self_service_api``、
  ``environment_management``、``operator_run_logs`` 等经
  ``register()`` 在 ``gateway_api.create_gateway_app`` 中装配，
  grep ``@app.`` 找不到这部分路由。
"""
