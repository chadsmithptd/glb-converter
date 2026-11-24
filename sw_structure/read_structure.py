"""Reads the structure SolidWorks stores as XML inside .SLDASM / .SLDDRW files (2015+ container).

Standard library only. Prints one JSON object:
  {ok, kind, components, tables, title_block, notes, warnings}
Drawing dimensions are not decoded: their text is stored apart from the features it belongs to.
"""
import json
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET
import zlib

MARKER = bytes([0x14, 0x00, 0x06, 0x00, 0x08, 0x00])
HEADER = 26
MAX_SECTION = 40 * 1024 * 1024
WANTED = ("swXmlContents/COMPINSTANCETREE", "swXmlContents/Tables", "swXmlContents/CustomProperties")
MAX_REFS = 20000
MAX_ROWS = 2000
MAX_NOTES = 200


class Unreadable(Exception):
    pass


def sections(buf):
    """Yields (name, data) for the wanted sections. Names are rotated left by a per-file key."""
    if len(buf) < HEADER:
        raise Unreadable("file too small")
    if buf[:4] == b"\xd0\xcf\x11\xe0":
        raise Unreadable("older than SolidWorks 2015 (OLE container)")
    key = buf[7] & 7
    pos = buf.find(MARKER)
    if pos < 0:
        raise Unreadable("not a SolidWorks 2015+ file")
    while pos >= 0:
        if pos + HEADER <= len(buf):
            comp, size, nlen = struct.unpack_from("<III", buf, pos + 14)
            start = pos + HEADER + nlen
            if 0 < nlen < 400 and 0 < comp and start + comp <= len(buf):
                raw = buf[pos + HEADER:start]
                name = bytes(((c << key) | (c >> (8 - key))) & 255 for c in raw).decode("latin1")
                if name in WANTED:
                    d = zlib.decompressobj(-15)
                    try:
                        data = d.decompress(buf[start:start + comp], MAX_SECTION + 1)
                    except zlib.error:
                        data = b""
                    if len(data) > MAX_SECTION:
                        raise Unreadable("section " + name + " is too large")
                    if data:
                        yield name, data
        pos = buf.find(MARKER, pos + 1)


def parse_xml(data):
    # No SolidWorks stream carries a DTD; refusing one shuts out entity expansion.
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise Unreadable("unexpected DTD")
    return ET.fromstring(data)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def base(path):
    """File name without folder or extension, from a Windows path."""
    return re.sub(r"\.sld(prt|asm|drw)$", "", re.split(r"[\\/]", path or "")[-1].strip(), flags=re.I)


def read_tree(data):
    """One entry per (assembly, configuration): its direct components, grouped and counted."""
    root = parse_xml(data)
    files = {e.get("id"): e for e in root.iter() if local(e.tag) == "swFile"}
    out = []
    seen = 0
    for model in (e for e in root.iter() if local(e.tag) == "swModel"):
        groups = {}
        stack = list(model)
        while stack:
            e = stack.pop(0)
            if local(e.tag) == "swModel":
                continue
            if local(e.tag) == "swReference":
                seen += 1
                if seen > MAX_REFS:
                    raise Unreadable("too many components")
                if e.get("swSuppressed") == "YES" or e.get("swExcludeFromBOM") == "YES":
                    continue
                k = (e.get("swName") or "", e.get("swConfigurationName") or "")
                groups[k] = groups.get(k, 0) + 1
                continue
            stack.extend(e)
        if groups:
            f = files.get(model.get("swFileRef"))
            out.append({
                "assembly": base(f.get("swPath")) if f is not None else (model.get("swName") or ""),
                "configuration": model.get("swConfigurationName") or "",
                "configuration_id": model.get("swConfigurationId") or "",
                "components": [{"file": k[0], "configuration": k[1], "quantity": n} for k, n in groups.items()],
            })
    return out


