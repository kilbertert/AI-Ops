# AI-Ops Dify 36 宿主双侧边车共享 OCI 运行镜像（ADR-0367）
# 同一份只读镜像在 36 上拆成两个严格隔离的容器运行：
#   - aiops-dify-console-bridge (aiopsbridge / 127.0.0.1:10118)
#   - aiops-dify-login-proxy    (aiopslogin  / 127.0.0.1:10119)
FROM python:3.12-slim

ARG AIOPS_EDGE_VERSION=dev
ARG AIOPS_EDGE_REVISION=unknown

LABEL org.opencontainers.image.title="aiops-dify-edge" \
      org.opencontainers.image.description="AI-Ops Dify console-bridge & login-proxy unified read-only OCI image" \
      org.opencontainers.image.version="${AIOPS_EDGE_VERSION}" \
      org.opencontainers.image.revision="${AIOPS_EDGE_REVISION}"

WORKDIR /opt/aiops-edge

RUN pip install --no-cache-dir cryptography==46.0.7

# 仅打入两只侧边车及其最小公共/协议依赖，不带 Gateway 调度栈或宿主机特权工具
COPY services/dify-console-bridge/dify_console_bridge /opt/aiops-edge/dify_console_bridge
COPY services/dify-login-proxy/dify_login_proxy /opt/aiops-edge/dify_login_proxy
COPY src/aiops_diagnostics/__init__.py /opt/aiops-edge/aiops_diagnostics/__init__.py
COPY src/aiops_diagnostics/common /opt/aiops-edge/aiops_diagnostics/common
COPY src/aiops_diagnostics/dify /opt/aiops-edge/aiops_diagnostics/dify
COPY src/aiops_diagnostics/gateway /opt/aiops-edge/aiops_diagnostics/gateway
COPY src/aiops_diagnostics/identity /opt/aiops-edge/aiops_diagnostics/identity
COPY src/aiops_diagnostics/onboarding /opt/aiops-edge/aiops_diagnostics/onboarding

# 登录页与宿主 Nginx 配置基准作为静态资产随版本同框归档（宿主按需比对/更新）
COPY ops/dify-login-proxy/dify-login-page.html /opt/aiops-edge/assets/dify-login-page.html
COPY ops/dify-console-bridge/dify-console-10008.conf /opt/aiops-edge/assets/dify-console-10008.conf
COPY ops/dify-console-bridge/dify-console-publish.conf /opt/aiops-edge/assets/dify-console-publish.conf
COPY ops/dify-login-proxy/dify-login-proxy.conf /opt/aiops-edge/assets/dify-login-proxy.conf

# 容器运行时降权至 aiopsbridge / aiopslogin 只读运行，必须确保代码与资产对非 root 可读
RUN chmod -R a+rX /opt/aiops-edge

ENV PYTHONPATH=/opt/aiops-edge \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "dify_console_bridge"]
