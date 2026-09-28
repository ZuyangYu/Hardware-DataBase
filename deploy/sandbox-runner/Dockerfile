# Offline minimal Python runtime; scratch has no inherited volumes, shell,
# network clients, PostgreSQL files, or HDB application files.
FROM hardware-database-app:latest AS py

FROM scratch

COPY --from=py /usr/lib /usr/lib
COPY --from=py /lib /lib
COPY --from=py /usr/lib64 /usr/lib64
COPY --from=py /lib64 /lib64
COPY --from=py /usr/local /usr/local

WORKDIR /opt/bundle
COPY --chmod=0555 bundles/report/entrypoint.py /opt/bundle/report/entrypoint.py
COPY --chmod=0555 bundles/slow/entrypoint.py /opt/bundle/slow/entrypoint.py
LABEL org.hdb.bundle.report.sha256="ce2087faaf6debfd8d0ac6cfd834a83651bcfb0dd5d6dcba01ce3a4c9f854e51" \
      org.hdb.bundle.slow.sha256="e2084f3145c87bdfecbd78c70a179560f5d5c9efb355ab0fd1a9233d14fc7437"

# No shell, no network, non-root; the Runner supplies all runtime flags and
# the reviewed entrypoint path from the bundle manifest.
USER 65534:65534
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/python3"]
