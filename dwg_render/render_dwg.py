"""Render a DXF (converted from DWG by LibreDWG's dwg2dxf) to a multi-page PDF and list its text.

Usage: render_dwg.py <input.dxf> <output.pdf | ->   ("-" lists the text only, no PDF)

One PDF page per paper-space layout that has content (the printable sheets, where the title block
and parts list usually live); a drawing with no paper-space content gets one page of model space.
Pages are sized like a real sheet so a downstream rasterizer treats them like a customer's PDF.

The last stdout line is a JSON result:
  {"ok": true, "dxf_version", "sheets": [{name, width_in, height_in}], "texts": [...],
   "text_truncated", "audit_fixes"}
or {"ok": false, "error": <code>, "message": ...} with exit code 1.

texts are the drawing's own strings (TEXT, MTEXT, block attributes such as title-block fields,
and text inside inserted blocks), in drawing order, de-duplicated -- exact values for a reader
whose copy of the page image is too small to read a parts list.
"""
import json
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402

from ezdxf import recover  # noqa: E402
from ezdxf.addons.drawing import Frontend, RenderContext  # noqa: E402
from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration  # noqa: E402
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend  # noqa: E402

MAX_SHEETS = 10
MAX_TEXTS = 2000
MAX_TEXT_CHARS = 60000
# Model space has no paper size; draw it on a D-size sheet (34 in long edge), the common size for
# assembly drawings, so small parts-list text stays legible after rasterizing.
MODEL_LONG_EDGE_IN = 34.0
MIN_SHORT_EDGE_IN = 8.5
MM_PER_IN = 25.4


def done(result, code=0):
    print(json.dumps(result))
    sys.exit(code)


def paper_size_in(layout):
    """Paper size of a paper-space layout in inches (landscape-aware), or None if unset."""
    try:
        w = float(layout.dxf.paper_width or 0)
        h = float(layout.dxf.paper_height or 0)
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    # plot_rotation 1/3 = 90/270 degrees: the sheet is printed sideways.
    if int(getattr(layout.dxf, "plot_rotation", 0) or 0) in (1, 3):
        w, h = h, w
    return w / MM_PER_IN, h / MM_PER_IN


def entity_text(entity):
    kind = entity.dxftype()
    try:
        if kind in ("TEXT", "ATTRIB", "ATTDEF"):
            return entity.plain_text()
        if kind == "MTEXT":
            return entity.plain_text()
    except Exception:
        return None
    return None


def collect_texts(entities, out, depth=0):
    for entity in entities:
        if len(out) >= MAX_TEXTS:
            return
        kind = entity.dxftype()
        if kind == "INSERT":
            # Keep the attribute tag: title-block fields only make sense with their names
            # ("DWGNR: 020630-00", "WHERE: RMS-3100 SHAPER DRIVE").
            for attrib in entity.attribs:
                value = entity_text(attrib)
                if value and str(value).strip():
                    out.append(f"{attrib.dxf.tag}: {value}")
            # Static text inside the block (title-block labels, parts-list rows drawn as a block).
            if depth < 2:
                try:
                    inner = [e for e in entity.virtual_entities() if e.dxftype() in ("TEXT", "MTEXT", "INSERT")]
                except Exception:
                    inner = []
                collect_texts(inner, out, depth + 1)
        elif kind in ("TEXT", "MTEXT"):
            out.append(entity_text(entity))


def render_pdf(doc, layouts, pdf_path, sheets, raw_texts):
    config = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.BLACK)
    with PdfPages(pdf_path) as pdf:
        for layout in layouts:
            fig = plt.figure()
            ax = fig.add_axes([0, 0, 1, 1])
            Frontend(RenderContext(doc), MatplotlibBackend(ax), config=config).draw_layout(layout, finalize=True)

            size = paper_size_in(layout) if layout.name != "Model" else None
            if size is None:
                x0, x1 = ax.get_xlim()
                y0, y1 = ax.get_ylim()
                aspect = (x1 - x0) / max(y1 - y0, 1e-9)
                if aspect >= 1:
                    size = (MODEL_LONG_EDGE_IN, max(MODEL_LONG_EDGE_IN / aspect, MIN_SHORT_EDGE_IN))
                else:
                    size = (max(MODEL_LONG_EDGE_IN * aspect, MIN_SHORT_EDGE_IN), MODEL_LONG_EDGE_IN)
            fig.set_size_inches(*size)
            pdf.savefig(fig)
            plt.close(fig)
            sheets.append({"name": layout.name, "width_in": round(size[0], 2), "height_in": round(size[1], 2)})
            collect_texts(layout, raw_texts)


def main():
    if len(sys.argv) != 3:
        done({"ok": False, "error": "usage", "message": "render_dwg.py <input.dxf> <output.pdf | ->"}, 1)
    dxf_path, pdf_path = sys.argv[1], sys.argv[2]

    try:
        doc, auditor = recover.readfile(dxf_path)
    except Exception as exc:  # ezdxf raises DXFStructureError and friends for unreadable files
        done({"ok": False, "error": "unreadable_dxf", "message": str(exc)[:500]}, 1)

    paper = [l for l in doc.layouts if l.name != "Model" and len(l) > 0][:MAX_SHEETS]
    layouts = paper or [doc.modelspace()]

    sheets = []
    raw_texts = []
    if pdf_path == "-":
        for layout in layouts:
            sheets.append({"name": layout.name, "width_in": None, "height_in": None})
            collect_texts(layout, raw_texts)
    else:
        render_pdf(doc, layouts, pdf_path, sheets, raw_texts)

    texts, seen, chars = [], set(), 0
    truncated = len(raw_texts) >= MAX_TEXTS
    for text in raw_texts:
        text = " ".join(str(text or "").split())
        if not text or text in seen:
            continue
        if chars + len(text) > MAX_TEXT_CHARS:
            truncated = True
            break
        seen.add(text)
        texts.append(text)
        chars += len(text)

    done({
        "ok": True,
        "dxf_version": doc.dxfversion,
        "sheets": sheets,
        "texts": texts,
        "text_truncated": truncated,
        "audit_fixes": len(auditor.fixes),
    })


if __name__ == "__main__":
    main()
