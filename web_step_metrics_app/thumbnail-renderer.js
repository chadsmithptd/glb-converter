"use strict";

const fs   = require("fs");
const path = require("path");
const { PNG } = require("pngjs");

// ─── Math ────────────────────────────────────────────────────────────────────

function dot3(a, b)  { return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]; }
function sub3(a, b)  { return [a[0]-b[0], a[1]-b[1], a[2]-b[2]]; }
function cross3(a, b) {
  return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
}
function norm3(a) {
  const l = Math.sqrt(dot3(a, a));
  return l > 1e-12 ? [a[0]/l, a[1]/l, a[2]/l] : [0, 1, 0];
}

// Column-major 4×4: index = col*4 + row
function mat4Mul(a, b) {
  const m = new Float32Array(16);
  for (let col = 0; col < 4; col++)
    for (let row = 0; row < 4; row++) {
      let v = 0;
      for (let k = 0; k < 4; k++) v += a[k*4+row] * b[col*4+k];
      m[col*4+row] = v;
    }
  return m;
}

function transformPoint(m, p) {
  const x = m[0]*p[0] + m[4]*p[1] + m[8]*p[2]  + m[12];
  const y = m[1]*p[0] + m[5]*p[1] + m[9]*p[2]  + m[13];
  const z = m[2]*p[0] + m[6]*p[1] + m[10]*p[2] + m[14];
  const w = m[3]*p[0] + m[7]*p[1] + m[11]*p[2] + m[15];
  return [x/w, y/w, z/w];
}

function lookAtMat(eye, target, up) {
  const f = norm3(sub3(target, eye));
  const s = norm3(cross3(f, up));
  const u = cross3(s, f);
  const m = new Float32Array(16);
  m[0]=s[0]; m[1]=u[0]; m[2]=-f[0];
  m[4]=s[1]; m[5]=u[1]; m[6]=-f[1];
  m[8]=s[2]; m[9]=u[2]; m[10]=-f[2];
  m[12]=-dot3(s,eye); m[13]=-dot3(u,eye); m[14]=dot3(f,eye);
  m[15]=1;
  return m;
}

function perspectiveMat(fovY, aspect, near, far) {
  const f  = 1 / Math.tan(fovY * 0.5);
  const nf = 1 / (near - far);
  const m  = new Float32Array(16);
  m[0]  = f / aspect;
  m[5]  = f;
  m[10] = (far + near) * nf;
  m[11] = -1;
  m[14] = 2 * far * near * nf;
  return m;
}

// ─── Rasteriser ──────────────────────────────────────────────────────────────

// v0/v1/v2: [sx, sy, ndcZ]  |  shade: [r, g, b] 0-255
function rasterTri(color, depth, W, H, v0, v1, v2, shade) {
  const minX = Math.max(0,   Math.floor(Math.min(v0[0], v1[0], v2[0])));
  const maxX = Math.min(W-1, Math.ceil( Math.max(v0[0], v1[0], v2[0])));
  const minY = Math.max(0,   Math.floor(Math.min(v0[1], v1[1], v2[1])));
  const maxY = Math.min(H-1, Math.ceil( Math.max(v0[1], v1[1], v2[1])));
  if (minX > maxX || minY > maxY) return;

  const denom = (v1[1]-v2[1])*(v0[0]-v2[0]) + (v2[0]-v1[0])*(v0[1]-v2[1]);
  if (Math.abs(denom) < 0.5) return;

  const [r, g, b] = shade;
  for (let py = minY; py <= maxY; py++) {
    for (let px = minX; px <= maxX; px++) {
      const w0 = ((v1[1]-v2[1])*(px-v2[0]) + (v2[0]-v1[0])*(py-v2[1])) / denom;
      const w1 = ((v2[1]-v0[1])*(px-v2[0]) + (v0[0]-v2[0])*(py-v2[1])) / denom;
      const w2 = 1 - w0 - w1;
      if (w0 < 0 || w1 < 0 || w2 < 0) continue;

      const z = w0*v0[2] + w1*v1[2] + w2*v2[2];
      const i = py*W + px;
      if (z >= depth[i]) continue;
      depth[i] = z;

      const p = i * 4;
      color[p] = r; color[p+1] = g; color[p+2] = b; color[p+3] = 255;
    }
  }
}

