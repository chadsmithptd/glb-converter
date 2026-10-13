from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import List, Optional, Sequence, Union

from .. import jscompat

SW3D_SECTION_MARKER = bytes([0x14, 0x00, 0x06, 0x00, 0x08, 0x00])
SW3D_HEADER_SIZE = 26

PARASOLID_MAGIC_SIGNATURES: Sequence[bytes] = (
    b"P_S_",
    b"PARA",
    b"PS-P",
    b"PS-A",
    b"PS-S",
)

NESTED_ZLIB_OFFSET = 28


@dataclass
class Sw3DSection:
    offset: int
    type_id: int
    compressed_size: int
    decompressed_size: int
    name_length: int
    metadata: bytes
    payload: bytes


@dataclass
class Sw3DParasolidResult:
    offset: int
    type_id: int
    data: bytes


def _decompress_capped(data: bytes, wbits: int, max_output_length: int) -> bytes:
    decompressor = zlib.decompressobj(wbits)
    out = decompressor.decompress(data, max_output_length)
    if len(out) == max_output_length:
        extra = decompressor.decompress(b"", 1)
        if extra:
            raise zlib.error("max output length exceeded")
    if decompressor.unconsumed_tail:
        extra = decompressor.decompress(decompressor.unconsumed_tail, 1)
        if extra:
            raise zlib.error("max output length exceeded")
    return out


def _inflate_sync(data: bytes, max_output_length: Optional[int] = None) -> bytes:
    if max_output_length is None:
        return zlib.decompress(data)
    return _decompress_capped(data, 0, max_output_length)


def _inflate_raw_sync(data: bytes, max_output_length: Optional[int] = None) -> bytes:
    if max_output_length is None:
        return zlib.decompress(data, -15)
    return _decompress_capped(data, -15, max_output_length)


def is_parasolid_buffer(buf: bytes) -> bool:
    if len(buf) < 4:
        return False
    head = buf[0:4]
    for sig in PARASOLID_MAGIC_SIGNATURES:
        if head == sig:
            return True
    if len(buf) >= 20 and buf[0] == 0x50 and buf[1] == 0x53:
        slice_bytes = buf[5:20]
        if 0x54 in slice_bytes:
            idx = buf.find(b"TRANSMIT")
            if idx >= 0 and idx < 32:
                return True
    return False


def _try_nested_decompress(
    outer: bytes, max_output: int = 50_000_000
) -> Optional[bytes]:
    if len(outer) <= NESTED_ZLIB_OFFSET:
        return None
    zlib_byte = outer[NESTED_ZLIB_OFFSET]
    if zlib_byte != 0x78:
        return None
    try:
        return _inflate_sync(outer[NESTED_ZLIB_OFFSET:], max_output)
    except zlib.error:
        return None


class Sw3DStorageParser:
    @staticmethod
    def extract_parasolid_sections(
        buf: bytes,
        opts: Optional[dict] = None,
    ) -> List[Sw3DParasolidResult]:
        if opts is None:
            opts = {}
        max_dec = opts.get("max_decompressed_size", 50_000_000)
        max_results = opts.get("max_results", float("inf"))
        results: List[Sw3DParasolidResult] = []
        idx = 0

        while True:
            idx = buf.find(SW3D_SECTION_MARKER, idx)
            if idx < 0:
                break
            if idx + SW3D_HEADER_SIZE > len(buf):
                break

            type_id = jscompat.read_u32le(buf, idx + 6)
            compressed_size = jscompat.read_u32le(buf, idx + 14)
            decompressed_size = jscompat.read_u32le(buf, idx + 18)
            name_length = jscompat.read_u32le(buf, idx + 22)

            if (
                name_length > 0
                and name_length < 1024
                and compressed_size > 4
                and compressed_size < len(buf)
                and decompressed_size > 4
                and decompressed_size < max_dec
            ):
                payload_offset = idx + SW3D_HEADER_SIZE + name_length
                payload_end = payload_offset + compressed_size

                if payload_end <= len(buf):
                    try:
                        decompressed = _inflate_raw_sync(
                            buf[payload_offset:payload_end],
                            decompressed_size + 1024,
                        )

                        if is_parasolid_buffer(decompressed):
                            results.append(
                                Sw3DParasolidResult(
                                    offset=idx,
                                    type_id=type_id,
                                    data=decompressed,
                                )
                            )
                        else:
                            nested = _try_nested_decompress(decompressed, max_dec)
                            if nested is not None and is_parasolid_buffer(nested):
                                results.append(
                                    Sw3DParasolidResult(
                                        offset=idx,
                                        type_id=type_id,
                                        data=nested,
                                    )
                                )
                        if len(results) >= max_results:
                            return results
                    except zlib.error:
                        pass

            idx += 1

        return results

    @staticmethod
    def extract_first_parasolid_section(
        buf: bytes,
        opts: Optional[dict] = None,
    ) -> Optional[Sw3DParasolidResult]:
        if opts is None:
            opts = {}
        all_sections = Sw3DStorageParser.extract_parasolid_sections(buf, opts)
        if not all_sections:
            return None
        best = all_sections[0]
        for current in all_sections[1:]:
            if len(current.data) > len(best.data):
                best = current
        return best
