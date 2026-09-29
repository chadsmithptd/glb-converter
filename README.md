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

## Feature output (hole detection + face classes)

`StepMetricsCli` (analyze mode, and `POST /api/analyze-step`) also reports:

- `geometry.face_classes` -- surface area in in^2 split into `planar`, `contour`
  (cylinder/cone/sphere/torus/revolution/extrusion) and `generic` (B-spline/other) faces.
- `geometry.hole_summary` -- hole count and through-hole count. Small, so it is safe in the
  `X-Part-Metrics` header returned by `POST /api/convert-step-to-glb`.
- `features.holes` -- one entry per detected hole, from the exact B-rep geometry: concave
  cylindrical faces grouped by radius, coaxial axis and overlapping extent (split halves merged;
  a hole needs >= 300 deg of combined arc). Each hole has a stable geometric `key`, `diameter`,
  `depth` (cylindrical wall, drill point excluded), `through` (true/false/null), `center`,
  `axis`, `end_points`, `stack_id`/`stack_size` (coaxial stacks such as counterbores) and a
  `suggested_type`. All lengths in inches. This list is top-level on purpose so it never goes
  into the HTTP header; read it from `/api/analyze-step`.

