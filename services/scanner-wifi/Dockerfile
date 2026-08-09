FROM python:3.12-slim

# iw: set monitor mode / channel. iproute2: bring the interface down/up
# around the mode switch.
RUN apt-get update && apt-get install -y --no-install-recommends iw iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

ENTRYPOINT ["python", "main_wifi.py"]
