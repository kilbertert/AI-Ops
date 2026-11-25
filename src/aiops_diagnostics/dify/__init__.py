"""Gateway 侧的 Dify 协议、工作空间、发布、工作流与知识适配。

独立部署的 Dify 旁路进程已迁至 ``services/``，不再混入本包：
- 控制台发布/管理桥：``services/dify-console-bridge/``（``python -m dify_console_bridge``）
- 公司登录代理：``services/dify-login-proxy/``（``python -m dify_login_proxy``）
"""
