import * as THREE from "three";
import { OrbitControls } from "/vendor/three/examples/jsm/controls/OrbitControls.js";

const form = document.getElementById("uploadForm");
const output = document.getElementById("output");
const statusEl = document.getElementById("status");
const downloadBtn = document.getElementById("downloadBtn");
const downloadGlbBtn = document.getElementById("downloadGlbBtn");
const generateThumbnailBtn = document.getElementById("generateThumbnailBtn");
const thumbnailModeButtons = Array.from(document.querySelectorAll("button[data-thumbnail-mode]"));
const thumbnailStage = document.getElementById("thumbnailStage");
const thumbnailImage = document.getElementById("thumbnailImage");
const thumbnailPlaceholder = document.getElementById("thumbnailPlaceholder");
const viewerEl = document.getElementById("viewer");
const fileInput = document.getElementById("stepFile");
const resetHighlightsBtn = document.getElementById("resetHighlightsBtn");
const faceFilterButtons = Array.from(document.querySelectorAll("button[data-face-filter]"));

let latestJson = null;
let latestThumbnailUrl = null;
let currentMesh = null;
let occtModulePromise = null;
let currentFaceFilter = "all";
let currentThumbnailMode = "occt";

const FACE_COLOR = {
  default: new THREE.Color(0x6a8db3),
  muted: new THREE.Color(0xc7d3df),
  planar: new THREE.Color(0xff7a00),
  cylindrical: new THREE.Color(0x0077ff),
  conical: new THREE.Color(0x9f2dff),
  other: new THREE.Color(0x0f766e)
};

const scene = new THREE.Scene();
scene.background = new THREE.Color(0xf4f7fb);

const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 100000);
camera.position.set(120, 80, 120);

const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
viewerEl.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

scene.add(new THREE.AmbientLight(0xffffff, 0.75));
const dir = new THREE.DirectionalLight(0xffffff, 0.9);
dir.position.set(80, 120, 60);
scene.add(dir);

const grid = new THREE.GridHelper(400, 24, 0x91a4b7, 0xc7d3df);
scene.add(grid);

