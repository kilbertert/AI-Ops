# 客户机 AI-Ops Gateway 不可变 OCI 运行镜像（ADR-0363）。
#
# 设计要点：
# 1. 两层缓存结构：requirements.txt（锁定第三方 wheel）作为稳定底座层，
#    aiops_diagnostics wheel（仅 ~400 KB）作为应用增量层，常规小版本升级国内秒拉。
# 2. 统一 chmod a+rX /opt/aiops，支持客户机项目专属非 root UID/GID 在 --read-only 下运行。
# 3. 通过标准 OCI LABEL 记录精确 version/revision/ingress_sha256，运行与回滚一律锁定 @sha256 摘要。

ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

ARG AIOPS_VERSION=0.0.0
ARG AIOPS_REVISION=unknown
ARG AIOPS_FULL_VERSION=0.0.0+unknown
ARG AIOPS_INGRESS_SHA256=

LABEL org.opencontainers.image.title="aiops-gateway" \
      org.opencontainers.image.description="AI-Ops customer read-only gateway runtime" \
      org.opencontainers.image.version="${AIOPS_FULL_VERSION}" \
      org.opencontainers.image.revision="${AIOPS_REVISION}" \
      aiops.version="${AIOPS_VERSION}" \
      aiops.full_version="${AIOPS_FULL_VERSION}" \
      aiops.revision="${AIOPS_REVISION}" \
      aiops.ingress_sha256="${AIOPS_INGRESS_SHA256}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONNOUSERSITE=1

RUN python -m venv /opt/aiops/venv

# 第一层：锁定第三方依赖底座（uv.lock 不变时跨版本完全复用）
COPY requirements.txt /tmp/requirements.txt
RUN /opt/aiops/venv/bin/pip install --no-cache-dir --no-compile \
        --only-binary=:all: --require-hashes -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt \
    && chmod -R a+rX /opt/aiops

# 第二层：自研 Gateway 应用增量层（含带 <ver>+<rev12> 标记的 __version__）
COPY wheelhouse/aiops_diagnostics-*.whl /tmp/wheelhouse/
RUN /opt/aiops/venv/bin/pip install --no-cache-dir --no-compile \
        --no-deps /tmp/wheelhouse/aiops_diagnostics-*.whl \
    && rm -rf /tmp/wheelhouse \
    && chmod -R a+rX /opt/aiops/venv/lib/python*/site-packages/aiops_diagnostics* /opt/aiops/venv/bin

WORKDIR /opt/aiops

ENTRYPOINT ["/opt/aiops/venv/bin/aiops-gateway"]
CMD ["serve", "--host", "127.0.0.1", "--port", "8788"]
