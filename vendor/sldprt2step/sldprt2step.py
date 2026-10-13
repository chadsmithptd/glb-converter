#!/usr/bin/env python3
"""sldprt2step — convert a .SLDPRT part file to ISO 10303-21 (STEP AP214).

Pure Python 3.9+, standard library only. Nothing to compile, no binaries, no
third-party packages, no network access.

    python3 sldprt2step.py part.SLDPRT -o part.step
    python3 sldprt2step.py part.SLDPRT -o part.step --json

The converter reads the OLE Structured Storage container, extracts the embedded
Parasolid XT transmit, walks its B-rep topology (body -> region -> shell -> face
-> loop -> fin -> edge -> vertex) and writes a solid-bearing STEP file.

Exit codes
----------
    0   success
    2   success, but approximations were made (see warnings on stderr)
    1   failure; the last stderr line is the error code, and the output file is
        never written (no partial or empty destination)

Error codes
-----------
    no_parasolid_geometry  the container holds no Parasolid XT stream
    empty_step             conversion produced no solid body
    unsupported_surface    a surface type this converter cannot map
    unsupported_curve      a curve type this converter cannot map
    unsupported_topology   a topology arrangement this converter cannot map
    xt_parse_error         the XT stream is malformed or truncated
    input_not_found        input missing or not a regular file
    bad_input              bad command line
    convert_failed         any other conversion failure

Library use
-----------
    from sldprt2step import run_conversion
    result = run_conversion("part.SLDPRT", "part.step")
    # -> {"ok": bool, "warnings": [...], "solids": int, "faces": int,
    #     "bodies": [...]} on success
    # -> {"ok": False, "error": "<code>", "message": "...", "warnings": [...]}
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SOLID_MARKERS = ("MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS")

__all__ = ["run_conversion", "main"]


def run_conversion(input_path: str, output_path: str) -> dict:
    """Convert one .SLDPRT to STEP. Never raises; returns a result dict."""
    if not os.path.isfile(input_path):
        return {"ok": False, "error": "input_not_found", "message": "input is not a regular file", "warnings": []}
    try:
        with open(input_path, "rb") as fh:
            buf = fh.read()
    except OSError as exc:
        return {"ok": False, "error": "input_not_found", "message": str(exc)[:300], "warnings": []}

    try:
        from sldprt2step_lib.step.convert import convert_sldprt_to_step
        from sldprt2step_lib.step.mapper import ConvertError
        from sldprt2step_lib.xt.reader import XtError
        from sldprt2step_lib.xt.geom import GeomError
    except Exception as exc:  # noqa: BLE001 - a broken install is a hard convert failure
        return {"ok": False, "error": "convert_failed", "message": "converter package is broken: %s" % str(exc)[:300], "warnings": []}

    try:
        result = convert_sldprt_to_step(buf, os.path.basename(input_path))
    except ConvertError as exc:
        return {"ok": False, "error": exc.code, "message": exc.message[:500], "warnings": []}
    except XtError as exc:
        return {"ok": False, "error": "xt_parse_error", "message": str(exc)[:500], "warnings": []}
    except GeomError as exc:
        return {"ok": False, "error": "convert_failed", "message": "geometry failure: %s" % str(exc)[:400], "warnings": []}
    except RecursionError:
        return {"ok": False, "error": "convert_failed", "message": "recursion limit reached while converting", "warnings": []}
    except MemoryError:
        return {"ok": False, "error": "convert_failed", "message": "out of memory while converting", "warnings": []}
    except Exception as exc:  # noqa: BLE001 - any other crash is a typed failure
        return {"ok": False, "error": "convert_failed", "message": "%s: %s" % (type(exc).__name__, str(exc)[:400]), "warnings": []}

    step = result.step
    if not isinstance(step, str) or not step:
        return {"ok": False, "error": "convert_failed", "message": "converter produced no STEP text", "warnings": list(result.warnings)}
    if result.solids <= 0 or not any(m in step for m in SOLID_MARKERS):
        return {"ok": False, "error": "empty_step", "message": "conversion produced no solid body", "warnings": list(result.warnings)}

    out_dir = os.path.dirname(output_path)
    if out_dir and not os.path.isdir(out_dir):
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": "convert_failed", "message": "cannot create output directory: %s" % str(exc)[:300], "warnings": list(result.warnings)}
    try:
        with open(output_path, "w", encoding="utf-8", newline="") as fh:  # LF only
            fh.write(step)
    except OSError as exc:
        return {"ok": False, "error": "convert_failed", "message": "cannot write STEP: %s" % str(exc)[:300], "warnings": list(result.warnings)}
    return {"ok": True, "warnings": list(result.warnings), "solids": result.solids, "faces": result.faces,
            "bodies": list(result.bodies)}


def main(argv):
    parser = argparse.ArgumentParser(
        prog="sldprt2step",
        description="Convert a .SLDPRT part file to ISO 10303-21 STEP (AP214).",
    )
    parser.add_argument("input", help="path to the .SLDPRT file")
    parser.add_argument("-o", "--out", required=True, help="path of the .step file to write")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print the result dict as one JSON object on stdout")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as exc:
        if not exc.code:  # -h / --help already printed usage; that is a success
            return 0
        sys.stderr.write("bad_input\n")
        sys.stderr.flush()
        return 1

    res = run_conversion(args.input, args.out)

    if args.as_json:
        sys.stdout.write(json.dumps(res, sort_keys=True) + "\n")
        sys.stdout.flush()
    else:
        for w in res.get("warnings", []):
            sys.stderr.write("warning: %s\n" % w)
        if res.get("ok"):
            sys.stderr.write("ok: %d solid(s), %d face(s)\n" % (res.get("solids", 0), res.get("faces", 0)))
        else:
            if res.get("message"):
                sys.stderr.write("%s\n" % res["message"])
            sys.stderr.write("%s\n" % res.get("error", "convert_failed"))
        sys.stderr.flush()

    if not res.get("ok"):
        return 1
    return 2 if res.get("warnings") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