function resizeViewer() {
  const w = Math.max(320, viewerEl.clientWidth);
  const h = Math.max(320, viewerEl.clientHeight);
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

window.addEventListener("resize", resizeViewer);
resizeViewer();

function animate() {
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(animate);
}
animate();

function setStatus(msg, isError = false) {
  statusEl.textContent = msg;
  statusEl.style.color = isError ? "#b00020" : "#1d6f42";
}

function setFaceFilterEnabled(enabled) {
  resetHighlightsBtn.disabled = !enabled;
  for (const btn of faceFilterButtons) {
    btn.disabled = !enabled;
  }
}

function setExportControlsEnabled(enabled) {
  downloadBtn.disabled = !enabled;
  downloadGlbBtn.disabled = !enabled;
  generateThumbnailBtn.disabled = !enabled;
}

function getThumbnailMode() {
  return currentThumbnailMode;
}

function setThumbnailMode(mode) {
  currentThumbnailMode = mode === "frontend" ? "frontend" : "occt";
  for (const btn of thumbnailModeButtons) {
    const isActive = btn.dataset.thumbnailMode === currentThumbnailMode;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-selected", String(isActive));
  }
}

function setFaceFilterActive(type) {
  currentFaceFilter = type;
  for (const btn of faceFilterButtons) {
    btn.classList.toggle("active", btn.dataset.faceFilter === type);
  }
}

function clearCurrentMesh() {
  if (!currentMesh) return;
  scene.remove(currentMesh);
  currentMesh.traverse((obj) => {
    if (obj.geometry) obj.geometry.dispose();
    if (obj.material) {
      if (Array.isArray(obj.material)) obj.material.forEach((m) => m.dispose());
      else obj.material.dispose();
    }
  });
  currentMesh = null;
  setFaceFilterEnabled(false);
}

function revokeThumbnailUrl() {
  if (!latestThumbnailUrl) return;
  URL.revokeObjectURL(latestThumbnailUrl);
  latestThumbnailUrl = null;
}

function clearThumbnail(message = "No thumbnail generated yet.") {
  revokeThumbnailUrl();
  thumbnailImage.hidden = true;
  thumbnailImage.removeAttribute("src");
  thumbnailStage.classList.add("empty");
  thumbnailPlaceholder.textContent = message;
}

function showThumbnail(blob) {
  revokeThumbnailUrl();
  latestThumbnailUrl = URL.createObjectURL(blob);
  thumbnailImage.src = latestThumbnailUrl;
  thumbnailImage.hidden = false;
  thumbnailStage.classList.remove("empty");
  thumbnailPlaceholder.textContent = "";
}

async function readErrorResponse(res, fallbackMessage) {
  const text = await res.text();
  if (!text) return fallbackMessage;

  try {
    const data = JSON.parse(text);
    return data.error || data.details || text || fallbackMessage;
  } catch {
    return text || fallbackMessage;
  }
}

function getOcctFactory() {
  const factory = window.occtimportjs || window.occtImportJs || window.occtImportJS;
  if (!factory || typeof factory !== "function") {
    throw new Error("occt-import-js was not loaded. Run npm install and restart the server.");
  }
  return factory;
}

async function getOcctModule() {
  if (!occtModulePromise) {
    const factory = getOcctFactory();
    occtModulePromise = factory({
      locateFile: (file) => `/vendor/occt-import-js/dist/${file}`
    });
  }
  return occtModulePromise;
}

function toArrayData(attr) {
  if (!attr) return null;
  if (ArrayBuffer.isView(attr)) return attr;
  if (Array.isArray(attr)) return attr;
  if (ArrayBuffer.isView(attr.array)) return attr.array;
  if (Array.isArray(attr.array)) return attr.array;
  if (ArrayBuffer.isView(attr.values)) return attr.values;
  if (Array.isArray(attr.values)) return attr.values;
  return null;
}

function splitTopLevelArgs(text) {
  const out = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === "(") depth += 1;
    else if (ch === ")") depth -= 1;
    else if (ch === "," && depth === 0) {
      out.push(text.slice(start, i).trim());
      start = i + 1;
    }
  }
  out.push(text.slice(start).trim());
  return out;
}

function parseEntityDef(def) {
  const m = /^([A-Z0-9_]+)\s*\((.*)\)$/s.exec(def.trim());
  if (!m) return null;
  return { type: m[1], args: m[2] };
}

