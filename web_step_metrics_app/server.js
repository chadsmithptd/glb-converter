const fs = require("fs");
const path = require("path");
const { execFile } = require("child_process");
const express = require("express");
const cors = require("cors");
const multer = require("multer");
require("dotenv").config();

const app = express();
app.use(cors());
app.use(express.json({ limit: "2mb" }));
app.use(express.static(path.join(__dirname, "public")));
app.use("/vendor", express.static(path.join(__dirname, "node_modules")));

const uploadsDir = path.join(__dirname, "uploads");
if (!fs.existsSync(uploadsDir)) fs.mkdirSync(uploadsDir, { recursive: true });

const upload = multer({
  dest: uploadsDir,
  limits: { fileSize: 100 * 1024 * 1024 }
});

function getDefaultCliPath() {
  const exe = process.platform === "win32" ? "StepMetricsCli.exe" : "StepMetricsCli";
  const candidates = [
    path.resolve(__dirname, "..", "build", "cli_engine", "Release", exe),
    path.resolve(__dirname, "..", "build", "cli_engine", "Debug", exe),
    path.resolve(__dirname, "..", "build_clean_occt", "cli_engine", "Release", exe),
    path.resolve(__dirname, "..", "build_clean_occt", "cli_engine", "Debug", exe),
    path.resolve(__dirname, "..", "build", "bin", exe)
  ];
  return candidates.find((p) => fs.existsSync(p)) || candidates[0];
}

function getCliPath() {
  return process.env.CLI_PATH && process.env.CLI_PATH.trim()
    ? process.env.CLI_PATH.trim()
    : getDefaultCliPath();
}

function ensureStepUpload(file) {
  if (!file) {
    const err = new Error("No file uploaded. Use form field 'stepFile'.");
    err.statusCode = 400;
    throw err;
  }

  const ext = path.extname(file.originalname || "").toLowerCase();
  if (ext !== ".step" && ext !== ".stp") {
    const err = new Error("Only .step/.stp files are allowed.");
    err.statusCode = 400;
    throw err;
  }
}

function cleanupFiles(...files) {
  for (const file of files.flat()) {
    if (!file) continue;
    fs.unlink(file, () => {});
  }
}

function runProcess(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    execFile(command, args, options, (err, stdout, stderr) => {
      if (err) {
        err.stdout = stdout;
        err.stderr = stderr;
        reject(err);
        return;
      }
      resolve({ stdout, stderr });
    });
  });
}

function parseDimension(rawValue, fallback) {
  const value = Number.parseInt(String(rawValue ?? fallback), 10);
  if (!Number.isFinite(value)) return fallback;
  return Math.max(64, Math.min(2048, value));
}

function sendProcessError(res, message, err, extra = {}) {
  return res.status(500).json({
    error: message,
    details: err.message,
    stderr: err.stderr,
    stdout: err.stdout,
    ...extra
  });
}

app.get("/api/health", (req, res) => {
  const cliPath = getCliPath();
  res.json({
    ok: true,
    cliPath,
    cliExists: fs.existsSync(cliPath)
  });
});

app.post("/api/analyze-step", upload.single("stepFile"), async (req, res) => {
  let inputPath;
  let outputPath;

  try {
    ensureStepUpload(req.file);
    inputPath = req.file.path;

    const cliPath = getCliPath();
    if (!fs.existsSync(cliPath)) {
      cleanupFiles(inputPath);
      return res.status(500).json({
        error: "StepMetricsCli not found. Set CLI_PATH in env.",
        cliPath
      });
    }

    outputPath = path.join(uploadsDir, `${req.file.filename}.json`);
    await runProcess(cliPath, [inputPath, outputPath], { timeout: 120000 });

    const text = await fs.promises.readFile(outputPath, "utf8");
    const parsed = JSON.parse(text);
    return res.json(parsed);
  } catch (err) {
    if (err.statusCode) {
      return res.status(err.statusCode).json({ error: err.message });
    }
    if (err instanceof SyntaxError) {
      return res.status(500).json({ error: "Invalid JSON returned by CLI", details: err.message });
    }
    return sendProcessError(res, "CLI execution failed", err, { cliPath: getCliPath() });
  } finally {
    cleanupFiles(inputPath, outputPath);
  }
});

app.post("/api/convert-step-to-glb", upload.single("stepFile"), async (req, res) => {
  let inputPath;
  let outputPath;

  try {
    ensureStepUpload(req.file);
    inputPath = req.file.path;

    const cliPath = getCliPath();
    if (!fs.existsSync(cliPath)) {
      cleanupFiles(inputPath);
      return res.status(500).json({
        error: "StepMetricsCli not found. Set CLI_PATH in env.",
        cliPath
      });
    }

    const baseName = path.parse(req.file.originalname || "model.step").name || "model";
    outputPath = path.join(uploadsDir, `${req.file.filename}.glb`);
    await runProcess(cliPath, ["--export-glb", inputPath, outputPath], { timeout: 240000 });

    res.setHeader("Content-Type", "model/gltf-binary");
    res.setHeader("Content-Disposition", `attachment; filename="${baseName}.glb"`);

    const stream = fs.createReadStream(outputPath);
    stream.on("close", () => cleanupFiles(inputPath, outputPath));
    stream.on("error", () => cleanupFiles(inputPath, outputPath));
    stream.pipe(res);
  } catch (err) {
    cleanupFiles(inputPath, outputPath);
    if (err.statusCode) {
      return res.status(err.statusCode).json({ error: err.message });
    }
    return sendProcessError(res, "GLB export failed", err, { cliPath: getCliPath() });
  }
});

app.post("/api/step-thumbnail", upload.single("stepFile"), async (req, res) => {
  let inputPath;
  let pngPath;

  try {
    ensureStepUpload(req.file);
    inputPath = req.file.path;

    const cliPath = getCliPath();
    if (!fs.existsSync(cliPath)) {
      cleanupFiles(inputPath);
      return res.status(500).json({
        error: "StepMetricsCli not found. Set CLI_PATH in env.",
        cliPath
      });
    }

    const width = parseDimension(req.body?.width, 500);
    const height = parseDimension(req.body?.height, 500);
    pngPath = path.join(uploadsDir, `${req.file.filename}.png`);

    await runProcess(cliPath, ["--thumbnail", inputPath, pngPath, String(width), String(height)], { timeout: 240000 });

    const image = await fs.promises.readFile(pngPath);
    res.setHeader("Content-Type", "image/png");
    res.setHeader("Cache-Control", "no-store");
    return res.send(image);
  } catch (err) {
    if (err.statusCode) {
      return res.status(err.statusCode).json({ error: err.message });
    }
    return sendProcessError(res, "Thumbnail generation failed", err, { cliPath: getCliPath() });
  } finally {
    cleanupFiles(inputPath, pngPath);
  }
});

const port = Number(process.env.PORT || 3000);
app.listen(port, () => {
  console.log(`Step Metrics Web App listening on http://localhost:${port}`);
  console.log(`CLI path: ${getCliPath()}`);
});
