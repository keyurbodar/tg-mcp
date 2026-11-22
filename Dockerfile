FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml ./
COPY tg_mcp ./tg_mcp
RUN pip install --no-cache-dir . && useradd -u 1000 -m app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["python", "-m", "tg_mcp.worker"]
