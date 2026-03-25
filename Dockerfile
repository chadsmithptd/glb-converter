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
    && rm -rf /var/lib/apt/lists/*

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
ENV PORT=3000

EXPOSE 3000
CMD ["node", "server.js"]