function buildFaceTypeListFromStepText(stepText) {
  const normalized = stepText.replace(/\r/g, "\n");
  const entityDefs = new Map();

  let start = 0;
  for (let i = 0; i < normalized.length; i += 1) {
    if (normalized[i] !== ";") continue;
    const stmt = normalized.slice(start, i).trim();
    start = i + 1;

    if (!stmt.startsWith("#")) continue;
    const m = /^#(\d+)\s*=\s*(.*)$/s.exec(stmt);
    if (!m) continue;
    entityDefs.set(Number(m[1]), m[2].trim().toUpperCase());
  }

  const resolvedSurfaceType = new Map();
  function resolveSurfaceType(entityId, guard = new Set()) {
    if (resolvedSurfaceType.has(entityId)) return resolvedSurfaceType.get(entityId);
    if (guard.has(entityId)) return "other";
    guard.add(entityId);

    const def = entityDefs.get(entityId);
    if (!def) return "other";

    const parsed = parseEntityDef(def);
    if (!parsed) return "other";

    let faceType = "other";
    if (parsed.type === "PLANE") faceType = "planar";
    else if (parsed.type === "CYLINDRICAL_SURFACE") faceType = "cylindrical";
    else if (parsed.type === "CONICAL_SURFACE") faceType = "conical";
    else if (parsed.type === "SURFACE_REPLICA" || parsed.type === "RECTANGULAR_TRIMMED_SURFACE" || parsed.type === "OFFSET_SURFACE") {
      const refs = parsed.args.match(/#\d+/g) || [];
      if (refs.length > 0) {
        faceType = resolveSurfaceType(Number(refs[0].slice(1)), guard);
      }
    }

    resolvedSurfaceType.set(entityId, faceType);
    return faceType;
  }

  const faceTypes = [];
  for (const [, def] of entityDefs) {
    const parsed = parseEntityDef(def);
    if (!parsed || parsed.type !== "ADVANCED_FACE") continue;

    const args = splitTopLevelArgs(parsed.args);
    if (args.length < 3) {
      faceTypes.push("other");
      continue;
    }

    const surfaceRef = /#(\d+)/.exec(args[2]);
    if (!surfaceRef) {
      faceTypes.push("other");
      continue;
    }

    const surfId = Number(surfaceRef[1]);
    faceTypes.push(resolveSurfaceType(surfId));
  }

  return faceTypes;
}

function toFaceType(type) {
  if (type === "planar" || type === "cylindrical" || type === "conical") return type;
  return "other";
}

function buildFaceGroups(triCount, brepFaces, faceTypeList, faceIndexOffsetRef) {
  const groups = [];

  if (!Array.isArray(brepFaces) || brepFaces.length === 0) {
    groups.push({ start: 0, count: triCount * 3, faceType: "other" });
    return groups;
  }

  let cursor = 0;
  for (const face of brepFaces) {
    const first = Math.max(0, Number(face.first) || 0);
    const last = Math.max(first, Number(face.last) || first);

    if (first > cursor) {
      groups.push({ start: cursor * 3, count: (first - cursor) * 3, faceType: "other" });
    }

    const faceType = toFaceType(faceTypeList[faceIndexOffsetRef.value] || "other");
    faceIndexOffsetRef.value += 1;

    const countTriangles = Math.max(0, last - first + 1);
    groups.push({ start: first * 3, count: countTriangles * 3, faceType });
    cursor = last + 1;
  }

  if (cursor < triCount) {
    groups.push({ start: cursor * 3, count: (triCount - cursor) * 3, faceType: "other" });
  }

  return groups;
}

function createMaterial(faceType, filterType) {
  if (filterType === "none") {
    return new THREE.MeshStandardMaterial({
      color: FACE_COLOR.default,
      metalness: 0.08,
      roughness: 0.6
    });
  }

  if (filterType === "all") {
    return new THREE.MeshStandardMaterial({
      color: FACE_COLOR[faceType] || FACE_COLOR.other,
      metalness: 0.08,
      roughness: 0.6
    });
  }

  const isSelected = faceType === filterType;
  return new THREE.MeshStandardMaterial({
    color: isSelected ? (FACE_COLOR[faceType] || FACE_COLOR.other) : FACE_COLOR.muted,
    opacity: isSelected ? 1.0 : 0.2,
    transparent: !isSelected,
    depthWrite: isSelected,
    metalness: 0.08,
    roughness: 0.6
  });
}

function applyFaceHighlight(filterType) {
  if (!currentMesh) return;
  setFaceFilterActive(filterType);

  currentMesh.traverse((obj) => {
    if (!obj.isMesh || !obj.userData.faceGroups) return;

    const geometry = obj.geometry;
    geometry.clearGroups();

    const materials = [];
    for (const group of obj.userData.faceGroups) {
      const materialIndex = materials.length;
      materials.push(createMaterial(group.faceType, filterType));
      geometry.addGroup(group.start, group.count, materialIndex);
    }

    const oldMaterial = obj.material;
    if (Array.isArray(oldMaterial)) oldMaterial.forEach((m) => m.dispose());
    else if (oldMaterial) oldMaterial.dispose();

    obj.material = materials;
  });
}

function buildThreeObjectFromOcct(result, faceTypeList) {
  const root = new THREE.Group();
  const meshes = Array.isArray(result?.meshes) ? result.meshes : [];
  const faceOffsetRef = { value: 0 };

  for (const m of meshes) {
    const attrs = m?.attributes || {};
    const posData = toArrayData(attrs.position);
    if (!posData) continue;

    const position = posData instanceof Float32Array ? posData : new Float32Array(posData);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(position, 3));

    const normData = toArrayData(attrs.normal);
    if (normData) {
      const normal = normData instanceof Float32Array ? normData : new Float32Array(normData);
      geometry.setAttribute("normal", new THREE.BufferAttribute(normal, 3));
    } else {
      geometry.computeVertexNormals();
    }

    const idxData = toArrayData(m.index) || toArrayData(m.indices);
    if (idxData) {
      const index = idxData instanceof Uint32Array ? idxData : new Uint32Array(idxData);
      geometry.setIndex(new THREE.BufferAttribute(index, 1));
    }

    const triCount = geometry.index ? Math.floor(geometry.index.count / 3) : Math.floor(position.length / 9);
    const faceGroups = buildFaceGroups(triCount, m.brep_faces, faceTypeList, faceOffsetRef);
    geometry.clearGroups();

    const materials = [];
    for (const group of faceGroups) {
      const materialIndex = materials.length;
      materials.push(createMaterial(group.faceType, "all"));
      geometry.addGroup(group.start, group.count, materialIndex);
    }

    const mesh = new THREE.Mesh(geometry, materials);
    mesh.userData.faceGroups = faceGroups;
    root.add(mesh);
  }

  return root;
}

