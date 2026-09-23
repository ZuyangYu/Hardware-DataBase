# Offline minimal Python runtime; scratch has no inherited anonymous volumes.
FROM hardware-database-app:latest AS py

FROM scratch

COPY --from=py /usr/lib /usr/lib
COPY --from=py /lib /lib
COPY --from=py /usr/lib64 /usr/lib64
COPY --from=py /lib64 /lib64
COPY --from=py /usr/local /usr/local

WORKDIR /opt/mcp
COPY --chmod=0555 stdio_server.py /opt/mcp/stdio_server.py
COPY --chmod=0555 stdio_bridge.py /opt/mcp/stdio_bridge.py
LABEL org.hdb.stdio.server.sha256="4c839643fb0ac5ead96754d3b08f8b4466ed91bc4d7ac4b566903ee26e1e2843" \
      org.hdb.stdio.bridge.sha256="969217af096f4d51dff6a75508eee4d0671e5e01519d8a30f0a721390d90469b"

ENV MCP_BRIDGE_PORT=8080 \
    MCP_STDIO_SERVER=/opt/mcp/stdio_server.py
EXPOSE 8080

USER 65534:65534
ENTRYPOINT ["/usr/local/bin/python3", "/opt/mcp/stdio_bridge.py"]
