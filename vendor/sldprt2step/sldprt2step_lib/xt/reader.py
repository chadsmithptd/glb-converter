"""Parasolid XT neutral-binary ('PS\\0\\0') transmit reader.

Implements the physical layout of the XT Format Reference (chapters 2-3):

* header: "PS\\0\\0", short-prefixed modeller string, int-prefixed schema string
  (SCH_<modeller>_<schema>[_<base>]), [short: max node types when a base schema
  is named], int: user-field size;
* nodes: 2-byte type; on the FIRST occurrence of a type the embedded schema
  info (0xff = identical to base, else an edit script C/D/I/A...Z against the
  base layout, or a full definition for types unknown to the base); then, for
  variable-length types, a 4-byte element count; then the node index; then the
  fields in layout order;
* terminator: type 1 followed by index 0;
* pointer indices / positive integers: a 2-byte value = index+1 when small,
  else a negative remainder followed by a quotient (spec 3.3.3);
* null markers: int -32764, double -3.14158e13.

Nothing here interprets the model; see `model.py` for topology traversal.
"""
from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional, Tuple

from . import schema
from .schema import VAR, Field


class XtError(Exception):
    """Structural / schema failure while reading an XT stream."""


class Node:
    __slots__ = ("index", "type", "f")

    def __init__(self, index: int, ntype: int, fields: Dict[str, Any]) -> None:
        self.index = index
        self.type = ntype
        self.f = fields

    @property
    def name(self) -> str:
        return schema.NODE_NAMES.get(self.type, "TYPE_%d" % self.type)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<%s #%d %r>" % (self.name, self.index, self.f)