async function previewStep(file) {
  const arrayBuffer = await file.arrayBuffer();
  const occt = await getOcctModule();
  const result = occt.ReadStepFile(new Uint8Array(arrayBuffer), null);
  const stepText = new TextDecoder("utf-8").decode(arrayBuffer);
  const faceTypeList = buildFaceTypeListFromStepText(stepText);
  const model = buildThreeObjectFromOcct(result, faceTypeList);
  if (!model || model.children.length === 0) {
    throw new Error("No renderable meshes were produced from STEP.");
  }

  clearCurrentMesh();

  model.traverse((obj) => {
    if (obj.isMesh) {
      obj.castShadow = false;
      obj.receiveShadow = false;
    }
  });

  model.rotation.x = -Math.PI / 2;

  const box = new THREE.Box3().setFromObject(model);
  const center = box.getCenter(new THREE.Vector3());
  const dimensions = box.getSize(new THREE.Vector3());
  const orbitTarget = new THREE.Vector3(0, dimensions.y * 0.5, 0);
  const size = dimensions.length() || 1;

  model.position.set(-center.x, -box.min.y, -center.z);

  camera.position.set(size * 0.9, Math.max(size * 0.6, dimensions.y * 1.25), size * 0.9);
  camera.near = Math.max(0.01, size / 10000);
  camera.far = Math.max(1000, size * 20);
  camera.updateProjectionMatrix();
  controls.target.copy(orbitTarget);
  controls.update();

  scene.add(model);
  currentMesh = model;
  setFaceFilterEnabled(true);
  applyFaceHighlight(currentFaceFilter);
}

function getSelectedFile() {
  return fileInput.files?.[0] || null;
}

function ensureSelectedFile() {
  const stepFile = getSelectedFile();
  if (!stepFile) {
    throw new Error("Please choose a STEP file.");
  }
  return stepFile;
}

function createStepFormData(stepFile, extraFields = {}) {
  const fd = new FormData();
  fd.append("stepFile", stepFile);
  for (const [key, value] of Object.entries(extraFields)) {
    fd.append(key, String(value));
  }
  return fd;
}

function getThumbnailSize() {
  return { width: 500, height: 500 };
}


async function downloadGlb() {
  const stepFile = ensureSelectedFile();
  setStatus("Exporting GLB...");

  const res = await fetch("/api/convert-step-to-glb", {
    method: "POST",
    body: createStepFormData(stepFile)
  });

  if (!res.ok) {
    throw new Error(await readErrorResponse(res, "GLB export failed."));
  }

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const baseName = (stepFile.name || "model.step").replace(/\.(step|stp)$/i, "");
  const a = document.createElement("a");
  a.href = url;
  a.download = `${baseName || "model"}.glb`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  setStatus("GLB export complete.");
}

