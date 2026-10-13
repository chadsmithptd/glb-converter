# sldprt2step

Convert **`.SLDPRT`** part files to **ISO 10303-21 (STEP AP214)** solid
B-reps — without the CAD application that wrote them, without a CAD kernel,
and without leaving the machine.

Pure Python 3.9+, **standard library only**. Nothing to compile, no binaries,
no third-party packages, no network access. It runs on a stock system
`python3` (including macOS's `/usr/bin/python3`, 3.9.6) with no
`site-packages` at all.

```
python3 sldprt2step.py part.SLDPRT -o part.step
```

## What it actually does

It is a real **Parasolid XT neutral-binary reader**, not a mesh dump or a
preview extractor.

1. Parses the `.SLDPRT` [MS-CFB] OLE Structured Storage container and locates
   the SolidWorks 3D partition holding the geometry stream.
2. Decompresses and parses the embedded Parasolid XT transmit (base schema
   `SCH_13006` plus the embedded edit scripts).
3. Walks the B-rep topology — `BODY → REGION → SHELL → FACE → LOOP → FIN →
   EDGE → VERTEX` — and emits `MANIFOLD_SOLID_BREP` / `BREP_WITH_VOIDS` solids
   built from `ADVANCED_FACE`s.

Analytic geometry is mapped **exactly**: plane, cylinder, cone, sphere, torus,
B-spline surface, swept, spun and offset surfaces; line, circle, ellipse and
B-spline curves. Geometry that STEP cannot represent exactly — surface/surface
intersection curves, SP curves, rolling-ball blends — is approximated by
interpolating B-splines through the XT chart points, and every approximation is
reported in `warnings`.

Output is deterministic: no timestamp is written, so the same input always
produces byte-identical STEP.

## Install

There is nothing to install. Drop the directory somewhere and run it, or
`import` it:

```
sldprt2step/
├── sldprt2step.py          CLI + `run_conversion()`
├── sldprt2step_lib/        the converter
│   ├── container/          OLE/CFB reader, SolidWorks 3D partition, blobs
│   ├── xt/                 Parasolid XT schema, reader, geometry
│   └── step/               topology mapper + STEP writer
└── tests/                  acceptance gate and robustness harness
```

## Use

### Command line

```
python3 sldprt2step.py part.SLDPRT -o part.step
python3 sldprt2step.py part.SLDPRT -o part.step --json
```

| Exit | Meaning |
|---|---|
| `0` | success |
| `2` | success, but approximations were made — see `warnings` |
| `1` | failure; the last stderr line is the error code |

On failure the output file is **never** written — there is no partial or empty
destination to clean up. `--json` prints the full result as one JSON object on
stdout instead of the human-readable stderr lines.

### As a library

```python
from sldprt2step import run_conversion

res = run_conversion("part.SLDPRT", "part.step")
if res["ok"]:
    print(res["solids"], "solid(s),", res["faces"], "face(s)")
    for w in res["warnings"]:
        print("approximation:", w)
else:
    print("failed:", res["error"], res.get("message"))
```

`run_conversion()` never raises — every failure comes back as a typed
`error` code.

### Error codes

| Code | Meaning |
|---|---|
| `no_parasolid_geometry` | the container holds no Parasolid XT stream |
| `empty_step` | conversion produced no solid body |
| `unsupported_surface` | a surface type this converter cannot map |
| `unsupported_curve` | a curve type this converter cannot map |
| `unsupported_topology` | a topology arrangement this converter cannot map |
| `xt_parse_error` | the XT stream is malformed or truncated |
| `input_not_found` | input missing or not a regular file |
| `bad_input` | bad command line |
| `convert_failed` | any other conversion failure |

A conversion that yields no solid body is a **failure**, not a warning. The
converter re-reads the bytes it wrote and refuses to report success over a STEP
file with no solid in it.

## Tests

No sample parts ship with this package — bring your own `.SLDPRT` files.

```
python3 -S -B tests/gate.py                # packaging checks only
python3 -S -B tests/gate.py path/to/parts  # + convert every part in the dir
python3 -B  tests/fuzz.py                  # synthetic malformed inputs
python3 -B  tests/fuzz.py part.SLDPRT      # + truncations and bit-flips
```

`tests/gate.py` asserts two things: the tree has zero compiled artifacts and
zero non-stdlib imports (running it under `-S`, with site-packages disabled, is
what proves it), and every part you give it becomes a solid-bearing, LF-only,
ISO-10303-21 STEP file *or* an honest typed error.

`tests/fuzz.py` asserts that malformed input never crashes, never hangs and
never leaves a partial file behind.

## Scope and limits

- **Parts only.** `.SLDASM` assemblies and `.SLDDRW` drawings are not
  supported.
- **Solid geometry only.** Sketches, features, the design tree, mates,
  configurations, materials, appearances, custom properties and PMI/MBD
  annotation are not carried over. What you get is the final B-rep.
- **Partial conversion by design.** Blend and intersection geometry is
  approximated (and reported); the result is dimensionally faithful within
  spline tolerance, not a bit-exact kernel round-trip. Always inspect the
  output before relying on it downstream.
- **Blend-heavy parts are slow.** Rolling-ball blend reconstruction is millions
  of NURBS operations in pure Python; a part with many fillet faces can take
  over a minute. Correct, just not fast.
- **Uncapped decompression.** The container's zlib streams are decompressed
  without a size ceiling, so a deliberately crafted "zip bomb" part could
  exhaust memory. This is not reachable from any normal SolidWorks file, but if
  you feed this untrusted input at scale, run it under a memory limit.

## Licence

Apache License 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

This is a clean-room implementation for interoperability, written from public
specifications. It contains no proprietary source, headers, SDKs or API
documentation from any CAD vendor. SolidWorks® is a registered trademark of
Dassault Systèmes SolidWorks Corporation; Parasolid® is a registered trademark
of Siemens Industry Software Inc. Both are used here for identification only.
