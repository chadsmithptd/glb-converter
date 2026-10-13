#!/usr/bin/env python3
"""Acceptance gate for sldprt2step.

Two independent checks:

  (A) PACKAGING   the shipped tree contains zero compiled/binary artifacts and
                  zero non-standard-library imports, so it runs on a bare
                  Python 3.9 with no site-packages at all.

  (B) CORRECTNESS every .SLDPRT you point it at converts to a solid-bearing,
                  LF-only, ISO-10303-21 STEP file -- or fails with an honest
                  typed error code. `ok: true` over a STEP with no solid body
                  is always a failure.

Check (A) needs nothing but the package. Check (B) needs your own .SLDPRT
files; no sample parts ship with this package.

Usage:

    python3 -S -B tests/gate.py                 # (A) only
    python3 -S -B tests/gate.py parts/          # (A) + (B) over parts/*.SLDPRT
    python3 -S -B tests/gate.py parts/ more/    # several directories

`-S` disables site-packages, which is what makes (A) meaningful: if the
converter imports anything third-party, it cannot start. Nothing is written
outside a temporary scratch directory, and the inputs are never modified.

Exit 0 = PASS, 1 = FAIL, 2 = bad usage.
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

# A bare MANIFOLD_SOLID_BREP test is not enough: solids with internal cavities
# are written as BREP_WITH_VOIDS.
SOLID_MARKERS = ("MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS")

HONEST_ERRORS = {
    "empty_step", "no_parasolid_geometry", "unsupported_surface",
    "unsupported_curve", "unsupported_topology", "xt_parse_error",
    "input_not_found", "bad_input", "convert_failed",
}

BINARY_EXT = {".pyc", ".pyo", ".so", ".dll", ".dylib", ".pyd", ".o", ".a",
              ".exe", ".bin"}

# Third-party names that must never appear in a stdlib-only converter.
THIRD_PARTY = re.compile(
    r"^\s*(?:from|import)\s+"
    r"(numpy|scipy|OCP|ocp|cadquery|OCC|occ|pythonocc|shapely|trimesh|"
    r"lxml|pandas|numba|cython|Cython|pybind11|vtk|open3d)\b",
    re.MULTILINE,
)


def scan_packaging(roots):
    """Return (binary_artifacts, third_party_imports) found under `roots`."""
    bad_files, bad_imports = [], []
    for root in roots:
        for p in sorted(root.rglob("*")) if root.is_dir() else [root]:
            if p.is_dir():
                if p.name == "__pycache__":
                    bad_files.append((str(p), "__pycache__ dir"))
                continue
            if p.suffix.lower() in BINARY_EXT:
                bad_files.append((str(p), "binary extension %s" % p.suffix))
                continue
            raw = p.read_bytes()
            if b"\x00" in raw[:65536]:
                bad_files.append((str(p), "NUL byte (binary)"))
                continue
            if p.suffix == ".py":
                try:
                    txt = raw.decode("utf-8")
                except UnicodeDecodeError:
                    bad_files.append((str(p), "non-utf8 .py"))
                    continue
                for m in THIRD_PARTY.finditer(txt):
                    bad_imports.append((str(p), m.group(0).strip()))
    return bad_files, bad_imports


def convert_one(run_conversion, part: Path, scratch: Path):
    """Convert one part into scratch; return (result, size, solid, lf, iso)."""
    out = scratch / (part.stem + ".step")
    if out.exists():
        out.unlink()
    buf_out, buf_err = io.StringIO(), io.StringIO()
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf_out, buf_err
    try:
        res = run_conversion(str(part), str(out))
    except Exception as exc:  # noqa: BLE001 - a raise is itself the failure
        res = {"ok": False, "error": "CRASH:%s" % type(exc).__name__,
               "message": traceback.format_exc()[-400:], "warnings": []}
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
    step = out.read_text(errors="replace") if out.exists() else ""
    has_solid = any(m in step for m in SOLID_MARKERS)
    lf_only = ("\r" not in step) if step else True
    iso = step.startswith("ISO-10303-21;") if step else False
    if out.exists():
        out.unlink()
    return res, len(step), has_solid, lf_only, iso


def main(argv):
    part_dirs = []
    for a in argv[1:]:
        p = Path(a)
        if not p.is_dir():
            print("not a directory: %s" % a)
            return 2
        part_dirs.append(p)

    lib_root = PKG_ROOT / "sldprt2step_lib"
    cli_file = PKG_ROOT / "sldprt2step.py"
    if not lib_root.is_dir() or not cli_file.is_file():
        print("FATAL: run this from inside the package (expected %s)" % PKG_ROOT)
        return 2

    print("=" * 72)
    print("sldprt2step gate | python %s | site=%s" % (
        sys.version.split()[0],
        "DISABLED (-S)" if "site" not in sys.modules else "enabled"))
    print("package:", PKG_ROOT)
    print("=" * 72)

    fails = []

    # (A) packaging ---------------------------------------------------------
    bad_files, bad_imports = scan_packaging([lib_root, cli_file])
    print("\n[A] PACKAGING scan of sldprt2step.py + sldprt2step_lib/")
    print("    compiled / binary artifacts : %d" % len(bad_files))
    for f, why in bad_files[:20]:
        print("      X %s -> %s" % (why, f))
    print("    third-party imports         : %d" % len(bad_imports))
    for f, imp in bad_imports[:20]:
        print("      X %s -> %s" % (imp, f))
    if bad_files:
        fails.append("binary / compiled artifacts present (%d)" % len(bad_files))
    if bad_imports:
        fails.append("non-stdlib imports present (%d)" % len(bad_imports))

    try:
        from sldprt2step import run_conversion
    except Exception:
        traceback.print_exc()
        print("\nFATAL: the converter does not import on this runtime -- most"
              " likely a non-stdlib dependency crept in.")
        return 1
    print("    imports on this runtime     : OK")

    # (B) correctness -------------------------------------------------------
    parts = []
    for d in part_dirs:
        parts += sorted(d.glob("*.SLDPRT")) + sorted(d.glob("*.sldprt"))
    if not parts:
        print("\n[B] CORRECTNESS skipped -- no .SLDPRT directory given.")
        print("    Pass one or more directories of your own parts to run it.")
    else:
        scratch = Path(tempfile.mkdtemp(prefix="sldprt2step-gate-"))
        print("\n[B] CORRECTNESS over %d part(s)" % len(parts))
        print("    %-46s %-6s %-22s %s" % ("part", "ok", "error / solids", "step"))
        for part in parts:
            res, n, has_solid, lf_only, iso = convert_one(run_conversion, part, scratch)
            ok = bool(res.get("ok"))
            err = res.get("error", "-")
            nm = part.name[:44]
            if ok:
                detail = "solids=%s" % res.get("solids", "?")
                status = "OK" if (has_solid and lf_only and iso) else "BAD"
                if not has_solid:
                    fails.append("%s: ok:true but the STEP has no solid" % nm)
                if not lf_only:
                    fails.append("%s: STEP contains CR (not LF-only)" % nm)
                if not iso:
                    fails.append("%s: STEP missing the ISO-10303-21 header" % nm)
            else:
                detail = str(err)
                status = "ERR-OK" if err in HONEST_ERRORS else "ERR-BAD"
                if err not in HONEST_ERRORS:
                    fails.append("%s: untyped / unknown error %r" % (nm, err))
            print("    %-46s %-6s %-22s %7dB  %s" % (nm, ok, detail, n, status))
        try:
            os.rmdir(scratch)
        except OSError:
            pass

    print("\n" + "=" * 72)
    if fails:
        print("GATE: FAIL (%d)" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("GATE: PASS -- zero binaries, zero non-stdlib imports, and every part"
          " given was a solid or an honest typed error.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