async function createFrontendThumbnail(width, height) {
  if (!currentMesh) {
    throw new Error("Analyze a STEP file before generating a Three.js thumbnail.");
  }

  const originalSize = new THREE.Vector2();
  renderer.getSize(originalSize);
  const originalAspect = camera.aspect;

  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  controls.update();
  renderer.render(scene, camera);

  const blob = await new Promise((resolve) => {
    renderer.domElement.toBlob(resolve, "image/png");
  });

  camera.aspect = originalAspect;
  camera.updateProjectionMatrix();
  renderer.setSize(originalSize.x, originalSize.y, false);
  controls.update();
  renderer.render(scene, camera);

  if (blob) return blob;

  const dataUrl = renderer.domElement.toDataURL("image/png");
  const response = await fetch(dataUrl);
  return response.blob();
}

async function generateThumbnail() {
  const stepFile = ensureSelectedFile();
  const { width, height } = getThumbnailSize();
  const mode = getThumbnailMode();
  setStatus(`Generating thumbnail from ${mode === "frontend" ? "Three.js" : "OCCT"}...`);

  let blob;
  if (mode === "frontend") {
    blob = await createFrontendThumbnail(width, height);
  } else {
    const res = await fetch("/api/step-thumbnail", {
      method: "POST",
      body: createStepFormData(stepFile, { width, height })
    });

    if (!res.ok) {
      throw new Error(await readErrorResponse(res, "Thumbnail generation failed."));
    }

    blob = await res.blob();
  }

  showThumbnail(blob);
  setStatus(`Thumbnail generated (${width} x ${height}) from ${mode === "frontend" ? "Three.js" : "OCCT"}.`);
}

for (const btn of faceFilterButtons) {
  btn.addEventListener("click", () => {
    applyFaceHighlight(btn.dataset.faceFilter || "all");
  });
}

for (const btn of thumbnailModeButtons) {
  btn.addEventListener("click", () => {
    setThumbnailMode(btn.dataset.thumbnailMode);
  });
}

setThumbnailMode(currentThumbnailMode);
resetHighlightsBtn.addEventListener("click", () => {
  applyFaceHighlight("none");
});
setFaceFilterActive("all");
clearThumbnail();

fileInput.addEventListener("change", () => {
  latestJson = null;
  output.textContent = "No output yet.";
  setExportControlsEnabled(false);
  clearThumbnail();
  setStatus("");
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  setStatus("Uploading and analyzing...");
  output.textContent = "";
  latestJson = null;
  setExportControlsEnabled(false);
  setFaceFilterEnabled(false);
  clearThumbnail();

  let stepFile;
  try {
    stepFile = ensureSelectedFile();
  } catch (err) {
    setStatus(err.message, true);
    return;
  }

  try {
    try {
      await previewStep(stepFile);
    } catch (previewErr) {
      setStatus(`Preview warning: ${previewErr.message}`, true);
    }

    const res = await fetch("/api/analyze-step", {
      method: "POST",
      body: createStepFormData(stepFile)
    });
    const data = await res.json();

    if (!res.ok) {
      output.textContent = JSON.stringify(data, null, 2);
      setStatus("Analysis failed", true);
      return;
    }

    latestJson = data;
    output.textContent = JSON.stringify(data, null, 2);
    setExportControlsEnabled(true);
    setStatus("Analysis complete.");
  } catch (err) {
    output.textContent = String(err);
    setStatus("Request failed", true);
  }
});

downloadBtn.addEventListener("click", () => {
  if (!latestJson) return;
  const blob = new Blob([JSON.stringify(latestJson, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "step-metrics-report.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
});

downloadGlbBtn.addEventListener("click", async () => {
  try {
    await downloadGlb();
  } catch (err) {
    setStatus(err.message || "GLB export failed.", true);
  }
});

generateThumbnailBtn.addEventListener("click", async () => {
  try {
    await generateThumbnail();
  } catch (err) {
    setStatus(err.message || "Thumbnail generation failed.", true);
  }
});

