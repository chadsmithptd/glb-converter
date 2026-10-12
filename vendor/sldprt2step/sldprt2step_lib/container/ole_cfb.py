from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from .. import jscompat

PARASOLID_STREAM_NAMES: Sequence[str] = (
    "Contents",
    "PowerContents",
)

OLE2_SIGNATURE = bytes(
    [0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1]
)


@dataclass
class OleDirectoryEntry:
    name: str
    type: int
    start_sector: int
    size: int


@dataclass
class OleStreamResult:
    stream_name: str
    data: bytes
    offset: int


class OleContainerParser:
    def __init__(self, buf: bytes) -> None:
        if len(buf) < 512:
            raise ValueError("Buffer too small to be a valid OLE2 file")
        if buf[0:8] != OLE2_SIGNATURE:
            raise ValueError("Not an OLE2 compound file (wrong magic bytes)")

        self.buf = buf
        self.sector_size = jscompat.js_shl(1, jscompat.read_u16le(buf, 30))
        self.fat = self._build_fat()
        self.entries = self._read_directory()

    def _build_fat(self) -> List[int]:
        buf = self.buf
        ss = self.sector_size
        difat_start_sector = jscompat.read_i32le(buf, 68)

        difat: List[int] = []
        for i in range(109):
            sector = jscompat.read_i32le(buf, 76 + i * 4)
            if sector >= 0:
                difat.append(sector)

        difat_sec = difat_start_sector
        while difat_sec >= 0 and difat_sec < 0xFFFFFFFE:
            off = (difat_sec + 1) * ss
            entries_per_sec = ss // 4 - 1
            for i in range(entries_per_sec):
                sector = jscompat.read_i32le(buf, off + i * 4)
                if sector >= 0:
                    difat.append(sector)
            difat_sec = jscompat.read_i32le(buf, off + ss - 4)

        fat: List[int] = []
        for sec in difat:
            off = (sec + 1) * ss
            for i in range(ss // 4):
                fat.append(jscompat.read_i32le(buf, off + i * 4))
        return fat

    def read_chain(self, start_sector: int) -> bytes:
        chunks: List[bytes] = []
        sec = start_sector
        visited = set()
        while sec >= 0 and sec < 0xFFFFFFFE:
            if sec in visited:
                break
            visited.add(sec)
            off = (sec + 1) * self.sector_size
            if off + self.sector_size > len(self.buf):
                break
            chunks.append(self.buf[off : off + self.sector_size])
            next_sec = jscompat.at(self.fat, sec)
            if next_sec is jscompat.JS_UNDEFINED:
                sec = -1
            else:
                sec = next_sec
        return b"".join(chunks)

    def _read_directory(self) -> List[OleDirectoryEntry]:
        dir_start_sector = jscompat.read_u32le(self.buf, 48)
        dir_data = self.read_chain(dir_start_sector)
        entries: List[OleDirectoryEntry] = []

        i = 0
        while i + 128 <= len(dir_data):
            name_len = jscompat.read_u16le(dir_data, i + 64)
            if name_len == 0:
                i += 128
                continue
            name_end = i + max(0, name_len - 2)
            name = jscompat.buf_to_utf16le(dir_data[i:name_end])
            entry_type = dir_data[i + 66]
            start_sector = jscompat.read_i32le(dir_data, i + 116)
            size = jscompat.read_u32le(dir_data, i + 120)
            entries.append(
                OleDirectoryEntry(
                    name=name,
                    type=entry_type,
                    start_sector=start_sector,
                    size=size,
                )
            )
            i += 128
        return entries

    def extract_stream(self, stream_name: str) -> Optional[OleStreamResult]:
        entry = None
        for candidate in self.entries:
            if (
                candidate.type == 2
                and candidate.name == stream_name
                and candidate.start_sector >= 0
            ):
                entry = candidate
                break
        if entry is None:
            return None

        data = self.read_chain(entry.start_sector)[: entry.size]
        return OleStreamResult(
            stream_name=entry.name,
            data=data,
            offset=(entry.start_sector + 1) * self.sector_size,
        )

    def extract_parasolid_stream(self) -> Optional[OleStreamResult]:
        for name in PARASOLID_STREAM_NAMES:
            result = self.extract_stream(name)
            if result is not None:
                return result
        return None

    @staticmethod
    def is_ole2(buf: bytes) -> bool:
        return len(buf) >= 8 and buf[0:8] == OLE2_SIGNATURE
