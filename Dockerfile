# Stellwerk als Container. Bauen: docker build -t stellwerk .
# Läuft ohne Root, ohne Schreibrechte außer /daten, mit Texterkennung (Deutsch).

FROM python:3.12-slim AS bau
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install ".[ocr,postgres]"

FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-deu poppler-utils \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --home-dir /daten --shell /usr/sbin/nologin stellwerk \
 && mkdir -p /daten && chown stellwerk /daten
COPY --from=bau /install /usr/local
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STELLWERK_ARBEITSBEREICH=/daten/arbeitsbereich \
    STELLWERK_AUSGABE=/daten/out
USER stellwerk
WORKDIR /daten
VOLUME ["/daten"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=4).status == 200 else 1)"
CMD ["stellwerk", "serve", "--host", "0.0.0.0", "--port", "8080"]
