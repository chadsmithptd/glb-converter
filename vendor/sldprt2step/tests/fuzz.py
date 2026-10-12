#!/usr/bin/env python3
"""Robustness harness for sldprt2step.

A malformed input must come back as a typed error -- never a traceback, never a
hang, and never a partial output file. This builds a battery of malformed
inputs and asserts exactly that.

    python3 -B tests/fuzz.py                  # synthetic inputs only
    python3 -B tests/fuzz.py part.SLDPRT      # + truncations and bit-flips of
                                              #   a real part (much better cover)

Each case runs with a wall-clock budget in a child process, so a hang is
reported as a failure rather than stalling the run. Exit 0 = PASS, 1 = FAIL.
"""
from __future__ import annotations

import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
CLI = PKG_ROOT / "sldprt2step.py"

HONEST_ERRORS = {
    "empty_step", "no_parasolid_geometry", "unsupported_surface",
    "unsupported_curve", "unsupported_topology", "xt_parse_error",
    "input_not_found", "bad_input", "convert_failed",
}

TIMEOUT_SEC = 120
SEED = 20250821  # fixed: the battery is reproducible


def synthetic_cases():
    """(name, bytes) pairs that are not valid .SLDPRT parts."""
    ole_magic = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    return [
        ("empty", b""),
        ("one_byte", b"\x00"),
        ("text", b"this is a text file, not a part\n" * 40),
        ("ole_header_only", ole_magic + b"\x00" * 504),
        ("ole_header_garbage", ole_magic + os.urandom(4096)),
        ("ole_truncated_mid_fat", ole_magic + b"\x00" * 60 + b"\xff" * 200),
        ("zeros_64k", b"\x00" * 65536),
        ("random_64k", bytes(random.Random(SEED).getrandbits(8) for _ in range(65536))),
        ("zip_not_ole", b"PK\x03\x04" + b"\x00" * 2048),
        ("step_file", b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"),
    ]


def derived_cases(seed_part: Path):
    """Truncations and bit-flips of a real part -- the cases that find bugs."""
    raw = seed_part.read_bytes()
    rnd = random.Random(SEED)
    cases = []
    for frac in (0.01, 0.05, 0.25, 0.5, 0.75, 0.9, 0.99):
        n = max(1, int(len(raw) * frac))
        cases.append(("truncate_%d%%" % int(frac * 100), raw[:n]))
    cases.append(("head_zeroed", b"\x00" * 512 + raw[512:]))
    cases.append(("tail_chopped_1b", raw[:-1]))
    for i in range(12):
        buf = bytearray(raw)
        pos = rnd.randrange(len(buf))
        buf[pos] ^= 1 << rnd.randrange(8)
        cases.append(("bitflip_%02d@%d" % (i, pos), bytes(buf)))
    return cases


def run_case(name: str, data: bytes, scratch: Path):
    inp = scratch / "case.sldprt"
    out = scratch / "case.step"
    inp.write_bytes(data)
    if out.exists():
        out.unlink()
    cmd = [sys.executable, "-B", str(CLI), str(inp), "-o", str(out), "--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return name, "TIMEOUT", "exceeded %ds" % TIMEOUT_SEC, out.exists()
    wrote = out.exists()
    if wrote:
        out.unlink()
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return name, "NO-RESULT", (proc.stderr or "")[-160:], wrote
    import json
    try:
        res = json.loads(line[-1])
    except ValueError:
        return name, "BAD-JSON", line[-1][:160], wrote
    if res.get("ok"):
        # Succeeding on a mangled input is allowed only if a real solid came out.
        return name, "OK-SOLID" if res.get("solids", 0) > 0 else "OK-EMPTY", \
            "solids=%s" % res.get("solids"), wrote
    err = res.get("error")
    return name, ("TYPED" if err in HONEST_ERRORS else "UNTYPED"), str(err), wrote


def main(argv):
    if not CLI.is_file():
        print("FATAL: run this from inside the package (expected %s)" % CLI)
        return 2

    cases = synthetic_cases()
    if len(argv) > 1:
        seed_part = Path(argv[1])
        if not seed_part.is_file():
            print("not a file: %s" % argv[1])
            return 2
        cases += derived_cases(seed_part)
    else:
        print("note: no seed part given -- synthetic cases only. Pass a real"
              " .SLDPRT for truncation and bit-flip coverage.\n")

    scratch = Path(tempfile.mkdtemp(prefix="sldprt2step-fuzz-"))
    fails = []
    print("%-24s %-10s %-34s %s" % ("case", "verdict", "detail", "wrote_output"))
    print("-" * 84)
    for name, data in cases:
        nm, verdict, detail, wrote = run_case(name, data, scratch)
        print("%-24s %-10s %-34s %s" % (nm[:24], verdict, detail[:34], wrote))
        if verdict in ("TIMEOUT", "NO-RESULT", "BAD-JSON", "UNTYPED"):
            fails.append("%s: %s (%s)" % (nm, verdict, detail[:80]))
        if verdict == "OK-EMPTY":
            fails.append("%s: reported ok with zero solids" % nm)
        if verdict.startswith("TYPED") and wrote:
            fails.append("%s: failed but still wrote an output file" % nm)
    try:
        os.rmdir(scratch)
    except OSError:
        pass

    print("-" * 84)
    if fails:
        print("FUZZ: FAIL (%d of %d)" % (len(fails), len(cases)))
        for f in fails:
            print("  -", f)
        return 1
    print("FUZZ: PASS -- %d malformed inputs, all typed errors or real solids,"
          " no crash, no hang, no partial output." % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