class XtFile:
    def __init__(self) -> None:
        self.modeller_version = 0
        self.schema_id = ""
        self.base_schema = 0
        self.partition = False
        self.nodes: Dict[int, Node] = {}
        self.root_index = 0
        self.layouts: Dict[int, List[Field]] = {}
        self.schema_log: List[str] = []
        self.trailing_bytes = 0

    def get(self, index: int) -> Optional[Node]:
        if not index:
            return None
        return self.nodes.get(index)

    def deref(self, node: Optional[Node], field: str) -> Optional[Node]:
        if node is None:
            return None
        return self.get(node.f.get(field, 0) or 0)

    def chain(self, head: Optional[Node], next_field: str, limit: int = 10_000_000) -> List[Node]:
        out: List[Node] = []
        seen = set()
        cur = head
        while cur is not None and cur.index not in seen and len(out) < limit:
            seen.add(cur.index)
            out.append(cur)
            cur = self.deref(cur, next_field)
        return out

    def census(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for n in self.nodes.values():
            out[n.name] = out.get(n.name, 0) + 1
        return out


_EDIT_CHARS = frozenset(b"CDIAZ")


class _Reader:
    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0
        self.n = len(buf)

    def need(self, k: int) -> None:
        if self.pos + k > self.n:
            raise XtError("truncated stream at offset %d (need %d bytes)" % (self.pos, k))

    def u8(self) -> int:
        self.need(1)
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def i16(self) -> int:
        self.need(2)
        v = struct.unpack_from(">h", self.buf, self.pos)[0]
        self.pos += 2
        return v

    def u16(self) -> int:
        self.need(2)
        v = struct.unpack_from(">H", self.buf, self.pos)[0]
        self.pos += 2
        return v

    def i32(self) -> int:
        self.need(4)
        v = struct.unpack_from(">i", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def f64(self) -> float:
        self.need(8)
        v = struct.unpack_from(">d", self.buf, self.pos)[0]
        self.pos += 8
        return v

    def f64s(self, k: int) -> Tuple[float, ...]:
        self.need(8 * k)
        v = struct.unpack_from(">%dd" % k, self.buf, self.pos)
        self.pos += 8 * k
        return v

    def raw(self, k: int) -> bytes:
        self.need(k)
        v = self.buf[self.pos:self.pos + k]
        self.pos += k
        return v

    def index(self) -> int:
        r = self.i16()
        q = 0
        if r < 0:
            q = self.i16()
            r = -r
        return q * 32767 + r - 1

    posint = index

    def short_string(self) -> str:
        k = self.u8()
        return self.raw(k).decode("latin-1")


def _read_field_def(r: _Reader) -> Field:
    name = r.short_string()
    ptr_class = r.u16()
    n_elts = r.posint()
    if ptr_class == 0:
        tstr = r.short_string()
        kind = tstr[-1] if tstr else "d"
    else:
        kind = "p"
    if n_elts == 1:
        r.u8()  # xmt_code, only present for variable-length fields
        n = VAR
    elif n_elts == 0:
        n = 1
    else:
        n = n_elts
    return Field(name, kind, n, ptr_class)


def _read_schema_info(r: _Reader, ntype: int, xt: XtFile) -> List[Field]:
    first = r.u8()
    tname = schema.NODE_NAMES.get(ntype, "TYPE_%d" % ntype)
    base = schema.BASE.get(ntype)
    if first == 0xFF:
        if base is None:
            raise XtError("node type %d (%s) flagged identical to base schema but unknown" % (ntype, tname))
        xt.schema_log.append("%s: base" % tname)
        return list(base)
    nfields = first
    # Disambiguate edit script vs full definition by the byte that follows.
    r.need(1)
    nxt = r.buf[r.pos]
    if base is not None and nxt in _EDIT_CHARS:
        out: List[Field] = []
        bi = 0
        ops: List[str] = []
        while True:
            op = r.u8()
            if op == ord("Z"):
                break
            if op == ord("C"):
                if bi >= len(base):
                    raise XtError("%s: edit script copies past end of base layout" % tname)
                out.append(base[bi])
                bi += 1
                ops.append("C")
            elif op == ord("D"):
                if bi >= len(base):
                    raise XtError("%s: edit script deletes past end of base layout" % tname)
                ops.append("D(%s)" % base[bi].name)
                bi += 1
            elif op == ord("I") or op == ord("A"):
                fld = _read_field_def(r)
                out.append(fld)
                ops.append("%s(%s:%s%s)" % (chr(op), fld.name, fld.kind,
                                            "" if fld.n == 1 else "[%s]" % ("var" if fld.n == VAR else fld.n)))
            else:
                raise XtError("%s: bad edit op 0x%02x at offset %d" % (tname, op, r.pos - 1))
        if bi < len(base):
            ops.append("implicit-drop(%s)" % ",".join(f.name for f in base[bi:]))
        if len(out) != nfields:
            raise XtError("%s: edit script yields %d fields, header says %d (%s)"
                          % (tname, len(out), nfields, " ".join(ops)))
        xt.schema_log.append("%s: %s -> %s" % (tname, " ".join(ops), [f.name for f in out]))
        return out
    # Full definition (type not in base schema).
    name = r.short_string()
    desc = r.short_string()
    out = [_read_field_def(r) for _ in range(nfields)]
    xt.schema_log.append("%s: full def name=%r desc=%r fields=%s" % (tname, name, desc, [f.name for f in out]))
    return out


def _read_value(r: _Reader, fld: Field, count: int) -> Any:
    k = fld.kind
    scalar = fld.n == 1
    if k == "p":
        if scalar:
            return r.index()
        return [r.index() for _ in range(count)]
    if k == "d" or k == "t":
        if scalar:
            return r.i32()
        return [r.i32() for _ in range(count)]
    if k == "f":
        if scalar:
            return r.f64()
        return list(r.f64s(count))
    if k == "v" or k == "h":
        if scalar:
            return r.f64s(3)
        return [r.f64s(3) for _ in range(count)]
    if k == "i":
        if scalar:
            return r.f64s(2)
        return [r.f64s(2) for _ in range(count)]
    if k == "b":
        if scalar:
            return r.f64s(6)
        return [r.f64s(6) for _ in range(count)]
    if k == "u":
        if scalar:
            return r.u8()
        return list(r.raw(count))
    if k == "l":
        if scalar:
            return r.u8() != 0
        return [b != 0 for b in r.raw(count)]
    if k == "c":
        if scalar:
            return chr(r.u8())
        return r.raw(count).decode("latin-1")
    if k == "n":
        if scalar:
            return r.i16()
        return [r.i16() for _ in range(count)]
    if k == "w":
        if scalar:
            return r.u16()
        return [r.u16() for _ in range(count)]
    raise XtError("unknown field kind %r for field %s" % (k, fld.name))


def read_xt(buf: bytes) -> XtFile:
    """Parse one neutral-binary XT transmit (part or partition) stream."""
    r = _Reader(buf)
    if r.raw(2) != b"PS":
        raise XtError("not a binary XT stream (no PS flag)")
    flag = r.raw(2)
    if flag != b"\x00\x00":
        raise XtError("unsupported XT binary flavour (flag %r); only neutral binary is supported" % flag)
    xt = XtFile()
    mlen = r.i16()
    modeller = r.raw(mlen).decode("latin-1")
    xt.partition = "partition" in modeller
    digits = ""
    for ch in modeller.rsplit(" ", 1)[-1]:
        if ch.isdigit():
            digits += ch
    xt.modeller_version = int(digits) if digits else 0
    slen = r.i32()
    xt.schema_id = r.raw(slen).decode("latin-1")
    parts = xt.schema_id.split("_")
    if len(parts) >= 4 and parts[0] == "SCH":
        xt.base_schema = int(parts[3])
        r.u16()  # maximum number of node types
        if xt.base_schema != 13006:
            raise XtError("unsupported base schema %d (only SCH_13006 embedding is supported)" % xt.base_schema)
    else:
        raise XtError("XT stream without embedded schema (%s) is not supported" % xt.schema_id)
    usfld = r.i32()
    if usfld != 0:
        raise XtError("XT user fields (USFLD_SIZE=%d) are not supported" % usfld)

    layouts = xt.layouts
    nodes = xt.nodes
    first = True
    while True:
        ntype = r.u16()
        if ntype == 1:
            if r.u16() != 1:
                raise XtError("bad terminator at offset %d" % (r.pos - 4))
            break
        layout = layouts.get(ntype)
        if layout is None:
            layout = _read_schema_info(r, ntype, xt)
            layouts[ntype] = layout
        count = 0
        if schema.is_variable(layout):
            count = r.i32()
            if count < 0:
                raise XtError("negative variable length at offset %d" % (r.pos - 4))
        idx = r.index()
        fields: Dict[str, Any] = {}
        try:
            for fld in layout:
                fields[fld.name] = _read_value(r, fld, count if fld.n == VAR else fld.n)
        except XtError as exc:
            raise XtError("while reading %s #%d: %s" % (schema.NODE_NAMES.get(ntype, ntype), idx, exc))
        if idx in nodes:
            raise XtError("duplicate node index %d" % idx)
        nodes[idx] = Node(idx, ntype, fields)
        if first:
            xt.root_index = idx
            first = False
    xt.trailing_bytes = r.n - r.pos
    return xt
