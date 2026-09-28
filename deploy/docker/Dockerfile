# syntax=docker/dockerfile:1
#
# Hardware DataBase 应用镜像：FastAPI API + 全部后台 worker 共用一个镜像，
# 通过容器 CMD/环境变量选择角色（默认 API，会按 src/api/app.py 的 lifespan
# 自动拉起 chat/authoring/parse worker 与 export worker）。
#
# 构建（在仓库根目录）：
#   docker build -f deploy/docker/Dockerfile -t hardware-database-app:latest .
# 或直接用 compose 覆盖文件：
#   docker compose -f deploy/docker/docker-compose.yml \
#                  -f deploy/docker/docker-compose.build.yml build
#
# 国内/内网构建加速（构建机仍需能访问源）：
#   --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
#   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#   --build-arg HDB_INSTALL_EVAL=1        # 额外装 RAGAS 评估依赖组

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

ARG UV_VERSION=0.12.5
ARG APT_MIRROR=
ARG PIP_INDEX_URL=
ARG HDB_INSTALL_EVAL=0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Asia/Shanghai \
    PATH=/app/.venv/bin:$PATH

# 系统依赖说明：
#   fonts-droid-fallback -> PDF 导出嵌入中文字体，src/result_exports/renderers.py
#                           探测 /usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf
#   ca-certificates      -> 访问 HTTPS 的 RAGFlow / LLM 端点
#   tzdata               -> 让 TZ 与日志时间戳用上真实时区
#   curl                 -> 排障用（健康检查走 python，不依赖它）
RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
      find /etc/apt \( -name '*.sources' -o -name 'sources.list' \) -type f \
        -exec sed -i -e "s|deb.debian.org|${APT_MIRROR}|g" \
                      -e "s|security.debian.org|${APT_MIRROR}|g" {} +; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates curl fonts-droid-fallback tzdata \
      # 附件 OCR/视觉解析（对话里贴扫描件/图片时用）：
      tesseract-ocr tesseract-ocr-chi-sim poppler-utils; \
    rm -rf /var/lib/apt/lists/*; \
    if [ -n "$PIP_INDEX_URL" ]; then \
      pip install --no-cache-dir --index-url "$PIP_INDEX_URL" "uv==${UV_VERSION}"; \
    else \
      pip install --no-cache-dir "uv==${UV_VERSION}"; \
    fi

WORKDIR /app

# 依赖层：只拷清单，uv.lock 不变时命中缓存。
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# 项目源码（.dockerignore 已排除 .env、运行期 storage/、node_modules 等）。
COPY . .

RUN uv sync --frozen --no-dev
RUN if [ "$HDB_INSTALL_EVAL" = "1" ]; then uv sync --frozen --no-dev --group eval; fi

# RAGAS 评估会用到 nltk 语料；预置进镜像避免内网首次运行时联网下载。
# 下载失败不阻断构建（应用本体不依赖这些语料）。
RUN if [ "$HDB_INSTALL_EVAL" = "1" ]; then \
      python -m nltk.downloader -d /usr/share/nltk_data punkt punkt_tab stopwords \
      || echo "[hdb] WARN: nltk 语料下载失败（仅影响分词类评估指标）"; \
    fi

# 应用运行期目录：storage 由卷挂载，config 挂载存放可写 .env。
RUN mkdir -p /app/storage /app/config && chmod +x /app/deploy/docker/entrypoint.sh

# 管理页「系统配置」保存时需要原子替换 .env，镜像层与单文件 bind mount 都不允许
# rename，因此把可写配置指向挂载目录（宿主机 ./config/.env）。
ENV HDB_ENV_PATH=/app/config/.env \
    HDB_API_HOST=0.0.0.0 \
    HDB_API_PORT=8001

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=5 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/health/live', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/app/deploy/docker/entrypoint.sh"]
# 默认角色：API。worker 角色示例：
#   hardware-database-worker --role chat|authoring|parse
#   hardware-database-memory-worker / hardware-database-export-worker
CMD ["hardware-database-server"]
