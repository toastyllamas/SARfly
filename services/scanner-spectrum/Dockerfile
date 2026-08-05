FROM debian:trixie-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git pkg-config ca-certificates \
        libusb-1.0-0-dev libfftw3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Debian packages an older hackrf release; build current upstream instead
# so hackrf_sweep matches the device firmware, same rationale as
# services/scanner/Dockerfile.ubertooth.
RUN git clone --depth 1 https://github.com/greatscottgadgets/hackrf.git && \
    cmake -S hackrf/host -B hackrf/host/build \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5 && \
    cmake --build hackrf/host/build -j"$(nproc)" && \
    cmake --install hackrf/host/build && ldconfig

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libusb-1.0-0 libfftw3-single3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/lib/libhackrf* /usr/local/lib/
COPY --from=build /usr/local/bin/hackrf_sweep /usr/local/bin/hackrf_info /usr/local/bin/
RUN ldconfig

WORKDIR /app
COPY app/ .

ENTRYPOINT ["python", "main_spectrum.py"]
