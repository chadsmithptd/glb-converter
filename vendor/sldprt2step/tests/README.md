# tests

Two stdlib-only scripts. No sample parts ship with this package — point them at
your own `.SLDPRT` files. Neither script modifies its inputs; both write only
into a temporary scratch directory and clean up after themselves.

## `gate.py` — acceptance gate

```
python3 -S -B tests/gate.py                 # (A) packaging only
python3 -S -B tests/gate.py path/to/parts   # (A) + (B) over parts/*.SLDPRT
python3 -S -B tests/gate.py dir1 dir2       # several directories
```

**(A) Packaging.** Zero compiled or binary artifacts (`.pyc`, `.so`, `.dylib`,
`.dll`, `.pyd`, `__pycache__`, anything with a NUL byte) and zero non-stdlib
imports anywhere in the shipped tree. Run it with `-S` — that disables
site-packages, so if a third-party dependency ever crept in, the converter
simply would not import.

**(B) Correctness.** Every part becomes a solid-bearing, LF-only,
`ISO-10303-21` STEP file, **or** it fails with an honest typed error code.
`ok: true` over a STEP with no solid body is always a failure. Note that a bare
`MANIFOLD_SOLID_BREP` check is not sufficient — solids with internal cavities
are written as `BREP_WITH_VOIDS`, and the gate accepts both.

Exit `0` = PASS, `1` = FAIL, `2` = bad usage.

## `fuzz.py` — robustness harness

```
python3 -B tests/fuzz.py                    # synthetic malformed inputs
python3 -B tests/fuzz.py part.SLDPRT        # + truncations and bit-flips
```

Builds a reproducible battery of malformed inputs — empty files, random bytes,
OLE headers with no payload, non-CAD files renamed `.sldprt`, and (given a seed
part) truncations at seven fractions plus twelve seeded single-bit flips. Each
case runs in a child process under a wall-clock budget, so a hang is reported
as a failure instead of stalling the run.

Every case must come back as a typed error with **no output file written**, or
as a genuine solid. A traceback, a timeout, an untyped error, an `ok` with zero
solids, or a failure that still left a file behind is a FAIL.

Bit-flips landing in compressed or metadata regions of a large part often still
convert cleanly — that is expected, and reported as `OK-SOLID`.
