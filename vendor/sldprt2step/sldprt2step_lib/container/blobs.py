"""SolidWorks GUID-wrapped Parasolid blobs.

Both storage flavours (OLE2 streams such as ``Config-N-Partition`` and the
post-2015 "sw3d" sections) wrap each Parasolid transmit in the same record:

    u32 blob_size | 16-byte marker 231dd571-da81-48a2-a858-98b21b89ef99 |
    u32 decompressed_size | u32 compressed_size | zlib data
    ( u32 decompressed_size | u32 compressed_size | zlib data )*   -- continuation chunks

A blob may be preceded (within ~200 bytes) by a UTF-16 name record
``ff fe ff <len> <utf16>`` such as ``Imported3``; that is the SolidWorks body
name for imported bodies.
"""
from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass
from typing import List, Optional

MARKER = bytes.fromhex("231dd571da8148a2a85898b21b89ef99")
_NAME_RE = re.compile(rb"\xff\xfe\xff(.)", re.DOTALL)


@dataclass
class Blob:
    offset: int
    name: str
    data: bytes

    @property
    def kind(self) -> str:
        """'partition' | 'part' | 'deltas' | 'other'."""
        head = self.data[:80]
        if not head.startswith(b"PS"):
            return "other"
        if b"(partition)" in head:
            return "partition"
        if b"(deltas)" in head:
            return "deltas"
        if b"TRANSMIT FILE created" in head:
            return "part"
        return "other"


def _inflate_chunks(data: bytes, pos: int, limit: int, first_dsz: int, first_csz: int) -> bytes:
    """Inflate the first chunk and any continuation chunks up to `limit`."""
    out = bytearray()
    dsz, csz = first_dsz, first_csz
    while True:
        if csz <= 0 or pos + csz > len(data):
            break
        try:
            out += zlib.decompress(data[pos:pos + csz])
        except zlib.error:
            try:
                d = zlib.decompressobj()
                out += d.decompress(data[pos:pos + csz])
            except zlib.error:
                break
        pos += csz
        if pos + 8 > limit or pos + 9 > len(data):
            break
        dsz, csz = struct.unpack_from("<II", data, pos)
        if csz <= 0 or pos + 8 + csz > len(data) or data[pos + 8] != 0x78:
            break
        pos += 8
    return bytes(out)


def find_blobs(data: bytes) -> List[Blob]:
    """Return every GUID-wrapped blob in `data`, inflated, with its name if any."""
    out: List[Blob] = []
    for m in re.finditer(re.escape(MARKER), data):
        h = m.start()
        if h < 4 or h + 24 > len(data):
            continue
        blob_size = struct.unpack_from("<I", data, h - 4)[0]
        dsz, csz = struct.unpack_from("<II", data, h + 16)
        limit = min(len(data), h - 4 + 4 + blob_size + 8) if blob_size else len(data)
        payload = _inflate_chunks(data, h + 24, limit, dsz, csz)
        if not payload:
            continue
        name = ""
        window = data[max(0, h - 200):h]
        last: Optional[re.Match] = None
        for nm in _NAME_RE.finditer(window):
            last = nm
        if last is not None:
            n = last.group(1)[0]
            s = last.end()
            raw = window[s:s + 2 * n]
            if len(raw) == 2 * n:
                try:
                    name = raw.decode("utf-16-le")
                except UnicodeDecodeError:
                    name = ""
        out.append(Blob(offset=h, name=name, data=payload))
    return out
