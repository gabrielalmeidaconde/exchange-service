# imagem slim: menos pacotes do SO para o Trivy apontar como vulneráveis
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# não roda como root (mesmo uid dos outros serviços nos manifestos k8s)
RUN groupadd --system app && useradd --system --gid app --uid 1001 app
USER 1001

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=2)"

# 1 processo por container: as métricas do prometheus_client ficam na memória
# do processo, e com vários workers cada scrape veria um worker diferente.
# Para escalar, mais réplicas (compose/k8s).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
