FROM python:3.12-slim

# 固定依赖版本；运行时不安装任何额外包（SANDBOX 边界：无动态安装）。
RUN pip install --no-cache-dir "mcp==1.30.0"

# 非 root、只读式部署：代码与运行时都放在镜像内，工作目录可写但不挂载宿主路径。
RUN useradd --create-home --uid 10001 kicad
USER kicad
WORKDIR /home/kicad/app

COPY --chown=kicad:kicad server.py ./

ENV KICAD_MCP_HOST=0.0.0.0 \
    KICAD_MCP_PORT=8080
EXPOSE 8080

# 仅暴露 /mcp（streamable_http）；由内网反向代理或容器网络控制可达范围。
CMD ["python", "server.py"]
