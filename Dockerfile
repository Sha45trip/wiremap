FROM python:3.12-slim

WORKDIR /opt/wiremap
COPY pyproject.toml README.md ./
COPY wiremap/ wiremap/
RUN pip install --no-cache-dir .

# /repo: the codebase to scan (mount read-only)
# /data: scan outputs, runtime store, cache (mount a volume)
VOLUME ["/repo", "/data"]
EXPOSE 8787
ENV PYTHONUNBUFFERED=1

CMD ["wiremap", "serve", "/repo", "--out", "/data", "--port", "8787", \
     "--rescan-interval", "900"]
