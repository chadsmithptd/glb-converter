# STEP Metrics Project

This repository now contains two components of the same project:

- `cli_engine/`: C++ headless engine (`StepMetricsCli`) that reads STEP files and emits JSON metrics.
- `web_step_metrics_app/`: npm web app that uploads STEP files and calls `StepMetricsCli`.

## Build CLI engine

### Windows

```bash
cmake -S . -B build
cmake --build build --config Release --target StepMetricsCli
```

### Linux (Ubuntu)

```bash
cmake -S . -B build -DOPEN_CASCADE_DIR=/path/to/occt
cmake --build build --target StepMetricsCli -j
```

## Run web app

```bash
cd web_step_metrics_app
npm install
```

Open the `.env` and set:

```env
CLI_PATH=/absolute/path/to/StepMetricsCli(.exe)
```

Then run:

```bash
npm start
```

Open `http://localhost:3000`.

