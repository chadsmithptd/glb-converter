"""SLDPRT bytes -> STEP text, with typed failures and warnings.

Selection of the live geometry:

1. Every ``partition`` transmit is parsed; the WORLD's body chain gives the
   live bodies. The partition with the most faces wins (SolidWorks keeps an
   empty/initial partition blob next to the real one).
2. If no partition holds a solid body, the standalone ``part`` transmits are
   used instead - this is how SolidWorks stores imported bodies (feature
   ``Imported1`` -> bodies ``Imported1..N``) when the partition is empty.
   Only solid bodies are taken and a warning records the fallback.

Every failure is a ConvertError(code, message) with a lower_snake code:
no_parasolid_geometry, empty_step, unsupported_surface, unsupported_curve,
unsupported_topology, xt_parse_error.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..container.container import Transmit, extract_transmits
from ..xt.reader import Node, XtError, XtFile, read_xt
from .mapper import ConvertError, write_step


@dataclass
class ConversionResult:
    step: str
    warnings: List[str] = field(default_factory=list)
    solids: int = 0
    faces: int = 0
    bodies: List[str] = field(default_factory=list)
    containerFormat: str = ""
    modellerVersion: int = 0


def _bodies_of(xt: XtFile) -> List[Node]:
    root = xt.get(xt.root_index)
    if root is None:
        return []
    if root.type == 101:  # WORLD (partition)
        return xt.chain(xt.deref(root, "body"), "next")
    if root.type == 12:   # BODY (part transmit)
        return [root]
    if root.type in (74, 176):  # pointer list / part block roots
        out: List[Node] = []
        for p in root.f.get("entries", []) or []:
            n = xt.get(p)
            if n is not None and n.type == 12:
                out.append(n)
        return out
    return []


def _face_count(xt: XtFile, body: Node) -> int:
    n = 0
    for region in xt.chain(xt.deref(body, "region"), "next"):
        for sh in xt.chain(xt.deref(region, "shell"), "next"):
            n += len(xt.chain(xt.deref(sh, "face"), "next"))
            n += len(xt.chain(xt.deref(sh, "front_face"), "next_front"))
    return n


def _parse(t: Transmit) -> Tuple[Optional[XtFile], Optional[str]]:
    try:
        return read_xt(t.data), None
    except XtError as exc:
        return None, str(exc)


def convert_sldprt_to_step(buf: bytes, file_name: str = "output.stp") -> ConversionResult:
    ext = extract_transmits(buf)
    if ext is None:
        raise ConvertError("no_parasolid_geometry", "no Parasolid transmit found in the file (not a SolidWorks part, or an unsupported storage layout)")
    warnings: List[str] = []
    parse_errors: List[str] = []

    # 1. partitions
    best: Optional[Tuple[int, XtFile, List[Node], Transmit]] = None
    for t in ext.partitions():
        xt, err = _parse(t)
        if xt is None:
            parse_errors.append("%s: %s" % (t.location, err))
            continue
        bodies = _bodies_of(xt)
        faces = sum(_face_count(xt, b) for b in bodies)
        if best is None or faces > best[0]:
            best = (faces, xt, bodies, t)

    chosen: List[Tuple[XtFile, Node, str]] = []
    modeller = 0
    if best is not None and best[0] > 0:
        _, xt, bodies, t = best
        modeller = xt.modeller_version
        solid_bodies = [b for b in bodies if b.f.get("body_type") == 1]
        if not solid_bodies:
            kinds = sorted({ {2: "wire", 3: "sheet", 6: "general"}.get(b.f.get("body_type"), str(b.f.get("body_type"))) for b in bodies})
            raise ConvertError("empty_step", "the part's live partition holds %d bod%s but none is a solid (body types: %s)"
                               % (len(bodies), "y" if len(bodies) == 1 else "ies", ", ".join(kinds)))
        if len(solid_bodies) < len(bodies):
            warnings.append("non_solid_bodies: %d non-solid bod%s (sheet/wire) skipped" % (len(bodies) - len(solid_bodies), "y" if len(bodies) - len(solid_bodies) == 1 else "ies"))
        for i, b in enumerate(solid_bodies):
            chosen.append((xt, b, "Body%d" % (i + 1) if len(solid_bodies) > 1 else "Body"))
    else:
        # 2. fall back to standalone part transmits (imported bodies)
        parts = []
        for t in ext.parts():
            xt, err = _parse(t)
            if xt is None:
                parse_errors.append("%s: %s" % (t.location, err))
                continue
            for b in _bodies_of(xt):
                if b.f.get("body_type") == 1 and _face_count(xt, b) > 0:
                    parts.append((xt, b, t))
        if not parts:
            if best is not None:
                detail = "the live partition is empty (no bodies)"
            elif parse_errors:
                detail = "no partition could be parsed (%s)" % parse_errors[0][:200]
            else:
                detail = "no partition transmit found"
            if parse_errors and best is not None:
                detail += "; %d transmit(s) failed to parse: %s" % (len(parse_errors), parse_errors[0][:200])
            raise ConvertError("empty_step" if best is not None else "xt_parse_error",
                               "%s and no solid feature bodies exist to fall back on" % detail)
        named = [t for _, _, t in parts if t.name]
        for i, (xt, b, t) in enumerate(parts):
            chosen.append((xt, b, t.name or "Body%d" % (i + 1)))
            modeller = modeller or xt.modeller_version
        warnings.append("imported_bodies: live partition is empty; %d solid bod%s taken from feature data (%s)"
                        % (len(parts), "y" if len(parts) == 1 else "ies", ", ".join(n.name for n in named[:16]) or "unnamed"))

    text, solids, faces = write_step(chosen, file_name, warnings)
    if solids == 0:
        raise ConvertError("empty_step", "conversion produced no solids")
    return ConversionResult(step=text, warnings=warnings, solids=solids, faces=faces,
                            bodies=[n for _, _, n in chosen], containerFormat=ext.format,
                            modellerVersion=modeller)
