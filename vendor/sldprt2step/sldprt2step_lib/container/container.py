"""SLDPRT container entry point: find every Parasolid transmit in a file.

Two storage formats exist:

* OLE2 compound files (SolidWorks <= 2014): the live model is the
  ``Config-N-Partition`` stream (GUID-wrapped partition transmit followed by a
  deltas transmit); ``Config-N`` and other streams hold feature/sketch bodies.
* "sw3d" flat sections (SolidWorks 2015+): zlib'd sections introduced by the
  marker 14 00 06 00 08 00; the partition section and feature sections contain
  the same GUID-wrapped blobs.

`extract_transmits` returns every decodable transmit with its kind
('partition' / 'part' / 'deltas') and, when present, the SolidWorks body name.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from typing import List, Optional

from . import blobs, ole_cfb, sw3d
from .. import jscompat

CONTAINER_FORMAT_OLE2 = "ole2"
CONTAINER_FORMAT_SW3D = "sw3d"


@dataclass
class Transmit:
    format: str          # ole2 | sw3d
    location: str        # stream name or section offset
    kind: str            # partition | part | deltas | other
    name: str            # SolidWorks body name when known
    data: bytes


@dataclass
class ParasolidExtractResult:
    """Backwards-compatible single-stream view (largest partition or part)."""
    format: str
    stream_name: str
    data: bytes


@dataclass
class Extraction:
    format: str
    transmits: List[Transmit] = field(default_factory=list)

    def partitions(self) -> List[Transmit]:
        return [t for t in self.transmits if t.kind == "partition"]

    def parts(self) -> List[Transmit]:
        return [t for t in self.transmits if t.kind == "part"]


def _sw3d_sections(buf: bytes):
    """Yield (offset, decompressed_bytes) for every inflatable sw3d section."""
    marker = sw3d.SW3D_SECTION_MARKER
    idx = 0
    n = len(buf)
    while True:
        idx = buf.find(marker, idx)
        if idx < 0 or idx + sw3d.SW3D_HEADER_SIZE > n:
            break
        csz = jscompat.read_u32le(buf, idx + 14)
        dsz = jscompat.read_u32le(buf, idx + 18)
        nl = jscompat.read_u32le(buf, idx + 22)
        if 0 < nl < 1024 and 4 < csz < n and 4 < dsz < 200_000_000:
            start = idx + sw3d.SW3D_HEADER_SIZE + nl
            end = start + csz
            if end <= n:
                try:
                    yield idx, zlib.decompress(buf[start:end], -15)
                except zlib.error:
                    pass
        idx += 1


def extract_transmits(buf: bytes) -> Optional[Extraction]:
    if ole_cfb.OleContainerParser.is_ole2(buf):
        ext = Extraction(CONTAINER_FORMAT_OLE2)
        try:
            parser = ole_cfb.OleContainerParser(buf)
        except Exception:
            return None
        for entry in parser.entries:
            if entry.type != 2 or entry.start_sector < 0 or entry.size <= 0:
                continue
            try:
                stream = parser.extract_stream(entry.name)
            except Exception:
                continue
            if stream is None or blobs.MARKER not in stream.data:
                continue
            for b in blobs.find_blobs(stream.data):
                ext.transmits.append(Transmit(CONTAINER_FORMAT_OLE2, entry.name, b.kind, b.name, b.data))
        return ext if ext.transmits else None

    ext = Extraction(CONTAINER_FORMAT_SW3D)
    for off, section in _sw3d_sections(buf):
        if blobs.MARKER not in section:
            continue
        for b in blobs.find_blobs(section):
            ext.transmits.append(Transmit(CONTAINER_FORMAT_SW3D, "section@%d" % off, b.kind, b.name, b.data))
    return ext if ext.transmits else None


def extract_parasolid(buf: bytes) -> Optional[ParasolidExtractResult]:
    """Largest partition transmit (or largest part transmit) - legacy helper."""
    ext = extract_transmits(buf)
    if ext is None:
        return None
    cands = ext.partitions() or ext.parts()
    if not cands:
        return None
    best = max(cands, key=lambda t: len(t.data))
    return ParasolidExtractResult(format=ext.format, stream_name=best.location, data=best.data)