def component_of(rep):
    """("ACK41004", "5 inch", depth) from 'ASM-20<Default>@Bill of Materials1/ACK41004-3<5 inch>@ASM'."""
    if "/" not in (rep or ""):
        return None, None, 0
    # A configuration name may itself hold "/": split only where a component path can start.
    parts = re.split(r"(?<=Materials\d)/|/(?=[^<>]*-\d+<)", rep)
    last = parts[-1]
    m = re.match(r"(.+?)-\d+<(.*)>@", last)
    if not m:
        return None, None, len(parts) - 1
    return m.group(1), m.group(2), len(parts) - 1


def clean(text):
    text = re.sub(r"<model_name>.*?</model_name>|<stream_name>.*?</stream_name>|<TableName>.*?</TableName>|<RowId>.*?</RowId>", "", text or "")
    text = re.sub(r"\$-[^$]{1,40}-\$", "", text)
    text = re.sub(r"<cache_string>(.*?)</cache_string>", r"\1", text)
    text = re.sub(r"<(?:PARA|FONT|STACK|SUP|SUB|IMG|Border|/)[^>]*>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def read_tables(data):
    out = []
    for t in parse_xml(data).iter("Table"):
        cols = [{"type": c.get("Type"), "property": (c.get("CustomProperty") or "")} for c in t.iter("ColumnData")]
        rows = []
        for r in t.iter("Row"):
            if len(rows) >= MAX_ROWS:
                break
            cells = [clean("".join(x.get("Text", "") for x in c.iter("Text_Element"))) for c in r.iter("Cell")]
            f, cfg, depth = component_of(r.get("ComponentRep"))
            rows.append({"cells": cells, "component": f, "configuration": cfg, "depth": depth})
        out.append({
            "type": t.get("Table_Type"),
            "name": t.get("Display_Name"),
            "sheet": t.get("SheetName"),
            "document": base(t.get("ReferencedDocument")),
            "is_assembly": (t.get("ReferencedDocument") or "").lower().endswith(".sldasm"),
            "columns": cols,
            "rows": rows,
        })
    return out


FIELD = re.compile(r"\$-([A-Za-z0-9 .]{1,30})-\$<model_name>([^<]{0,400})</model_name>(?:<stream_name>[^<]{0,100}</stream_name>)?<cache_string>([^<]{0,300})</cache_string>")


def read_annotations(data):
    """Title-block values linked to a model (a copied drawing's own leftovers are not), and notes."""
    fields, notes = [], []
    for e in parse_xml(data).iter("Text_Element"):
        text = e.get("Text") or ""
        for m in FIELD.finditer(text):
            if m.group(3).strip():
                f = {"field": m.group(1).strip(), "model": base(m.group(2)), "value": m.group(3).strip()}
                if f not in fields:
                    fields.append(f)
        plain = clean(text)
        if len(plain) > 2 and "$-" not in text and plain not in notes and len(notes) < MAX_NOTES:
            notes.append(plain)
    return fields, notes


def read(path):
    with open(path, "rb") as fh:
        buf = fh.read()
    result = {"ok": True, "kind": os.path.splitext(path)[1].lower().lstrip("."),
              "components": [], "tables": [], "title_block": [], "notes": [], "warnings": []}
    found = set()
    for name, data in sections(buf):
        if name in found:
            continue
        try:
            if name.endswith("COMPINSTANCETREE"):
                result["components"] = read_tree(data)
            elif name.endswith("Tables"):
                result["tables"] = read_tables(data)
            else:
                result["title_block"], result["notes"] = read_annotations(data)
            found.add(name)
        except ET.ParseError as err:
            result["warnings"].append("%s: %s" % (name, err))
    if not found:
        raise Unreadable("no readable structure in this file")
    return result


if __name__ == "__main__":
    try:
        out = read(sys.argv[1])
    except Unreadable as err:
        out = {"ok": False, "error": "unreadable", "message": str(err)}
    print(json.dumps(out))
