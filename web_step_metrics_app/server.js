const fs = require("fs");
const path = require("path");
const { execFile } = require("child_process");
const express = require("express");
const cors = require("cors");
const multer = require("multer");
require("dotenv").config();
const { renderThumbnail } = require("./thumbnail-renderer");

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

app.get("/", (req, res) => {
  res.json({ ok: true, service: "glb-converter" });
});

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

const GLB_MAGIC = Buffer.from([0x67, 0x6c, 0x54, 0x46]); // 'glTF'

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

    // Validate output before sending — an HTML error page saved as .glb would
    // crash any GLTF loader with "Unexpected token '<'" on the client side.
    const glbBuffer = await fs.promises.readFile(outputPath);
    if (glbBuffer.length < 12 || !glbBuffer.slice(0, 4).equals(GLB_MAGIC)) {
      cleanupFiles(inputPath, outputPath);
      return res.status(500).json({
        error: "CLI produced invalid GLB output (bad magic bytes or empty file)",
        byteLength: glbBuffer.length
      });
    }

    res.setHeader("Content-Type", "model/gltf-binary");
    res.setHeader("Content-Disposition", `attachment; filename="${baseName}.glb"`);
    res.setHeader("Content-Length", glbBuffer.length);

    cleanupFiles(inputPath, outputPath);
    return res.end(glbBuffer);
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

  try {
    ensureStepUpload(req.file);
    inputPath = req.file.path;

    const width  = parseDimension(req.body?.width,  500);
    const height = parseDimension(req.body?.height, 500);

    const pngBuffer = await renderThumbnail(inputPath, width, height);

    res.setHeader("Content-Type", "image/png");
    res.setHeader("Cache-Control", "no-store");
    res.setHeader("Content-Length", pngBuffer.length);
    return res.send(pngBuffer);
  } catch (err) {
    if (err.statusCode) {
      return res.status(err.statusCode).json({ error: err.message });
    }
    return res.status(500).json({ error: "Thumbnail generation failed", details: err.message });
  } finally {
    cleanupFiles(inputPath);
  }
});

const port = Number(process.env.PORT || 3000);
app.listen(port, "0.0.0.0", () => {
  console.log(`Step Metrics Web App listening on port ${port}`);
  console.log(`CLI path: ${getCliPath()}`);
});
