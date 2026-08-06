FROM python:3.12-slim

# Unlike Ubertooth/HackRF, Debian's packaged rtl-sdr is current enough --
# no from-source build needed here (verified: rtl_power's CSV output and
# process-lifecycle behavior were checked against exactly this package on
# the target Raspberry Pi).
RUN apt-get update && apt-get install -y --no-install-recommends \
        rtl-sdr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ .

ENTRYPOINT ["python", "main_rtlsdr.py"]
