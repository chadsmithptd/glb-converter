# STEP Metrics Web App

A minimal npm web app wrapper around `StepMetricsCli`.

## 1) Install

```bash
npm install
```

## 2) Configure CLI path

Copy `.env.example` to `.env` and set `CLI_PATH`.

Windows example:

```env
CLI_PATH=G:\UpWorks\16_STEP_READER\bezier_Curves_Qt5_App\build\cli_engine\Release\StepMetricsCli.exe
```

Linux example:

```env
CLI_PATH=/opt/step-metrics/StepMetricsCli
```

## 3) Run

```bash
npm start
```

Open: http://localhost:3000

## API

- `GET /api/health`
- `POST /api/analyze-step` (form-data field: `stepFile`)

## Notes

- Backend executes CLI server-side via subprocess.
- Only `.step` / `.stp` files are accepted.
- Uploaded files are stored temporarily in `uploads/` and removed after processing.
- STEP preview in browser uses `three` + `occt-import-js` served locally from `node_modules` (`/vendor/*`).
- If preview fails after dependency changes, run `npm install` again and restart `npm start`.

## Thumbnail Modes

- `Three.js Frontend`: generated in the browser from the current Three.js preview canvas.
- `OCCT Native`: generated on the server by `StepMetricsCli --thumbnail` using native OCCT rendering.
