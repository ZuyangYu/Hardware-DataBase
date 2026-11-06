FROM node:22-slim

WORKDIR /app

# Pinned dependencies; install scripts are skipped because the draw.io package
# only uses postinstall to warm an optional CDN cache.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

COPY server.mjs smoke.mjs ./

RUN useradd --create-home --uid 10001 drawio
USER drawio

ENV DRAWIO_MCP_HOST=0.0.0.0 \
    DRAWIO_MCP_PORT=8091 \
    DRAWIO_BASE_URL=https://app.diagrams.net/
EXPOSE 8091

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:8091/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.mjs"]
