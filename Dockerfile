# LedgerSight. One process, one worker: state and evidence chains are in memory.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AVOCET_DATA_DIR=/tmp/ledgersight-data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY ledgersight ./ledgersight
RUN useradd --uid 10001 --create-home app && chown -R app:app /app
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).status==200 else 1)"
# Hosted: MFA on, secure cookies on, keys from the secret store (AVOCET_SIGNING_KEY_PEM, AVOCET_BOOTSTRAP_*).
CMD ["python", "-m", "uvicorn", "ledgersight.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
