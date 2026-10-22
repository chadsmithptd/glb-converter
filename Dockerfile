FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# System build tools + CMake + OCCT dev packages (no visualization needed)
RUN apt-get update && apt-get install -y \
    cmake \
    g++ \
    curl \
    pkg-config \
    libocct-foundation-dev \
    libocct-modeling-data-dev \
    libocct-modeling-algorithms-dev \
    libocct-data-exchange-dev \
    libocct-ocaf-dev \
    python3 \
    python3-venv \
    make \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# DWG drawings (POST /api/convert-dwg-to-pdf). LibreDWG (GPL-3.0) is built from the GNU release
# tarball, pinned by checksum, and only its dwg2dxf binary is kept (statically linked).
ARG LIBREDWG_VERSION=0.14
ARG LIBREDWG_SHA256=cb6ee0b078c6d9e0f09d66f1feac33ba6342df88ae544e9f9335fab475218351
RUN curl -fsSL "https://ftpmirror.gnu.org/libredwg/libredwg-${LIBREDWG_VERSION}.tar.gz" -o /tmp/libredwg.tar.gz \
    && echo "${LIBREDWG_SHA256}  /tmp/libredwg.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/libredwg.tar.gz -C /tmp \
    && cd "/tmp/libredwg-${LIBREDWG_VERSION}" \
    && ./configure --disable-bindings --disable-python --disable-docs --disable-shared \
    && make -j"$(nproc)" \
    && install -m 0755 programs/dwg2dxf /usr/local/bin/dwg2dxf \
    && cd / && rm -rf /tmp/libredwg*

# The DWG renderer's Python packages, pinned to the tested versions, in their own venv. The
# matplotlib font cache is built here so requests don't rebuild it.
RUN python3 -m venv /opt/dwg-venv \
    && /opt/dwg-venv/bin/pip install --no-cache-dir ezdxf==1.4.5 matplotlib==3.10.9 \
    && mkdir -p /opt/mplconfig \
    && MPLCONFIGDIR=/opt/mplconfig /opt/dwg-venv/bin/python -c "import matplotlib.font_manager" \
    && chmod -R a+rX /opt/mplconfig

# Unprivileged user the DWG tools run as (DWG_RENDER_UID below).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin dwgrender

# Node.js 20
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Build C++ CLI
RUN cmake -S . -B build && cmake --build build --target StepMetricsCli -j$(nproc)

# Install Node.js dependencies
WORKDIR /app/web_step_metrics_app
RUN npm install --omit=dev

# CLI will be at this path after build
ENV CLI_PATH=/app/build/cli_engine/StepMetricsCli
ENV DWG2DXF_PATH=/usr/local/bin/dwg2dxf
ENV DWG_PYTHON=/opt/dwg-venv/bin/python
ENV DWG_MPLCONFIGDIR=/opt/mplconfig
ENV DWG_RENDER_UID=10001

ENV PORT=3000
EXPOSE 3000
CMD ["node", "server.js"]
