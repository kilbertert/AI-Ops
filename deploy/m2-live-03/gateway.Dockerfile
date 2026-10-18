# 隔离 Gateway 的可部署镜像：**启动不下载、不解析依赖**。
#
# 依赖来自 `gateway-requirements.txt`（由 `uv export --frozen` 从 `uv.lock` 生成，
# 每条都带 `--hash`），源码来自构建上下文里的 `src/`。二者与 Dockerfile 的摘要都
# 写进镜像标签，preflight 用标签核对实际运行的镜像，而不是看 tag 名。
#
# 构建只发生在开发机/runner；服务宿主只加载已识别的镜像（docker save/load）。
ARG M2LIVE_BASE_IMAGE=python:3.13-slim
FROM ${M2LIVE_BASE_IMAGE}

LABEL org.opencontainers.image.title="aiops-gateway (m2-live-03 isolated)" \
      m2live.role="isolated-gateway"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 依赖装进独立前缀，运行时只读；/data 是可写状态，不放依赖。
COPY gateway-requirements.txt /tmp/gateway-requirements.txt
RUN python -m pip install --no-cache-dir --no-compile \
        --require-hashes --no-deps -r /tmp/gateway-requirements.txt \
    && rm -f /tmp/gateway-requirements.txt

COPY src/ /opt/aiops-src/src/
COPY tools/ /opt/aiops-src/tools/
# 仓库里的文件默认是 0640/0750（开发机 umask 027）。镜像里的服务用户不是构建用户，
# 不放开读权限就会以「模块不存在」的形式失败 —— 那是权限问题，不是缺文件。
RUN chmod -R a+rX /opt/aiops-src

ENV PYTHONPATH=/opt/aiops-src/src
WORKDIR /opt/aiops-src

# 以数字 UID 运行；compose 里再显式写一次，两者必须一致（preflight 核对）。
USER 999:987

# 启动前先过隔离门禁，再起服务：门禁拒绝就不监听任何端口。
CMD ["python", "-m", "aiops_diagnostics.gateway_server", "serve"]
