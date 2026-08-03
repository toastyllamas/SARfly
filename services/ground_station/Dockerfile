FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# Vendor Leaflet at build time (not via pip/npm -- it's a plain JS/CSS
# library) so the map UI loads with zero internet at container runtime.
# Only the actual tile images need live connectivity; markers/popups/zoom
# all work offline once these two files are present.
ARG LEAFLET_VERSION=1.9.4
RUN mkdir -p static/vendor/leaflet && \
    python -c "import urllib.request; urllib.request.urlretrieve('https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/leaflet.js', 'static/vendor/leaflet/leaflet.js')" && \
    python -c "import urllib.request; urllib.request.urlretrieve('https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/leaflet.css', 'static/vendor/leaflet/leaflet.css')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
