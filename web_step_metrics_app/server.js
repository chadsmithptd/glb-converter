const crypto = require("crypto");
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

// Shared-secret check for the conversion endpoints. When CONVERTER_API_KEY is set, every /api
// request except /api/health must send the same value in the X-Converter-Key header. Unset means
// open (the old behaviour), so callers can start sending the header before it is enforced.
function keysMatch(given, expected) {
  const a = Buffer.from(String(given || ""));
  const b = Buffer.from(expected);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

app.use("/api", (req, res, next) => {
  const expected = (process.env.CONVERTER_API_KEY || "").trim();
  if (!expected || req.path === "/health" || req.method === "OPTIONS") return next();
  if (keysMatch(req.get("X-Converter-Key"), expected)) return next();
  return res.status(401).json({ error: "Missing or invalid X-Converter-Key header." });
});
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
  let metricsPath;

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
    outputPath  = path.join(uploadsDir, `${req.file.filename}.glb`);
    metricsPath = path.join(uploadsDir, `${req.file.filename}.metrics.json`);

    // Run GLB export and geometry analysis concurrently against the same input file.
    const [glbResult, metricsResult] = await Promise.allSettled([
      runProcess(cliPath, ["--export-glb", inputPath, outputPath],  { timeout: 240000 }),
      runProcess(cliPath, [inputPath, metricsPath],                  { timeout: 120000 })
    ]);

    if (glbResult.status === "rejected") throw glbResult.reason;

    // Validate output before sending — an HTML error page saved as .glb would
    // crash any GLTF loader with "Unexpected token '<'" on the client side.
    const glbBuffer = await fs.promises.readFile(outputPath);
    if (glbBuffer.length < 12 || !glbBuffer.slice(0, 4).equals(GLB_MAGIC)) {
      cleanupFiles(inputPath, outputPath, metricsPath);
      return res.status(500).json({
        error: "CLI produced invalid GLB output (bad magic bytes or empty file)",
        byteLength: glbBuffer.length
      });
    }

    res.setHeader("Content-Type", "model/gltf-binary");
    res.setHeader("Content-Disposition", `attachment; filename="${baseName}.glb"`);
    res.setHeader("Content-Length", glbBuffer.length);

    // Attach geometry metrics as a header (best-effort — a metrics failure never
    // blocks the GLB download).
    if (metricsResult.status === "fulfilled") {
      try {
        const metricsText = await fs.promises.readFile(metricsPath, "utf8");
        const { geometry } = JSON.parse(metricsText);
        res.setHeader("Access-Control-Expose-Headers", "X-Part-Metrics");
        res.setHeader("X-Part-Metrics", JSON.stringify(geometry));
      } catch (_) { /* ignore */ }
    }

    cleanupFiles(inputPath, outputPath, metricsPath);
    return res.end(glbBuffer);
  } catch (err) {
    cleanupFiles(inputPath, outputPath, metricsPath);
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

    const width = parseDimension(req.body?.width, 500);

    const pngBuffer = await renderThumbnail(inputPath, width);

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

// SolidWorks part -> STEP via the vendored pure-Python sldprt2step (vendor/sldprt2step, Apache-2.0).
// It reads the Parasolid XT B-rep embedded in the .SLDPRT; no SolidWorks needed. The returned STEP
// is meant to be fed back through /api/convert-step-to-glb, /api/analyze-step, etc. like any other.
const SLDPRT2STEP_PATH = path.resolve(__dirname, "..", "vendor", "sldprt2step", "sldprt2step.py");
// Address-space cap: the library decompresses zlib sections without a size ceiling.
const SLDPRT_MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024;
// Blend-heavy parts are slow in pure Python; keep this under Xano's 180 s request timeout.
const SLDPRT_TIMEOUT_MS = 170000;

app.post("/api/convert-sldprt-to-step", upload.single("sldprtFile"), async (req, res) => {
  let inputPath;
  let workDir;

  try {
    if (!req.file) {
      return res.status(400).json({ error: "No file uploaded. Use form field 'sldprtFile'." });
    }
    inputPath = req.file.path;

    const ext = path.extname(req.file.originalname || "").toLowerCase();
    if (ext !== ".sldprt") {
      return res.status(400).json({ error: "Only .sldprt files are allowed." });
    }

    // The converter names the STEP product after the input file, so give it the original name
    // (sanitized) inside a per-request directory instead of multer's random one.
    const baseName = (path.parse(req.file.originalname).name || "part").replace(/[^A-Za-z0-9._-]/g, "_");
    workDir = await fs.promises.mkdtemp(path.join(uploadsDir, "sldprt-"));
    const namedInput = path.join(workDir, `${baseName}.SLDPRT`);
    const outputPath = path.join(workDir, `${baseName}.step`);
    await fs.promises.rename(inputPath, namedInput);
    inputPath = undefined;

    // Exit 0 = ok, 2 = ok with approximations, 1 = typed failure; --json prints the result either
    // way, so read stdout instead of relying on runProcess (which rejects on any non-zero exit).
    const { code, stdout, stderr, timedOut } = await new Promise((resolve) => {
      execFile(
        "prlimit",
        [`--as=${SLDPRT_MEMORY_LIMIT_BYTES}`, "--", "python3", "-I", "-S", "-B", SLDPRT2STEP_PATH, namedInput, "-o", outputPath, "--json"],
        { timeout: SLDPRT_TIMEOUT_MS, maxBuffer: 4 * 1024 * 1024 },
        (err, out, errOut) => resolve({
          code: err ? (typeof err.code === "number" ? err.code : -1) : 0,
          stdout: out,
          stderr: errOut,
          timedOut: Boolean(err && err.killed)
        })
      );
    });

    if (timedOut) {
      return res.status(422).json({ error: "SLDPRT conversion timed out", code: "timeout", message: `exceeded ${SLDPRT_TIMEOUT_MS / 1000} s` });
    }

    let result;
    try {
      result = JSON.parse(String(stdout).trim().split("\n").pop());
    } catch (_) {
      return res.status(500).json({ error: "sldprt2step returned no JSON result", exitCode: code, stderr: String(stderr).slice(-2000) });
    }

    if (!result.ok || (code !== 0 && code !== 2)) {
      return res.status(422).json({ error: "SLDPRT conversion failed", code: result.error || "convert_failed", message: result.message || null });
    }

    const stepBuffer = await fs.promises.readFile(outputPath);
    const warnings = Array.isArray(result.warnings) ? result.warnings : [];

    res.setHeader("Content-Type", "application/step");
    res.setHeader("Content-Disposition", `attachment; filename="${baseName}.step"`);
    res.setHeader("Content-Length", stepBuffer.length);
    // Small summary only (like X-Part-Metrics): warning texts can be long, so send kinds + count.
    res.setHeader("Access-Control-Expose-Headers", "X-Sldprt-Conversion");
    res.setHeader("X-Sldprt-Conversion", JSON.stringify({
      exit_code: code,
      solids: result.solids,
      faces: result.faces,
      warning_count: warnings.length,
      warning_kinds: warnings.map((w) => String(w).split(":")[0]).slice(0, 10)
    }));
    return res.end(stepBuffer);
  } catch (err) {
    return sendProcessError(res, "SLDPRT conversion failed", err);
  } finally {
    cleanupFiles(inputPath);
    if (workDir) fs.promises.rm(workDir, { recursive: true, force: true }).catch(() => {});
  }
});

// AutoCAD drawing (.dwg) -> PDF: LibreDWG's dwg2dxf (GPL-3.0, run as its own binary) converts the
// DWG to DXF, then dwg_render/render_dwg.py (ezdxf + matplotlib) draws one PDF page per sheet and
// lists the drawing's text. Both parse files from outside parties, so they run under resource limits
// (prlimit), as DWG_RENDER_UID when set, with a minimal environment (no converter secrets), in a
// per-request directory that is deleted afterwards. Neither needs the network.
const DWG2DXF_PATH = process.env.DWG2DXF_PATH || "dwg2dxf";
const DWG_PYTHON = process.env.DWG_PYTHON || "python3";
const DWG_RENDER_PATH = path.resolve(__dirname, "..", "dwg_render", "render_dwg.py");
const DWG_MAX_BYTES = 50 * 1024 * 1024;
const DWG_STEP_TIMEOUT_MS = 60000;
const DWG_MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024;
const DWG_FILE_SIZE_LIMIT_BYTES = 512 * 1024 * 1024;
const DWG_RENDER_UID = Number.parseInt(process.env.DWG_RENDER_UID || "", 10);
const DWG_RUN_AS = Number.isInteger(DWG_RENDER_UID) && process.getuid && process.getuid() === 0 ? DWG_RENDER_UID : null;
const HAS_PRLIMIT = fs.existsSync("/usr/bin/prlimit");

function runUntrusted(command, args, workDir) {
  const [cmd, cmdArgs] = HAS_PRLIMIT
    ? ["prlimit", [`--as=${DWG_MEMORY_LIMIT_BYTES}`, `--fsize=${DWG_FILE_SIZE_LIMIT_BYTES}`, "--", command, ...args]]
    : [command, args];
  const options = {
    cwd: workDir,
    timeout: DWG_STEP_TIMEOUT_MS,
    maxBuffer: 8 * 1024 * 1024,
    env: {
      PATH: process.env.PATH,
      HOME: workDir,
      LANG: "C.UTF-8",
      MPLCONFIGDIR: path.join(workDir, "mpl"),
      // numpy's bundled OpenBLAS reserves memory per CPU core at import; on a many-core host that
      // alone can exceed the prlimit address-space cap.
      OPENBLAS_NUM_THREADS: "1",
      OMP_NUM_THREADS: "1"
    }
  };
  if (DWG_RUN_AS !== null) {
    options.uid = DWG_RUN_AS;
    options.gid = DWG_RUN_AS;
  }
  return new Promise((resolve) => {
    execFile(cmd, cmdArgs, options, (err, stdout, stderr) => resolve({
      code: err ? (typeof err.code === "number" ? err.code : -1) : 0,
      stdout: String(stdout || ""),
      stderr: String(stderr || ""),
      timedOut: Boolean(err && err.killed)
    }));
  });
}

// POST multipart: dwgFile = the .dwg; output = "pdf" (default; the PDF body plus a small
// X-Dwg-Render summary header) or "text" (JSON {dxf_version, sheets, texts, text_truncated,
// audit_fixes} -- the drawing's own strings, too long for a header).
app.post("/api/convert-dwg-to-pdf", upload.single("dwgFile"), async (req, res) => {
  let inputPath;
  let workDir;

  try {
    if (!req.file) {
      return res.status(400).json({ error: "No file uploaded. Use form field 'dwgFile'." });
    }
    inputPath = req.file.path;

    const ext = path.extname(req.file.originalname || "").toLowerCase();
    if (ext !== ".dwg") {
      return res.status(400).json({ error: "Only .dwg files are allowed." });
    }
    if (req.file.size > DWG_MAX_BYTES) {
      return res.status(413).json({ error: `DWG larger than ${DWG_MAX_BYTES / 1024 / 1024} MB.` });
    }
    const output = String(req.body?.output || req.query.output || "pdf").toLowerCase();
    if (output !== "pdf" && output !== "text") {
      return res.status(400).json({ error: "output must be 'pdf' or 'text'." });
    }

    // Every DWG starts with its version string, AC1009 (R12) .. AC1032 (2018+).
    const handle = await fs.promises.open(inputPath, "r");
    const header = Buffer.alloc(6);
    await handle.read(header, 0, 6, 0);
    await handle.close();
    if (!/^AC1\d{3}$/.test(header.toString("latin1"))) {
      return res.status(422).json({ error: "Not a DWG file", code: "not_dwg" });
    }

    // Fixed file names inside the work dir: the customer's file name never reaches a command line.
    workDir = await fs.promises.mkdtemp(path.join(uploadsDir, "dwg-"));
    const dwgPath = path.join(workDir, "input.dwg");
    const dxfPath = path.join(workDir, "drawing.dxf");
    const pdfPath = path.join(workDir, "drawing.pdf");
    await fs.promises.rename(inputPath, dwgPath);
    inputPath = undefined;
    // matplotlib's font cache, prebuilt in the image, copied in per request: building it takes
    // seconds, and the shared copy stays read-only to the render user.
    const mplDir = path.join(workDir, "mpl");
    await fs.promises.mkdir(mplDir);
    if (process.env.DWG_MPLCONFIGDIR) {
      await fs.promises.cp(process.env.DWG_MPLCONFIGDIR, mplDir, { recursive: true });
    }
    if (DWG_RUN_AS !== null) {
      for (const p of [workDir, mplDir, ...(await fs.promises.readdir(mplDir)).map((f) => path.join(mplDir, f))]) {
        await fs.promises.chown(p, DWG_RUN_AS, DWG_RUN_AS);
      }
      await fs.promises.chmod(dwgPath, 0o644);
    }

    // dwg2dxf logs many recoverable errors and its exit code is unreliable either way; the DXF
    // it leaves behind is what decides.
    const toDxf = await runUntrusted(DWG2DXF_PATH, ["-y", "-o", dxfPath, dwgPath], workDir);
    if (toDxf.timedOut) {
      return res.status(422).json({ error: "DWG conversion timed out", code: "timeout" });
    }
    const dxfStat = await fs.promises.stat(dxfPath).catch(() => null);
    if (!dxfStat || dxfStat.size === 0) {
      return res.status(422).json({ error: "DWG could not be read", code: "dwg_unreadable", exitCode: toDxf.code, stderr: toDxf.stderr.split(workDir).join("").slice(-1000) });
    }

    const render = await runUntrusted(DWG_PYTHON, ["-I", "-B", DWG_RENDER_PATH, dxfPath, output === "text" ? "-" : pdfPath], workDir);
    if (render.timedOut) {
      return res.status(422).json({ error: "DWG rendering timed out", code: "timeout" });
    }
    let result;
    try {
      result = JSON.parse(render.stdout.trim().split("\n").pop());
    } catch (_) {
      return res.status(500).json({ error: "render_dwg returned no JSON result", exitCode: render.code, stderr: render.stderr.split(workDir).join("").slice(-2000) });
    }
    if (!result.ok) {
      return res.status(422).json({ error: "DWG rendering failed", code: result.error || "render_failed", message: result.message || null });
    }

    if (output === "text") {
      return res.json({
        dxf_version: result.dxf_version,
        sheets: result.sheets,
        texts: result.texts,
        text_truncated: result.text_truncated,
        audit_fixes: result.audit_fixes
      });
    }

    const pdfBuffer = await fs.promises.readFile(pdfPath);
    if (pdfBuffer.length < 5 || pdfBuffer.subarray(0, 5).toString("latin1") !== "%PDF-") {
      return res.status(500).json({ error: "Renderer produced no PDF" });
    }
    const baseName = (path.parse(req.file.originalname).name || "drawing").replace(/[^A-Za-z0-9._-]/g, "_");
    res.setHeader("Content-Type", "application/pdf");
    res.setHeader("Content-Disposition", `attachment; filename="${baseName}.pdf"`);
    res.setHeader("Content-Length", pdfBuffer.length);
    res.setHeader("Access-Control-Expose-Headers", "X-Dwg-Render");
    res.setHeader("X-Dwg-Render", JSON.stringify({
      dxf_version: result.dxf_version,
      sheet_count: result.sheets.length,
      text_count: result.texts.length,
      text_truncated: result.text_truncated
    }));
    return res.end(pdfBuffer);
  } catch (err) {
    return sendProcessError(res, "DWG conversion failed", err);
  } finally {
    cleanupFiles(inputPath);
    if (workDir) fs.promises.rm(workDir, { recursive: true, force: true }).catch(() => {});
  }
});

const port = Number(process.env.PORT || 3000);
app.listen(port, "0.0.0.0", () => {
  console.log(`Step Metrics Web App listening on port ${port}`);
  console.log(`CLI path: ${getCliPath()}`);
});
