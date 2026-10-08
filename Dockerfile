# Skill OS 镜像 —— 云端（容器 A）与沙盒（容器 B）共用，仅环境变量切换（04 §1.4）：
#   SKILLOS_SANDBOX=1  沙盒可写隔离演示模式（FR-2.4.3）
#   SKILLOS_ROOT       runtime/repo 根指向容器内虚拟目录（T1）

# ---- Web 构建层 ----
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---- 运行层 ----
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# CODEBUDDY.md 是 pyproject 的 readme（long_description 来源）—— 缺它 setuptools
# 构建元数据阶段直接失败，镜像构建会在 pip install 处断掉。
COPY pyproject.toml CODEBUDDY.md ./
COPY skillos/ ./skillos/
COPY scripts/ ./scripts/
COPY sandbox/ ./sandbox/
RUN pip install --no-cache-dir .

COPY --from=web /web/dist ./web_dist

# seed 预置演示数据（构建期生成；sandbox_bootstrap install-seed 负责落位）
RUN python scripts/build_sandbox_seed.py /seed

# SKILLOS_WEB_DIST：FastAPI 同进程托管 SPA（04 §1.4 容器 A，挂载点在 create_app 末尾）
ENV SKILLOS_WEB_DIST=/app/web_dist \
    PYTHONUNBUFFERED=1

EXPOSE 8000
# 入口必须用 asgi（main.py 只导出 create_app 工厂，没有模块级 app）。
CMD ["uvicorn", "skillos.api.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