// ─── OCCT module (cached across requests) ────────────────────────────────────

let occtPromise = null;

async function getOcctModule() {
  if (!occtPromise) {
    const factory = require("occt-import-js");
    const pkgDir  = path.dirname(require.resolve("occt-import-js/package.json"));
    occtPromise   = factory({ locateFile: (f) => path.join(pkgDir, "dist", f) });
  }
  return occtPromise;
}

// Mirrors the frontend's toArrayData — handles TypedArrays, plain arrays,
// and objects with .array/.values (Three.js BufferAttribute shape).
function toArray(attr) {
  if (!attr) return null;
  if (ArrayBuffer.isView(attr))        return attr;
  if (Array.isArray(attr))             return attr;
  if (ArrayBuffer.isView(attr.array))  return attr.array;
  if (Array.isArray(attr.array))       return attr.array;
  if (ArrayBuffer.isView(attr.values)) return attr.values;
  if (Array.isArray(attr.values))      return attr.values;
  return null;
}

// Rotate -90° around X: converts OCCT/STEP Z-up → Y-up (matches frontend rotation.x = -π/2)
function rotX90(x, y, z) { return [x, z, -y]; }

// ─── Main export ─────────────────────────────────────────────────────────────

async function renderThumbnail(stepFilePath, width) {
  const height = Math.round(width * 3 / 4);
  const occt   = await getOcctModule();
  const result = occt.ReadStepFile(new Uint8Array(fs.readFileSync(stepFilePath)), null);

  const meshes = Array.isArray(result?.meshes) ? result.meshes : [];
  if (meshes.length === 0) throw new Error("No meshes produced from STEP file");

  // ── Collect triangles + bounding box ────────────────────────────────────────

  let minX=Infinity, minY=Infinity, minZ=Infinity;
  let maxX=-Infinity, maxY=-Infinity, maxZ=-Infinity;
  const tris = [];

  for (const mesh of meshes) {
    const attrs = mesh?.attributes || {};
    const pos   = toArray(attrs.position);
    if (!pos) continue;
    const nor   = toArray(attrs.normal);
    const idx   = toArray(mesh.index) || toArray(mesh.indices);
    const count = idx ? Math.floor(idx.length / 3) : Math.floor(pos.length / 9);

    for (let t = 0; t < count; t++) {
      const i0 = idx ? idx[t*3]   : t*3;
      const i1 = idx ? idx[t*3+1] : t*3+1;
      const i2 = idx ? idx[t*3+2] : t*3+2;

      const p0 = rotX90(pos[i0*3], pos[i0*3+1], pos[i0*3+2]);
      const p1 = rotX90(pos[i1*3], pos[i1*3+1], pos[i1*3+2]);
      const p2 = rotX90(pos[i2*3], pos[i2*3+1], pos[i2*3+2]);

      for (const p of [p0, p1, p2]) {
        if (p[0] < minX) minX=p[0]; if (p[0] > maxX) maxX=p[0];
        if (p[1] < minY) minY=p[1]; if (p[1] > maxY) maxY=p[1];
        if (p[2] < minZ) minZ=p[2]; if (p[2] > maxZ) maxZ=p[2];
      }

      let n;
      if (nor) {
        const n0 = rotX90(nor[i0*3], nor[i0*3+1], nor[i0*3+2]);
        const n1 = rotX90(nor[i1*3], nor[i1*3+1], nor[i1*3+2]);
        const n2 = rotX90(nor[i2*3], nor[i2*3+1], nor[i2*3+2]);
        n = norm3([(n0[0]+n1[0]+n2[0])/3, (n0[1]+n1[1]+n2[1])/3, (n0[2]+n1[2]+n2[2])/3]);
      } else {
        n = norm3(cross3(sub3(p1, p0), sub3(p2, p0)));
      }

      tris.push({ p0, p1, p2, n });
    }
  }

  if (tris.length === 0) throw new Error("No renderable triangles");

  // ── Camera ──────────────────────────────────────────────────────────────────

  const cx = (minX+maxX)/2;
  const cy = (minY+maxY)/2;
  const cz = (minZ+maxZ)/2;
  const dx = maxX-minX, dy = maxY-minY, dz = maxZ-minZ;

  // Bounding-sphere radius — guarantees the whole part fits regardless of shape
  const bsRadius = Math.sqrt(dx*dx + dy*dy + dz*dz) / 2 || 1;

  const fovY   = 45 * Math.PI / 180;
  const aspect = width / height;
  const fovX   = 2 * Math.atan(Math.tan(fovY / 2) * aspect);

  // Exact distance so the bounding sphere just fits inside both FOV axes, +8% margin
  const fitDist = Math.max(bsRadius / Math.tan(fovY / 2), bsRadius / Math.tan(fovX / 2)) * 1.08;

  // Camera direction is unnormalised [1, 0.816, 1]; scale so ‖eye−center‖ = fitDist
  const dirLen   = Math.sqrt(1 + 0.816*0.816 + 1);
  const distParam = fitDist / dirLen;

  // Fixed 45° azimuth, ~35° elevation — consistent isometric-like angle
  const eye    = [cx + distParam, cy + distParam * 0.816, cz + distParam];
  const target = [cx, cy, cz];
  const mvp    = mat4Mul(
    perspectiveMat(fovY, aspect, fitDist * 0.01, fitDist * 10),
    lookAtMat(eye, target, [0, 1, 0])
  );

  const lightDir = norm3([0.6, 0.9, 0.7]);

  // ── Buffers (2× supersampling — render at double resolution, downsample after) ─

  const W = width  * 2;
  const H = height * 2;

  const color = new Uint8Array(W * H * 4);
  const depth = new Float32Array(W * H).fill(1); // NDC far = 1

  // Background #ffffff
  for (let i = 0, n = W * H; i < n; i++) {
    const p = i * 4;
    color[p] = 255; color[p+1] = 255; color[p+2] = 255; color[p+3] = 255;
  }

  // ── Rasterise ────────────────────────────────────────────────────────────────

  // NDC → pixels at 2× resolution; Y inverted (NDC +1 = top → row 0)
  function toScreen([nx, ny, nz]) {
    return [(nx*0.5+0.5)*W, (0.5-ny*0.5)*H, nz];
  }

  const [BR, BG, BB] = [157, 157, 157]; // #9D9D9D

  for (const { p0, p1, p2, n } of tris) {
    const sp0 = toScreen(transformPoint(mvp, p0));
    const sp1 = toScreen(transformPoint(mvp, p1));
    const sp2 = toScreen(transformPoint(mvp, p2));

    // Back-face cull: in screen-Y-down space, front faces wind CW (negative signed area)
    const ex = sp1[0]-sp0[0], ey = sp1[1]-sp0[1];
    const fx = sp2[0]-sp0[0], fy = sp2[1]-sp0[1];
    if (ex*fy - ey*fx >= 0) continue;

    const diff = Math.max(0, dot3(n, lightDir));
    const lit  = Math.min(1, 0.4 + 0.65 * diff);
    rasterTri(color, depth, W, H, sp0, sp1, sp2,
      [Math.round(BR*lit), Math.round(BG*lit), Math.round(BB*lit)]);
  }

  // ── Downsample 2×2 → 1 pixel ─────────────────────────────────────────────────

  const final = new Uint8Array(width * height * 4);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      let r=0, g=0, b=0, a=0;
      for (let sy = 0; sy < 2; sy++) {
        for (let sx = 0; sx < 2; sx++) {
          const s = ((y*2+sy) * W + (x*2+sx)) * 4;
          r += color[s]; g += color[s+1]; b += color[s+2]; a += color[s+3];
        }
      }
      const d = (y * width + x) * 4;
      final[d] = r>>2; final[d+1] = g>>2; final[d+2] = b>>2; final[d+3] = a>>2;
    }
  }

  // ── Encode PNG ───────────────────────────────────────────────────────────────

  const png = new PNG({ width, height });
  png.data = Buffer.from(final);
  return PNG.sync.write(png);
}

module.exports = { renderThumbnail };
