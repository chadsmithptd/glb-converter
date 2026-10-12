"""XT (Parasolid) topology -> ISO 10303-21 AP214 writer.

Walks BODY -> REGION(solid) -> SHELL -> FACE -> LOOP -> FIN -> EDGE -> VERTEX
exactly as stored in the XT stream and emits MANIFOLD_SOLID_BREP solids built
from ADVANCED_FACEs. Exact geometry is mapped exactly (plane, cylinder, cone,
sphere, torus, B-surface, swept, spun, offset, line, circle, ellipse, B-curve).
Geometry that STEP cannot hold exactly (intersection curves, SP-curves,
rolling-ball blends) is approximated by interpolating B-splines and reported in
the warnings list; anything else raises ConvertError with a precise code.

Orientation rules (XT Format Reference 5.2.8, 5.3.x):
* face normal parallel to surface natural normal  <=> face.sense == surface.sense
* edge direction parallel to curve direction       <=> curve.sense == '+'
* fin direction parallel to edge direction         <=> fin.sense == '+'
* edge start vertex = edge.halfedge.other.vertex, end vertex = edge.halfedge.vertex
* shell.face chain = faces whose normal points out of the region (back-faces)
All analytic surfaces share the ISO 10303-42 parameterisation (the cone as
actually written by Parasolid is R = P + vA + e(u)(r + v tan a), identical to
CONICAL_SURFACE), so same_sense follows the XT rule directly.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..xt import geom
from ..xt.geom import Vec, add, cross, dist, dot, mul, norm, sub, unit
from ..xt.reader import Node, XtFile

SCALE = 1000.0  # XT metres -> STEP millimetres


class ConvertError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def fmt(x: float) -> str:
    """Shortest round-trip real in Part-21 syntax."""
    if x != x or x in (float("inf"), float("-inf")):
        raise ConvertError("convert_failed", "non-finite number in geometry")
    if x == 0.0:
        return "0."
    s = repr(float(x))
    if "e" in s or "E" in s:
        mant, exp = s.lower().split("e")
        if "." not in mant:
            mant += "."
        return "%sE%d" % (mant, int(exp))
    if "." not in s:
        s += "."
    if s.endswith(".0"):
        s = s[:-1]
    return s


def fmm(x: float) -> str:
    return fmt(x * SCALE)


def fpt(p: Sequence[float]) -> str:
    return "(%s,%s,%s)" % (fmm(p[0]), fmm(p[1]), fmm(p[2]))


def fdir(p: Sequence[float]) -> str:
    return "(%s,%s,%s)" % (fmt(p[0]), fmt(p[1]), fmt(p[2]))


class StepWriter:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.next_id = 1

    def add(self, text: str) -> int:
        eid = self.next_id
        self.next_id += 1
        self.lines.append("#%d=%s;" % (eid, text))
        return eid

    def point(self, p: Sequence[float], name: str = "") -> int:
        return self.add("CARTESIAN_POINT('%s',%s)" % (name, fpt(p)))

    def direction(self, d: Sequence[float]) -> int:
        return self.add("DIRECTION('',%s)" % fdir(unit(d)))

    def axis2(self, origin: Sequence[float], z: Sequence[float], x: Sequence[float]) -> int:
        # make x exactly perpendicular to z (STEP requires it)
        zz = unit(z)
        xx = sub(x, mul(zz, dot(x, zz)))
        if norm(xx) < 1e-12:
            xx = geom.perp(zz)
        xx = unit(xx)
        return self.add("AXIS2_PLACEMENT_3D('',#%d,#%d,#%d)" % (self.point(origin), self.direction(zz), self.direction(xx)))


def _is_null(x: float) -> bool:
    return x is None or abs(x - (-3.14158e13)) < 1e6


class _SurfaceInfo:
    __slots__ = ("step_id", "match", "ev", "approx")

    def __init__(self, step_id: int, match: bool, ev: Optional[geom.Surface], approx: bool = False) -> None:
        self.step_id = step_id
        self.match = match      # STEP natural normal == XT natural normal
        self.ev = ev            # evaluator in XT parameterisation (for projections)
        self.approx = approx


class XtStepMapper:
    def __init__(self, xt: XtFile, warnings: List[str], body_label: str = "") -> None:
        self.xt = xt
        self.w = StepWriter()
        self.warnings = warnings
        self.body_label = body_label
        self._surf: Dict[int, _SurfaceInfo] = {}
        self._curve: Dict[Tuple[int, float, float], Tuple[int, Any]] = {}
        self._vertex: Dict[int, int] = {}
        self._edge: Dict[int, Tuple[int, int, int]] = {}   # edge idx -> (edge_curve id, start vtx id, end vtx id)
        self._blend: Dict[int, geom.NurbsSurface] = {}
        self._warned: Dict[str, int] = {}
        self.face_count = 0
        self.solid_count = 0

    # -- warnings -------------------------------------------------------------
    def warn(self, key: str, text: str) -> None:
        n = self._warned.get(key, 0)
        self._warned[key] = n + 1
        if n == 0:
            self.warnings.append(text)

    def finish_warnings(self) -> None:
        # append counts for repeated approximations
        for i, text in enumerate(self.warnings):
            for key, n in self._warned.items():
                if text.startswith(key + ":") and n > 1 and not text.endswith("occurrences)"):
                    self.warnings[i] = text + " (%d occurrences)" % n

    # -- helpers --------------------------------------------------------------
    def get(self, idx: int) -> Optional[Node]:
        return self.xt.get(idx)

    def deref(self, n: Optional[Node], f: str) -> Optional[Node]:
        return self.xt.deref(n, f)

    def chain(self, head: Optional[Node], nxt: str) -> List[Node]:
        return self.xt.chain(head, nxt)

    # -- bodies ----------------------------------------------------------------
    def convert_body(self, body: Node, name: str) -> List[int]:
        """Emit all solids of a BODY; returns STEP ids of solid items."""
        btype = body.f.get("body_type")
        if btype != 1:
            kind = {2: "wire", 3: "sheet", 6: "general"}.get(btype, "type %s" % btype)
            raise ConvertError("empty_step", "body %r is a %s body, not a solid (XT body_type=%s)" % (name, kind, btype))
        solids: List[int] = []
        for region in self.chain(self.deref(body, "region"), "next"):
            if region.f.get("type") != "S":
                continue
            shells = self.chain(self.deref(region, "shell"), "next")
            if not shells:
                continue
            shell_ids: List[Tuple[int, float]] = []
            for sh in shells:
                sid, extent = self.convert_shell(sh)
                shell_ids.append((sid, extent))
            shell_ids.sort(key=lambda t: -t[1])
            outer = shell_ids[0][0]
            label = name if len(solids) == 0 else "%s_%d" % (name, len(solids) + 1)
            label = label.replace("'", "''")
            if len(shell_ids) == 1:
                solids.append(self.w.add("MANIFOLD_SOLID_BREP('%s',#%d)" % (label, outer)))
            else:
                self.warn("void_shells", "void_shells: solid %r has %d inner void shells (BREP_WITH_VOIDS)" % (name, len(shell_ids) - 1))
                # XT void-shell faces already point into the cavity (away from the
                # material), which is the orientation ISO 10303-42 requires of void
                # shells, so they are used as-is (orientation .T.)
                voids = []
                for sid, _ in shell_ids[1:]:
                    voids.append(self.w.add("ORIENTED_CLOSED_SHELL('',*,#%d,.T.)" % sid))
                solids.append(self.w.add("BREP_WITH_VOIDS('%s',#%d,(%s))" % (label, outer, ",".join("#%d" % v for v in voids))))
        if not solids:
            raise ConvertError("empty_step", "body %r has no solid region with shells" % name)
        self.solid_count += len(solids)
        return solids

    def convert_shell(self, shell: Node) -> Tuple[int, float]:
        face_ids: List[int] = []
        lo = [float("inf")] * 3
        hi = [float("-inf")] * 3
        seen = set()
        for face in self.chain(self.deref(shell, "face"), "next"):
            if face.index in seen:
                continue
            seen.add(face.index)
            face_ids.append(self.convert_face(face, flip=False, bbox=(lo, hi)))
        for face in self.chain(self.deref(shell, "front_face"), "next_front"):
            if face.index in seen:
                continue
            seen.add(face.index)
            self.warn("front_face", "front_face: shell uses a face from its front side (flipped on output)")
            face_ids.append(self.convert_face(face, flip=True, bbox=(lo, hi)))
        if not face_ids:
            raise ConvertError("unsupported_topology", "shell #%d has no faces (acorn/wire shell)" % shell.index)
        extent = 0.0
        if lo[0] < hi[0]:
            extent = dist(tuple(lo), tuple(hi))
        sid = self.w.add("CLOSED_SHELL('',(%s))" % ",".join("#%d" % f for f in face_ids))
        return sid, extent

    # -- faces -------------------------------------------------------------------
    def convert_face(self, face: Node, flip: bool, bbox) -> int:
        surf_node = self.deref(face, "surface")
        if surf_node is None:
            raise ConvertError("unsupported_topology", "face id %s has no surface (rubber face)" % face.f.get("node_id"))
        sinfo = self.surface(surf_node)
        xt_out = face.f.get("sense") == surf_node.f.get("sense")
        same_sense = xt_out if sinfo.match else (not xt_out)
        if flip:
            same_sense = not same_sense
        loops = self.chain(self.deref(face, "loop"), "next")
        if not loops:
            raise ConvertError("unsupported_topology",
                               "face id %s (%s) has no loops (unbounded periodic face)" % (face.f.get("node_id"), surf_node.name))
        bound_specs: List[Tuple[int, float, bool]] = []
        for loop in loops:
            fins = self.fin_ring(loop)
            if not fins:
                raise ConvertError("unsupported_topology", "loop id %s has no fins" % loop.f.get("node_id"))
            first_edge = self.deref(fins[0], "edge")
            if first_edge is None:
                # isolated loop: single vertex
                vtx = self.deref(fins[0], "vertex")
                if vtx is None:
                    raise ConvertError("unsupported_topology", "isolated loop id %s without vertex" % loop.f.get("node_id"))
                vid = self.vertex(vtx, bbox)
                lid = self.w.add("VERTEX_LOOP('',#%d)" % vid)
                bound_specs.append((lid, 0.0, True))
                continue
            oe_ids: List[int] = []
            extent_lo = [float("inf")] * 3
            extent_hi = [float("-inf")] * 3
            for fin in fins:
                edge = self.deref(fin, "edge")
                if edge is None:
                    raise ConvertError("unsupported_topology", "mixed isolated/ordinary fins in loop id %s" % loop.f.get("node_id"))
                ec_id, sv, ev = self.edge_curve(edge, bbox)
                orient = fin.f.get("sense") == "+"
                if flip:
                    orient = not orient
                oe_ids.append(self.w.add("ORIENTED_EDGE('',*,*,#%d,%s)" % (ec_id, ".T." if orient else ".F.")))
                for vtx_idx in (sv, ev):
                    p = self._vertex_pos.get(vtx_idx)
                    if p is not None:
                        for k in range(3):
                            extent_lo[k] = min(extent_lo[k], p[k])
                            extent_hi[k] = max(extent_hi[k], p[k])
            if flip:
                oe_ids.reverse()
            lid = self.w.add("EDGE_LOOP('',(%s))" % ",".join("#%d" % o for o in oe_ids))
            ext = dist(tuple(extent_lo), tuple(extent_hi)) if extent_lo[0] < extent_hi[0] else 0.0
            bound_specs.append((lid, ext, False))
        # largest loop is the outer bound
        outer_i = max(range(len(bound_specs)), key=lambda i: bound_specs[i][1])
        bound_ids = []
        for i, (lid, _, is_vertex_loop) in enumerate(bound_specs):
            if i == outer_i and not is_vertex_loop and len(bound_specs) > 1:
                bound_ids.append(self.w.add("FACE_OUTER_BOUND('',#%d,.T.)" % lid))
            elif i == outer_i and len(bound_specs) == 1:
                bound_ids.append(self.w.add("FACE_OUTER_BOUND('',#%d,.T.)" % lid))
            else:
                bound_ids.append(self.w.add("FACE_BOUND('',#%d,.T.)" % lid))
        self.face_count += 1
        return self.w.add("ADVANCED_FACE('',(%s),#%d,%s)" % (",".join("#%d" % b for b in bound_ids), sinfo.step_id,
                                                             ".T." if same_sense else ".F."))

    def fin_ring(self, loop: Node) -> List[Node]:
        head = self.deref(loop, "halfedge")
        out: List[Node] = []
        cur = head
        seen = set()
        while cur is not None and cur.index not in seen:
            seen.add(cur.index)
            out.append(cur)
            cur = self.deref(cur, "forward")
            if len(out) > 1_000_000:
                break
        return out

    # -- vertices -------------------------------------------------------------------
    _vertex_pos: Dict[int, Vec] = {}

    def vertex(self, vtx: Node, bbox) -> int:
        if vtx.index in self._vertex:
            return self._vertex[vtx.index]
        pt = self.deref(vtx, "point")
        if pt is None:
            raise ConvertError("unsupported_topology", "vertex id %s has no point" % vtx.f.get("node_id"))
        p = pt.f["pvec"]
        self._vertex_pos[vtx.index] = p
        vid = self.w.add("VERTEX_POINT('',#%d)" % self.w.point(p))
        self._vertex[vtx.index] = vid
        self._bbox_add(bbox, p)
        return vid

    def _bbox_add(self, bbox, p: Vec) -> None:
        lo, hi = bbox
        for k in range(3):
            lo[k] = min(lo[k], p[k])
            hi[k] = max(hi[k], p[k])

    def synthetic_vertex(self, key: int, p: Vec, bbox) -> int:
        k = -1_000_000 - key
        if k in self._vertex:
            return self._vertex[k]
        self._vertex_pos[k] = p
        vid = self.w.add("VERTEX_POINT('',#%d)" % self.w.point(p))
        self._vertex[k] = vid
        self._bbox_add(bbox, p)
        return vid

    # -- edges ---------------------------------------------------------------------
    def edge_curve(self, edge: Node, bbox) -> Tuple[int, int, int]:
        if edge.index in self._edge:
            return self._edge[edge.index]
        fin = self.deref(edge, "halfedge")
        other = self.deref(fin, "other") if fin is not None else None
        v_end = self.deref(fin, "vertex") if fin is not None else None
        v_start = self.deref(other, "vertex") if other is not None else None
        curve_node = self.deref(edge, "curve")
        tolerant = False
        if curve_node is None:
            # tolerant edge: the fins carry trimmed SP-curves
            tolerant = True
            fin_curve = self.deref(fin, "curve")
            if fin_curve is None and other is not None:
                fin_curve = self.deref(other, "curve")
            if fin_curve is None:
                raise ConvertError("unsupported_topology", "edge id %s has neither a curve nor fin curves" % edge.f.get("node_id"))
            curve_node = fin_curve
            self.warn("tolerant_edge", "tolerant_edge: tolerant edges rebuilt from their fin SP-curves")
        p_start = self._vertex_point(v_start)
        p_end = self._vertex_point(v_end)
        cid, same_sense, ev = self.curve(curve_node, p_start, p_end)
        if v_start is None or v_end is None:
            # ring edge: closed curve, synthesise one vertex at the curve start
            p0 = ev(0.0)
            sv = ev_id = self.synthetic_vertex(edge.index, p0, bbox)
            vs = ve = sv
            sv_idx = ve_idx = -1_000_000 - edge.index
        else:
            vs = self.vertex(v_start, bbox)
            ve = self.vertex(v_end, bbox)
            sv_idx, ve_idx = v_start.index, v_end.index
            if tolerant or getattr(ev, "approx", False):
                # approximated curves: orient by the vertices, not by the sense flags
                ps, pe = ev(0.0), ev(1.0)
                if dist(ps, p_start) + dist(pe, p_end) > dist(ps, p_end) + dist(pe, p_start):
                    same_sense = not same_sense
        ec = self.w.add("EDGE_CURVE('',#%d,#%d,#%d,%s)" % (vs, ve, cid, ".T." if same_sense else ".F."))
        res = (ec, sv_idx, ve_idx)
        self._edge[edge.index] = res
        return res

    def _vertex_point(self, v: Optional[Node]) -> Optional[Vec]:
        if v is None:
            return None
        pt = self.deref(v, "point")
        return pt.f["pvec"] if pt is not None else None

    # -- curves ---------------------------------------------------------------------
    def curve(self, c: Node, p_start: Optional[Vec], p_end: Optional[Vec],
              prange: Optional[Tuple[float, float]] = None) -> Tuple[int, bool, Any]:
        """Returns (STEP curve id, edge_same_sense_vs_curve, evaluator(s in [0,1]))."""
        t = c.type
        sense_pos = c.f.get("sense", "+") == "+"
        if t == 30:  # LINE
            p = c.f["pvec"]
            d = unit(c.f["direction"])
            cid = self.w.add("LINE('',#%d,#%d)" % (self.w.point(p), self.w.add("VECTOR('',#%d,1.)" % self.w.direction(d))))
            # evaluator: between the vertices when known
            a = p_start if p_start is not None else p
            b = p_end if p_end is not None else add(p, d)
            if not sense_pos:
                a, b = b, a

            def ev_line(s: float, a=a, b=b) -> Vec:
                return geom.lerp(a, b, s)
            return cid, sense_pos, ev_line
        if t == 31:  # CIRCLE
            c0, n, x, r = c.f["centre"], c.f["normal"], c.f["x_axis"], c.f["radius"]
            cid = self.w.add("CIRCLE('',#%d,%s)" % (self.w.axis2(c0, n, x), fmm(r)))
            xx = unit(x)
            yy = cross(unit(n), xx)

            def ev_circ(s: float, c0=c0, xx=xx, yy=yy, r=r) -> Vec:
                a = 2 * math.pi * s
                return add(c0, add(mul(xx, r * math.cos(a)), mul(yy, r * math.sin(a))))
            return cid, sense_pos, ev_circ
        if t == 32:  # ELLIPSE
            c0, n, x = c.f["centre"], c.f["normal"], c.f["x_axis"]
            a_r, b_r = c.f["major_radius"], c.f["minor_radius"]
            cid = self.w.add("ELLIPSE('',#%d,%s,%s)" % (self.w.axis2(c0, n, x), fmm(a_r), fmm(b_r)))
            xx = unit(x)
            yy = cross(unit(n), xx)

            def ev_ell(s: float, c0=c0, xx=xx, yy=yy, a_r=a_r, b_r=b_r) -> Vec:
                a = 2 * math.pi * s
                return add(c0, add(mul(xx, a_r * math.cos(a)), mul(yy, b_r * math.sin(a))))
            return cid, sense_pos, ev_ell
        if t == 134:  # B_CURVE
            nc = self.nurbs_curve(c)
            if nc.dim != 3:
                raise ConvertError("unsupported_curve", "2D B-curve id %s used directly by an edge" % c.f.get("node_id"))
            if prange is not None:
                nc = _restrict_curve(nc, prange)
            cid = self.emit_bspline_curve(nc)
            return cid, sense_pos, _ev_nurbs(nc)
        if t == 133:  # TRIMMED_CURVE
            basis = self.deref(c, "basis_curve")
            if basis is None:
                raise ConvertError("unsupported_curve", "trimmed curve id %s without basis" % c.f.get("node_id"))
            p1, p2 = c.f["parm_1"], c.f["parm_2"]
            increasing = p2 > p1
            rng = (min(p1, p2), max(p1, p2))
            bs_id, _, ev = self.curve(basis, c.f["point_1"] if increasing else c.f["point_2"],
                                      c.f["point_2"] if increasing else c.f["point_1"], prange=rng)
            # edge runs from point_1 to point_2 (trimmed sense is '+'); basis param increases
            # from point_1 to point_2 iff p2 > p1
            same = increasing if sense_pos else (not increasing)
            return bs_id, same, ev
        if t == 38:  # INTERSECTION
            nc = None
            for sp_idx in c.f["surface"]:
                sn = self.get(sp_idx)
                if sn is not None and sn.type == 59:
                    nc = self.blend_boundary_curve(sn, self.intersection_points(c))
                    if nc is not None:
                        if prange is not None:
                            pass
                        break
            if nc is None:
                pts, tangents = self.intersection_curve(c)
                nc = geom.interpolate_cubic(pts, tangents)
            self.warn("intersection_curve", "intersection_curve: surface/surface intersection edges approximated by interpolating cubic splines through the XT chart points")
            cid = self.emit_bspline_curve(nc)
            ev = _ev_nurbs(nc)
            ev.approx = True
            return cid, sense_pos, ev
        if t == 137:  # SP_CURVE
            nc3, exact = self.sp_curve_3d(c, prange)
            if not exact:
                self.warn("sp_curve", "sp_curve: curves defined in surface parameter space approximated by cubic splines through evaluated points")
            cid = self.emit_bspline_curve(nc3)
            ev = _ev_nurbs(nc3)
            ev.approx = not exact
            return cid, sense_pos, ev
        if t == 200:  # POLYLINE
            data = self.deref(c, "data")
            pv = self.deref(data, "pvec") if data is not None else None
            if pv is None:
                raise ConvertError("unsupported_curve", "polyline id %s without points" % c.f.get("node_id"))
            pts = list(pv.f["values"])
            ids = [self.w.point(p) for p in pts]
            cid = self.w.add("POLYLINE('',(%s))" % ",".join("#%d" % i for i in ids))

            def ev_poly(s: float, pts=pts) -> Vec:
                i = min(int(s * (len(pts) - 1)), len(pts) - 2)
                f = s * (len(pts) - 1) - i
                return geom.lerp(pts[i], pts[i + 1], f)
            return cid, sense_pos, ev_poly
        if t == 130:
            raise ConvertError("unsupported_curve", "foreign (PE) curve id %s cannot be represented" % c.f.get("node_id"))
        raise ConvertError("unsupported_curve", "curve node type %s (id %s) is not supported" % (c.name, c.f.get("node_id")))

    def nurbs_curve(self, c: Node) -> geom.NurbsCurve:
        nb = self.deref(c, "nurbs")
        if nb is None:
            raise ConvertError("unsupported_curve", "B-curve id %s without NURBS data" % c.f.get("node_id"))
        deg = nb.f["degree"]
        nv = nb.f["n_vertices"]
        vdim = nb.f["vertex_dim"]
        rational = bool(nb.f["rational"])
        verts = self.deref(nb, "bspline_vertices").f["vertices"]
        mult = self.deref(nb, "knot_mult").f["mult"]
        kn = self.deref(nb, "knots").f["knots"]
        knots = geom.expand_knots(kn, mult)
        ctrl = []
        weights = [] if rational else None
        for i in range(nv):
            row = verts[i * vdim:(i + 1) * vdim]
            if rational:
                w = row[-1]
                ctrl.append(tuple(x / w for x in row[:-1]))
                weights.append(w)
            else:
                ctrl.append(tuple(row))
        try:
            return geom.NurbsCurve(deg, ctrl, knots, weights)
        except geom.GeomError as exc:
            raise ConvertError("unsupported_curve", "B-curve id %s: %s" % (c.f.get("node_id"), exc))

    def nurbs_surface(self, s: Node) -> geom.NurbsSurface:
        nb = self.deref(s, "nurbs")
        if nb is None:
            raise ConvertError("unsupported_surface", "B-surface id %s without NURBS data" % s.f.get("node_id"))
        p, q = nb.f["u_degree"], nb.f["v_degree"]
        nu, nvv = nb.f["n_u_vertices"], nb.f["n_v_vertices"]
        vdim = nb.f["vertex_dim"]
        rational = bool(nb.f["rational"])
        verts = self.deref(nb, "bspline_vertices").f["vertices"]
        uk = geom.expand_knots(self.deref(nb, "u_knots").f["knots"], self.deref(nb, "u_knot_mult").f["mult"])
        vk = geom.expand_knots(self.deref(nb, "v_knots").f["knots"], self.deref(nb, "v_knot_mult").f["mult"])
        ctrl: List[List[Vec]] = []
        weights: Optional[List[List[float]]] = [] if rational else None
        for i in range(nu):
            row = []
            wrow = []
            for j in range(nvv):
                k = (i * nvv + j) * vdim
                v = verts[k:k + vdim]
                if rational:
                    w = v[-1]
                    row.append((v[0] / w, v[1] / w, v[2] / w))
                    wrow.append(w)
                else:
                    row.append((v[0], v[1], v[2]))
            ctrl.append(row)
            if rational:
                weights.append(wrow)
        try:
            return geom.NurbsSurface(p, q, ctrl, uk, vk, weights)
        except geom.GeomError as exc:
            raise ConvertError("unsupported_surface", "B-surface id %s: %s" % (s.f.get("node_id"), exc))

    def emit_bspline_curve(self, nc: geom.NurbsCurve) -> int:
        nc = nc.clamped()
        nc = _normalise_curve_knots(nc)
        pts = ",".join("#%d" % self.w.point(p) for p in nc.ctrl)
        distinct: List[float] = []
        mults: List[int] = []
        for k in nc.knots:
            if distinct and abs(k - distinct[-1]) <= 0.0:
                mults[-1] += 1
            else:
                distinct.append(k)
                mults.append(1)
        closed = ".T." if dist(nc.eval(nc.t0), nc.eval(nc.t1)) < 1e-9 else ".F."
        kstr = "(%s),(%s)" % (",".join(str(m) for m in mults), ",".join(fmt(k) for k in distinct))
        if nc.weights:
            wstr = ",".join(fmt(w) for w in nc.weights)
            return self.w.add("(BOUNDED_CURVE()B_SPLINE_CURVE(%d,(%s),.UNSPECIFIED.,%s,.F.)B_SPLINE_CURVE_WITH_KNOTS(%s,.UNSPECIFIED.)CURVE()GEOMETRIC_REPRESENTATION_ITEM()RATIONAL_B_SPLINE_CURVE((%s))REPRESENTATION_ITEM(''))"
                             % (nc.degree, pts, closed, kstr, wstr))
        return self.w.add("B_SPLINE_CURVE_WITH_KNOTS('',%d,(%s),.UNSPECIFIED.,%s,.F.,%s,.UNSPECIFIED.)"
                          % (nc.degree, pts, closed, kstr))

    def emit_bspline_surface(self, ns: geom.NurbsSurface) -> int:
        ns = ns.clamped()
        ns = _normalise_surface_knots(ns)
        rows = []
        for row in ns.ctrl:
            rows.append("(%s)" % ",".join("#%d" % self.w.point(p) for p in row))
        grid = "(%s)" % ",".join(rows)

        def km(knots: List[float]) -> Tuple[str, str]:
            d: List[float] = []
            m: List[int] = []
            for k in knots:
                if d and k == d[-1]:
                    m[-1] += 1
                else:
                    d.append(k)
                    m.append(1)
            return ",".join(str(x) for x in m), ",".join(fmt(x) for x in d)
        um, uk = km(ns.u_knots)
        vm, vk = km(ns.v_knots)
        uclosed = ".T." if all(dist(ns.ctrl[0][j], ns.ctrl[-1][j]) < 1e-9 for j in range(ns.n_v)) else ".F."
        vclosed = ".T." if all(dist(ns.ctrl[i][0], ns.ctrl[i][-1]) < 1e-9 for i in range(ns.n_u)) else ".F."
        if ns.weights:
            wgrid = "(%s)" % ",".join("(%s)" % ",".join(fmt(w) for w in row) for row in ns.weights)
            return self.w.add("(BOUNDED_SURFACE()B_SPLINE_SURFACE(%d,%d,%s,.UNSPECIFIED.,%s,%s,.F.)B_SPLINE_SURFACE_WITH_KNOTS((%s),(%s),(%s),(%s),.UNSPECIFIED.)GEOMETRIC_REPRESENTATION_ITEM()RATIONAL_B_SPLINE_SURFACE(%s)REPRESENTATION_ITEM('')SURFACE())"
                             % (ns.p, ns.q, grid, uclosed, vclosed, um, vm, uk, vk, wgrid))
        return self.w.add("B_SPLINE_SURFACE_WITH_KNOTS('',%d,%d,%s,.UNSPECIFIED.,%s,%s,.F.,(%s),(%s),(%s),(%s),.UNSPECIFIED.)"
                          % (ns.p, ns.q, grid, uclosed, vclosed, um, vm, uk, vk))

    # -- intersection curves ----------------------------------------------------------
    def intersection_points(self, c: Node) -> List[Vec]:
        chart = self.deref(c, "chart")
        if chart is None:
            raise ConvertError("unsupported_curve", "intersection curve id %s without chart" % c.f.get("node_id"))
        pts: List[Vec] = [tuple(h) for h in chart.f["hvec"]]
        start = self.deref(c, "start")
        end = self.deref(c, "end")
        # 'T' terminators hold the exact singular end point (just outside the chart);
        # 'L' limits hold the artificial end of the curve, which may also lie beyond
        # the last chart point. 'H' help points belong to closed curves and are ignored.
        if start is not None and start.f.get("type") in ("T", "L") and start.f["hvec"]:
            q = tuple(start.f["hvec"][0])
            if not pts or dist(q, pts[0]) > 1e-9:
                pts = [q] + pts
        if end is not None and end.f.get("type") in ("T", "L") and end.f["hvec"]:
            q = tuple(end.f["hvec"][0])
            if not pts or dist(q, pts[-1]) > 1e-9:
                pts = pts + [q]
        clean: List[Vec] = []
        for p in pts:
            if not clean or dist(clean[-1], p) > 1e-10:
                clean.append(p)
        if len(clean) < 2:
            raise ConvertError("unsupported_curve", "intersection curve id %s has fewer than two distinct chart points" % c.f.get("node_id"))
        return clean

    def intersection_evaluators(self, c: Node) -> Optional[List[Tuple[geom.Surface, bool]]]:
        evs = []
        for p in c.f["surface"]:
            sn = self.get(p)
            if sn is None:
                return None
            try:
                info = self.surface(sn)
            except ConvertError:
                return None
            if info.ev is None:
                return None
            evs.append((info.ev, sn.f.get("sense") == "+"))
        return evs

    def blend_boundary_curve(self, bb: Node, ref_pts: List[Vec]) -> Optional[geom.NurbsCurve]:
        """The iso-curve of an approximated blend along the support named by a BLEND_BOUND."""
        blend = self.deref(bb, "blend")
        if blend is None or blend.type != 56:
            return None
        try:
            ns = self.blend_surface(blend)
        except ConvertError:
            return None
        spine = self.deref(blend, "spine")
        swap = spine is not None and spine.f.get("sense", "+") != "+"
        support_index = 1 - int(bb.f.get("boundary", 0))
        at_v0 = (support_index == 0) != swap
        col = 0 if at_v0 else ns.n_v - 1
        ctrl = [tuple(ns.ctrl[i][col]) for i in range(ns.n_u)]
        weights = [ns.weights[i][col] for i in range(ns.n_u)] if ns.weights else None
        nc = geom.NurbsCurve(ns.p, ctrl, list(ns.u_knots), weights)
        if ref_pts:
            a, b = nc.eval(nc.t0), nc.eval(nc.t1)
            if dist(a, ref_pts[0]) + dist(b, ref_pts[-1]) > dist(a, ref_pts[-1]) + dist(b, ref_pts[0]):
                nc = _reverse_curve(nc)
            # the iso-curve must actually pass through the chart, else it is not this boundary
            worst = 0.0
            for q in ref_pts:
                worst = max(worst, _curve_point_distance(nc, q))
            if worst > 2e-6:
                return None
        return nc

    def intersection_curve(self, c: Node) -> Tuple[List[Vec], Optional[List[Vec]]]:
        """Chart points (plus terminators), refined on the two surfaces when possible."""
        pts = self.intersection_points(c)
        evs = self.intersection_evaluators(c)
        if evs is None:
            return pts, None
        tans = _tangents_from_surfaces(pts, evs)
        if tans is None:
            return pts, None
        return _refine_on_surfaces(pts, tans, evs)

    # -- SP curves --------------------------------------------------------------------
    def sp_curve_3d(self, c: Node, prange: Optional[Tuple[float, float]]) -> Tuple[geom.NurbsCurve, bool]:
        host = self.deref(c, "surface")
        bc = self.deref(c, "b_curve")
        if host is None or bc is None:
            raise ConvertError("unsupported_curve", "SP-curve id %s without surface or 2D curve" % c.f.get("node_id"))
        c2 = self.nurbs_curve(bc)
        if c2.dim != 2:
            raise ConvertError("unsupported_curve", "SP-curve id %s: embedded curve is not 2D" % c.f.get("node_id"))
        info = self.surface(host)
        ev = info.ev
        if ev is None:
            raise ConvertError("unsupported_curve", "SP-curve id %s on a surface that cannot be evaluated (%s)" % (c.f.get("node_id"), host.name))
        t0, t1 = c2.t0, c2.t1
        if prange is not None:
            t0, t1 = max(t0, prange[0]), min(t1, prange[1])
            if t1 <= t0:
                t0, t1 = c2.t0, c2.t1
        if host.type == 50:
            # affine map (u,v) -> P + uX + vY is exact for B-splines
            pl = ev
            ctrl3 = [pl.eval(p[0], p[1]) for p in c2.ctrl]
            nc = geom.NurbsCurve(c2.degree, ctrl3, list(c2.knots), list(c2.weights) if c2.weights else None)
            if prange is not None:
                nc = _restrict_curve(nc, (t0, t1))
            return nc, True

        def point_tangent(t: float) -> Tuple[Vec, Vec]:
            (u, v), (du, dv) = c2.eval_deriv(t)
            if isinstance(ev, geom.NurbsSurfaceAdapter):
                S, Su, Sv = ev.s.eval_derivs(u, v)
                tv = add(mul(Su, du), mul(Sv, dv))
            else:
                S = ev.eval(u, v)
                h = 1e-7
                S2 = ev.eval(u + du * h, v + dv * h)
                tv = mul(sub(S2, S), 1.0 / h)
            return S, tv

        # adaptive sampling: knot spans plus a base grid, subdivided until the
        # cubic Hermite midpoint matches the exact point to 1e-7 m
        params: List[float] = sorted({t0, t1} | {k for k in c2.knots if t0 < k < t1})
        n0 = max(8, 4 * max(1, len(c2.ctrl)))
        base = [t0 + (t1 - t0) * i / n0 for i in range(n0 + 1)]
        params = sorted(set(params) | set(base))
        data = {t: point_tangent(t) for t in params}
        stack = [(params[i], params[i + 1]) for i in range(len(params) - 1)]
        budget = 0
        while stack and budget < 20000:
            a, b = stack.pop()
            budget += 1
            pa, ta = data[a]
            pb, tb = data[b]
            m = 0.5 * (a + b)
            pm, tm = point_tangent(m)
            hm = _hermite_mid(pa, ta, pb, tb)
            if dist(hm, pm) > 1e-7 and (b - a) > 1e-9 * (t1 - t0):
                data[m] = (pm, tm)
                stack.append((a, m))
                stack.append((m, b))
        params = sorted(data)
        pts = [data[t][0] for t in params]
        tans: List[Vec] = []
        for i, t in enumerate(params):
            tv = data[t][1]
            if norm(tv) < 1e-14:
                tans.append(unit(sub(pts[min(i + 1, len(pts) - 1)], pts[max(i - 1, 0)])))
            else:
                tans.append(unit(tv))
        return geom.interpolate_cubic(pts, tans), False

    # -- surfaces ---------------------------------------------------------------------
    # -- surfaces ---------------------------------------------------------------------
    def surface(self, s: Node) -> _SurfaceInfo:
        if s.index in self._surf:
            return self._surf[s.index]
        info = self._surface(s)
        self._surf[s.index] = info
        return info

    def _surface(self, s: Node) -> _SurfaceInfo:
        t = s.type
        w = self.w
        if t == 50:
            ev = geom.Plane(s.f["pvec"], s.f["normal"], s.f["x_axis"])
            return _SurfaceInfo(w.add("PLANE('',#%d)" % w.axis2(ev.p, ev.n, ev.x)), True, ev)
        if t == 51:
            ev = geom.Cylinder(s.f["pvec"], s.f["axis"], s.f["radius"], s.f["x_axis"])
            return _SurfaceInfo(w.add("CYLINDRICAL_SURFACE('',#%d,%s)" % (w.axis2(ev.p, ev.a, ev.x), fmm(ev.r))), True, ev)
        if t == 52:
            ev = geom.Cone(s.f["pvec"], s.f["axis"], s.f["radius"], s.f["sin_half_angle"], s.f["cos_half_angle"], s.f["x_axis"])
            half = math.atan2(ev.sin, ev.cos)
            sid = w.add("CONICAL_SURFACE('',#%d,%s,%s)" % (w.axis2(ev.p, ev.a, ev.x), fmm(ev.r), fmt(half)))
            return _SurfaceInfo(sid, True, ev)
        if t == 53:
            ev = geom.Sphere(s.f["centre"], s.f["radius"], s.f["axis"], s.f["x_axis"])
            return _SurfaceInfo(w.add("SPHERICAL_SURFACE('',#%d,%s)" % (w.axis2(ev.c, ev.a, ev.x), fmm(ev.r))), True, ev)
        if t == 54:
            ev = geom.Torus(s.f["centre"], s.f["axis"], s.f["major_radius"], s.f["minor_radius"], s.f["x_axis"])
            if ev.R > ev.rr:
                sid = w.add("TOROIDAL_SURFACE('',#%d,%s,%s)" % (w.axis2(ev.c, ev.a, ev.x), fmm(ev.R), fmm(ev.rr)))
                return _SurfaceInfo(sid, True, ev)
            if ev.R > 0:
                # apple: outer part of a self-intersecting torus, same parameterisation
                sid = w.add("DEGENERATE_TOROIDAL_SURFACE('',#%d,%s,%s,.T.)" % (w.axis2(ev.c, ev.a, ev.x), fmm(ev.R), fmm(ev.rr)))
                return _SurfaceInfo(sid, True, ev)
            # lemon (R < 0): XT point (u, v) == ISO torus(|R|) point (u+pi, v+pi); the
            # parameter shift keeps the natural normal, so match stays True
            sid = w.add("DEGENERATE_TOROIDAL_SURFACE('',#%d,%s,%s,.F.)" % (w.axis2(ev.c, ev.a, mul(ev.x, -1.0)), fmm(-ev.R), fmm(ev.rr)))
            self.warn("lemon_torus", "lemon_torus: inner self-intersecting torus mapped to DEGENERATE_TOROIDAL_SURFACE")
            return _SurfaceInfo(sid, True, ev)
        if t == 124:
            ns = self.nurbs_surface(s)
            sid = self.emit_bspline_surface(ns)
            ext = None
            data = self.deref(s, "data")
            if data is not None:
                eu = data.f.get("extended_uint")
                ev_ = data.f.get("extended_vint")
                if eu and ev_ and not _is_null(eu[0]) and not _is_null(ev_[0]):
                    du = ns.u1 - ns.u0
                    dv = ns.v1 - ns.v0
                    ext = (min(eu[0], ns.u0 - 0.25 * du), max(eu[1], ns.u1 + 0.25 * du),
                           min(ev_[0], ns.v0 - 0.25 * dv), max(ev_[1], ns.v1 + 0.25 * dv))
            return _SurfaceInfo(sid, True, geom.NurbsSurfaceAdapter(ns, ext))
        if t == 67:  # SWEPT_SURF
            section = self.deref(s, "section")
            if section is None:
                raise ConvertError("unsupported_surface", "swept surface id %s without section curve" % s.f.get("node_id"))
            sec_id, _, sec_ev = self.curve(section, None, None)
            cev = self._curve_param_evaluator(section)
            sweep = unit(s.f["sweep"])
            sid = w.add("SURFACE_OF_LINEAR_EXTRUSION('',#%d,#%d)" % (sec_id, w.add("VECTOR('',#%d,1.)" % w.direction(sweep))))
            ev = geom.SweptSurface(cev[0], cev[1], cev[2], sweep) if cev is not None else None
            return _SurfaceInfo(sid, True, ev)
        if t == 68:  # SPUN_SURF
            profile = self.deref(s, "profile")
            if profile is None:
                raise ConvertError("unsupported_surface", "spun surface id %s without profile curve" % s.f.get("node_id"))
            prof_id, _, _ = self.curve(profile, None, None)
            cev = self._curve_param_evaluator(profile)
            base, axis = s.f["base"], unit(s.f["axis"])
            sid = w.add("SURFACE_OF_REVOLUTION('',#%d,#%d)" % (prof_id, w.add("AXIS1_PLACEMENT('',#%d,#%d)" % (w.point(base), w.direction(axis)))))
            ev = geom.SpunSurface(cev[0], cev[1], cev[2], base, axis) if cev is not None else None
            return _SurfaceInfo(sid, True, ev)
        if t == 60:  # OFFSET_SURF
            under = self.deref(s, "surface")
            if under is None:
                raise ConvertError("unsupported_surface", "offset surface id %s without underlying surface" % s.f.get("node_id"))
            uinfo = self.surface(under)
            off = s.f["offset"]
            if under.f.get("sense", "+") != "+":
                off = -off
            # STEP offsets along the basis' natural normal; if the basis STEP surface has the
            # opposite natural normal (cone), negate
            step_off = off if uinfo.match else -off
            sid = w.add("OFFSET_SURFACE('',#%d,%s,.F.)" % (uinfo.step_id, fmm(step_off)))
            ev = _OffsetEval(uinfo.ev, off) if uinfo.ev is not None else None
            return _SurfaceInfo(sid, uinfo.match, ev)
        if t == 56:  # BLENDED_EDGE
            ns = self.blend_surface(s)
            sid = self.emit_bspline_surface(ns)
            self.warn("rolling_ball_blend", "rolling_ball_blend: exact rolling-ball blend surfaces approximated by interpolating rational B-spline surfaces (exact circular cross-sections)")
            du = ns.u1 - ns.u0
            dv = ns.v1 - ns.v0
            ext = (ns.u0 - 0.5 * du, ns.u1 + 0.5 * du, ns.v0 - 0.75 * dv, ns.v1 + 0.75 * dv)
            return _SurfaceInfo(sid, True, geom.NurbsSurfaceAdapter(ns, ext), approx=True)
        if t == 59:
            raise ConvertError("unsupported_surface", "blend boundary surface id %s used as a face surface" % s.f.get("node_id"))
        if t == 120:
            raise ConvertError("unsupported_surface", "foreign (PE) surface id %s cannot be represented" % s.f.get("node_id"))
        if t == 201:
            raise ConvertError("unsupported_surface", "mesh (facet) surface id %s is not a B-rep surface" % s.f.get("node_id"))
        raise ConvertError("unsupported_surface", "surface node type %s (id %s) is not supported" % (s.name, s.f.get("node_id")))

    def _curve_param_evaluator(self, c: Node):
        """(fn(t) -> (point, tangent), t0, t1) in the curve's own parameter for analytic/B curves."""
        t = c.type
        if t == 30:
            p, d = c.f["pvec"], unit(c.f["direction"])
            return (lambda tt, p=p, d=d: (add(p, mul(d, tt)), d)), -1e3, 1e3
        if t == 31:
            c0, n, x, r = c.f["centre"], unit(c.f["normal"]), unit(c.f["x_axis"]), c.f["radius"]
            y = cross(n, x)
            return (lambda tt, c0=c0, x=x, y=y, r=r: (add(c0, add(mul(x, r * math.cos(tt)), mul(y, r * math.sin(tt)))),
                                                     add(mul(x, -r * math.sin(tt)), mul(y, r * math.cos(tt))))), 0.0, 2 * math.pi
        if t == 32:
            c0, n, x = c.f["centre"], unit(c.f["normal"]), unit(c.f["x_axis"])
            a, b = c.f["major_radius"], c.f["minor_radius"]
            y = cross(n, x)
            return (lambda tt, c0=c0, x=x, y=y, a=a, b=b: (add(c0, add(mul(x, a * math.cos(tt)), mul(y, b * math.sin(tt)))),
                                                          add(mul(x, -a * math.sin(tt)), mul(y, b * math.cos(tt))))), 0.0, 2 * math.pi
        if t == 134:
            nc = self.nurbs_curve(c)
            if nc.dim != 3:
                return None
            return (lambda tt, nc=nc: nc.eval_deriv(tt)), nc.t0, nc.t1
        return None

    # -- rolling-ball blends -----------------------------------------------------------
    def blend_surface(self, s: Node) -> geom.NurbsSurface:
        if s.index in self._blend:
            return self._blend[s.index]
        nid = s.f.get("node_id")
        spine = self.deref(s, "spine")
        if spine is None:
            raise ConvertError("unsupported_surface", "blend surface id %s without spine" % nid)
        supports = [self.get(p) for p in s.f["surface"]]
        if any(x is None for x in supports):
            raise ConvertError("unsupported_surface", "blend surface id %s with missing support surface" % nid)
        sup_ev: List[geom.Surface] = []
        for sn in supports:
            if sn.type == 56 and all(abs(x) < 1e-12 for x in sn.f["range"]):
                # cliff edge: the degenerate blend's spine is the sharp edge the ball rolls on
                sup_ev.append(self.curve_support(sn))
                self.warn("cliff_edge_blend", "cliff_edge_blend: cliff-edge blends built as rolling-ball blends between a face and the cliff edge curve")
                continue
            info = self.surface(sn)
            if info.ev is None:
                raise ConvertError("unsupported_surface", "blend surface id %s: support %s cannot be evaluated" % (nid, sn.name))
            sup_ev.append(info.ev)
        rng = s.f["range"]
        # the spine lies on the supports offset by range[k] along their sense-adjusted normals
        offsets: List[Tuple[geom.Surface, bool]] = []
        for k, sn in enumerate(supports):
            sign = 1.0 if sn.f.get("sense", "+") == "+" else -1.0
            offsets.append((_OffsetEval(sup_ev[k], rng[k] * sign), True))
        spine_pts, spine_fn = self.spine_samples(spine, s, offsets)
        if len(spine_pts) < 2:
            raise ConvertError("unsupported_surface", "blend surface id %s: spine too short" % nid)
        swap = spine.f.get("sense", "+") != "+"
        expect = max(abs(rng[0]), abs(rng[1]))
        samples = []   # (cpt, f0, f1, ok)
        for cpt in spine_pts:
            feet = []
            for ev in sup_ev:
                u, v = ev.project(cpt)
                feet.append(ev.eval(u, v))
            f0, f1 = (feet[1], feet[0]) if swap else (feet[0], feet[1])
            r0, r1 = dist(f0, cpt), dist(f1, cpt)
            ok = abs(r0 - r1) <= 0.02 * max(r0, r1) + 1e-9 and (expect == 0 or
                                                                  (abs(r0 - expect) <= 0.02 * expect and abs(r1 - expect) <= 0.02 * expect))
            if not ok:
                feet = [_careful_project(ev, cpt) for ev in sup_ev]
                f0, f1 = (feet[1], feet[0]) if swap else (feet[0], feet[1])
                r0, r1 = dist(f0, cpt), dist(f1, cpt)
                ok = abs(r0 - r1) <= 0.02 * max(r0, r1) + 1e-9 and (expect == 0 or
                                                                      (abs(r0 - expect) <= 0.02 * expect and abs(r1 - expect) <= 0.02 * expect))
            samples.append((cpt, f0, f1, ok, r0, r1))
        # longest contiguous run of valid cross-sections
        best_run = (0, 0)
        i = 0
        while i < len(samples):
            if samples[i][3]:
                j = i
                while j < len(samples) and samples[j][3]:
                    j += 1
                if j - i > best_run[1] - best_run[0]:
                    best_run = (i, j)
                i = j
            else:
                i += 1
        n_valid = best_run[1] - best_run[0]
        if n_valid < 2 or n_valid < 0.5 * len(samples):
            bad = next(x for x in samples if not x[3])
            raise ConvertError("unsupported_surface",
                               "blend surface id %s: support foot points are not at the blend radius along the spine (%.4g vs %.4g m, expected %.4g; %d of %d cross-sections valid); blend type %r"
                               % (nid, bad[4], bad[5], expect, n_valid, len(samples), s.f.get("blend_type")))
        if n_valid < len(samples):
            self.warn("blend_trimmed", "blend_trimmed: rolling-ball blend spines trimmed where they overshoot a bounded support surface")
        valid = samples[best_run[0]:best_run[1]]
        max_ang = 0.0
        for cpt, f0, f1, ok, r0, r1 in valid:
            a = _section_angle(cpt, f0, f1)
            max_ang = max(max_ang, a)
        if max_ang <= 0.0:
            raise ConvertError("unsupported_surface", "blend surface id %s: every cross-section is degenerate" % nid)
        nseg = max(1, int(math.ceil(max_ang / (math.pi / 2) - 1e-9)))

        def row_of(cpt: Vec, f0: Vec, f1: Vec) -> geom.NurbsCurve:
            r = 0.5 * (dist(f0, cpt) + dist(f1, cpt))
            xv = unit(sub(f0, cpt))
            d1 = sub(f1, cpt)
            yv = sub(d1, mul(xv, dot(d1, xv)))
            if norm(yv) < 1e-7 * r:
                return _degenerate_arc(add(cpt, mul(xv, r)), nseg)
            yv = unit(yv)
            ang = math.atan2(dot(d1, yv), dot(d1, xv))
            if ang <= 1e-9:
                return _degenerate_arc(add(cpt, mul(xv, r)), nseg)
            return geom.arc_nurbs(cpt, xv, yv, r, 0.0, ang, nseg=nseg)

        def feet_of(cpt: Vec):
            feet = []
            for ev in sup_ev:
                u, v = ev.project(cpt)
                feet.append(ev.eval(u, v))
            return (feet[1], feet[0]) if swap else (feet[0], feet[1])

        entries = [(cpt, row_of(cpt, f0, f1), t) for (cpt, f0, f1, ok, r0, r1), t in
                   zip(valid, [i / (len(samples) - 1) for i in range(best_run[0], best_run[1])])]
        # drop coincident spine samples
        dedup = [entries[0]]
        for e in entries[1:]:
            if dist(e[0], dedup[-1][0]) > 1e-12:
                dedup.append(e)
        entries = dedup
        if len(entries) < 2:
            raise ConvertError("unsupported_surface", "blend surface id %s: spine too short" % nid)

        def build(ents):
            # drop coincident spine samples, which would give zero-length parameter steps
            kept = [ents[0]]
            for e in ents[1:]:
                if dist(e[0], kept[-1][0]) > 1e-10:
                    kept.append(e)
            ents[:] = kept
            params: List[float] = [0.0]
            for i in range(1, len(ents)):
                params.append(params[-1] + dist(ents[i - 1][0], ents[i][0]))
            try:
                return geom.interpolate_cubic_surface_rows([e[1] for e in ents], params), params
            except geom.GeomError as exc:
                raise ConvertError("unsupported_surface", "blend surface id %s: %s" % (nid, exc))

        ns, params = build(entries)
        spine_is_int = spine.type == 38
        prev_worst = float("inf")
        for _round in range(4):
            if len(entries) >= 240:
                break
            inserts = []
            round_worst = 0.0
            for i in range(len(entries) - 1):
                ca, cb = entries[i][0], entries[i + 1][0]
                if dist(ca, cb) < 1e-7:
                    continue
                # exact mid spine point: re-project the chord midpoint onto the offset
                # surfaces (intersection spines) or evaluate the curve (analytic spines)
                m = geom.lerp(ca, cb, 0.5)
                tm = 0.5 * (entries[i][2] + entries[i + 1][2])
                q = _project_both(m, offsets) if spine_is_int else spine_fn(tm)
                if q is None or dist(q, m) > 0.5 * dist(ca, cb):
                    continue
                if dist(q, ca) < 1e-9 or dist(q, cb) < 1e-9:
                    continue
                f0, f1 = feet_of(q)
                r0, r1 = dist(f0, q), dist(f1, q)
                if abs(r0 - r1) > 0.02 * max(r0, r1) + 1e-9:
                    continue
                row = row_of(q, f0, f1)
                um = 0.5 * (params[i] + params[i + 1])
                worst = 0.0
                for k in range(5):
                    vv = k / 4.0
                    exact = row.eval(vv)
                    approx = ns.eval(um, vv)
                    worst = max(worst, dist(exact, approx))
                round_worst = max(round_worst, worst)
                if worst > 1e-6:
                    inserts.append((i, worst, (q, row, tm)))
            if not inserts or round_worst > 0.75 * prev_worst and _round > 0:
                break
            prev_worst = round_worst
            inserts.sort(key=lambda t: -t[1])
            room = max(0, 240 - len(entries))
            chosen = sorted(inserts[:room], key=lambda t: t[0])
            for i, _w, e in reversed(chosen):
                entries.insert(i + 1, e)
            ns, params = build(entries)
        self._blend[s.index] = ns
        return ns

    def curve_support(self, dblend: Node) -> "_CurveSupport":
        spine = self.deref(dblend, "spine")
        if spine is None:
            raise ConvertError("unsupported_surface", "degenerate blend id %s without spine" % dblend.f.get("node_id"))
        if spine.type == 38:
            pts, tans = self.intersection_curve(spine)
            nc = geom.interpolate_cubic(pts, tans)
        else:
            cev = self._curve_param_evaluator(spine)
            if cev is None:
                raise ConvertError("unsupported_surface", "cliff edge curve of type %s cannot be evaluated" % spine.name)
            fn, t0, t1 = cev
            if spine.type == 30:
                t0, t1 = -1.0, 1.0
            n = 256
            pts = [fn(t0 + (t1 - t0) * i / n)[0] for i in range(n + 1)]
            tans = [unit(fn(t0 + (t1 - t0) * i / n)[1]) for i in range(n + 1)]
            nc = geom.interpolate_cubic(pts, tans)
        return _CurveSupport(nc)

    def spine_samples(self, spine: Node, blend: Node, offsets: List[Tuple[geom.Surface, bool]]) -> List[Vec]:
        t = spine.type
        if t == 38:
            pts = self.intersection_points(spine)
            tans = _tangents_from_surfaces(pts, offsets)
            if tans is not None:
                pts, tans = _refine_on_surfaces(pts, tans, offsets, tol=5e-8, min_segments=32)
            return pts, None
        cev = self._curve_param_evaluator(spine)
        if cev is None:
            raise ConvertError("unsupported_surface", "blend spine of type %s cannot be sampled" % spine.name)
        fn, t0, t1 = cev
        periodic = t in (31, 32)
        # LIMIT nodes bound the spine when the blend does not use the whole curve
        lims = []
        for key in ("start", "end"):
            ln = self.deref(blend, key)
            if ln is not None and ln.f.get("hvec"):
                lims.append(tuple(ln.f["hvec"][0]))
        if len(lims) == 2:
            ta = _curve_param_of_point(fn, t0, t1, lims[0], periodic)
            tb = _curve_param_of_point(fn, t0, t1, lims[1], periodic)
            if periodic and tb <= ta:
                tb += 2 * math.pi
            t0, t1 = ta, tb
        elif t == 30:
            t0, t1 = -1.0, 1.0
        n = 64
        return ([fn(t0 + (t1 - t0) * i / n)[0] for i in range(n + 1)],
                lambda s_, fn=fn, t0=t0, t1=t1: fn(t0 + (t1 - t0) * s_)[0])

    # -- file assembly --------------------------------------------------------------------
    # -- file assembly --------------------------------------------------------------------
    def header_entities(self) -> Dict[str, int]:
        w = self.w
        app = w.add("APPLICATION_CONTEXT('automotive_design')")
        w.add("APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2003,#%d)" % app)
        pc = w.add("PRODUCT_CONTEXT('',#%d,'mechanical')" % app)
        pdc = w.add("PRODUCT_DEFINITION_CONTEXT('detailed design',#%d,'design')" % app)
        mm = w.add("(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.))")
        rad = w.add("(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.))")
        sr = w.add("(NAMED_UNIT(*)SI_UNIT($,.STERADIAN.)SOLID_ANGLE_UNIT())")
        unc = w.add("UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-05),#%d,'distance_accuracy_value','Maximum model space distance between geometric entities at asserted connectivities')" % mm)
        geo = w.add("(GEOMETRIC_REPRESENTATION_CONTEXT(3)GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#%d))GLOBAL_UNIT_ASSIGNED_CONTEXT((#%d,#%d,#%d))REPRESENTATION_CONTEXT('Context3D','3D'))" % (unc, mm, rad, sr))
        return {"app": app, "pc": pc, "pdc": pdc, "geo": geo}


def _hermite_mid(pa: Vec, ta: Vec, pb: Vec, tb: Vec) -> Vec:
    """Midpoint of the cubic Hermite segment with chord-length scaled unit tangents."""
    c = dist(pa, pb)
    ua = unit(ta) if norm(ta) > 1e-300 else ta
    ub = unit(tb) if norm(tb) > 1e-300 else tb
    return add(mul(add(pa, pb), 0.5), mul(sub(ua, ub), c / 8.0))


def _tangents_from_surfaces(pts: List[Vec], evs: List[Tuple[geom.Surface, bool]]) -> Optional[List[Vec]]:
    """Unit tangents of the intersection of two surfaces at the given points."""
    out: List[Vec] = []
    n = len(pts)
    for i, p in enumerate(pts):
        try:
            ns = []
            for ev, pos in evs:
                u, v = ev.project(p)
                nn = ev.normal(u, v)
                ns.append(nn if pos else mul(nn, -1.0))
            t = cross(ns[0], ns[1])
        except (geom.GeomError, ValueError, ZeroDivisionError, OverflowError):
            return None
        ref = sub(pts[min(i + 1, n - 1)], pts[max(i - 1, 0)]) if n > 1 else (0.0, 0.0, 0.0)
        if norm(t) < 1e-6:
            # parallel normals (terminator / tangency): fall back to the chart direction
            if norm(ref) < 1e-300:
                return None
            out.append(unit(ref))
            continue
        t = unit(t)
        if norm(ref) > 1e-300 and dot(t, ref) < 0:
            t = mul(t, -1.0)
        out.append(t)
    return out


def _inside_domain(ev: geom.Surface) -> bool:
    """After a projection: was the foot inside the surface's own parameter domain?"""
    if isinstance(ev, geom.NurbsSurfaceAdapter) and ev._last is not None:
        s_ = ev.s
        u, v = ev._last
        du = (s_.u1 - s_.u0) * 0.01
        dv = (s_.v1 - s_.v0) * 0.01
        return s_.u0 - du <= u <= s_.u1 + du and s_.v0 - dv <= v <= s_.v1 + dv
    if isinstance(ev, _OffsetEval):
        return _inside_domain(ev.base)
    return True


def _project_both(p: Vec, evs: List[Tuple[geom.Surface, bool]]) -> Optional[Vec]:
    """Alternating projection onto two surfaces; None if it does not converge
    or the foot leaves a NURBS surface's parameter domain."""
    q = p
    moved = 1.0
    for _ in range(80):
        moved = 0.0
        for ev, _pos in evs:
            u, v = ev.project(q)
            r = ev.eval(u, v)
            moved = max(moved, dist(q, r))
            q = r
        if moved < 1e-12:
            break
    if moved > 1e-10:
        return None
    for ev, _pos in evs:
        if not _inside_domain(ev):
            return None
    return q


def _refine_on_surfaces(pts: List[Vec], tans: List[Vec], evs: List[Tuple[geom.Surface, bool]],
                        tol: float = 1e-7, min_segments: int = 0) -> Tuple[List[Vec], List[Vec]]:
    """Insert exact intersection points until the Hermite spline is within tol."""
    segs = [(pts[i], tans[i], pts[i + 1], tans[i + 1]) for i in range(len(pts) - 1)]
    out_pts: List[Vec] = [pts[0]]
    out_tans: List[Vec] = [tans[0]]
    total = sum(dist(a, b) for a, _, b, _ in segs)
    max_len = total / min_segments if min_segments > 0 and total > 0 else float("inf")

    def tangent_at(q: Vec, ref: Vec) -> Optional[Vec]:
        t = _tangents_from_surfaces([q], evs)
        if t is None:
            return None
        tq = t[0]
        return tq if dot(tq, ref) >= 0 else mul(tq, -1.0)

    budget = [600]

    def rec(pa: Vec, ta: Vec, pb: Vec, tb: Vec, depth: int) -> None:
        chord = dist(pa, pb)
        need = chord > max_len
        if depth < 8 and budget[0] > 0 and chord > 1e-6:
            m = _hermite_mid(pa, ta, pb, tb)
            q = _project_both(m, evs)
            # a genuine correction is small compared with the chord; large jumps mean the
            # projection slid along a tangential or extrapolated region
            if q is not None and dist(q, m) < 0.05 * chord + 1e-9:
                dev = dist(q, m)
                if dev > max(tol, 1e-6 * chord) or need:
                    tq = tangent_at(q, sub(pb, pa))
                    if tq is not None:
                        budget[0] -= 1
                        rec(pa, ta, q, tq, depth + 1)
                        rec(q, tq, pb, tb, depth + 1)
                        return
        out_pts.append(pb)
        out_tans.append(tb)

    for pa, ta, pb, tb in segs:
        rec(pa, ta, pb, tb, 0)
    return out_pts, out_tans


def _section_angle(cpt: Vec, f0: Vec, f1: Vec) -> float:
    r = 0.5 * (dist(f0, cpt) + dist(f1, cpt))
    if r < 1e-300 or dist(f0, cpt) < 1e-300 or dist(f1, cpt) < 1e-300:
        return 0.0
    xv = unit(sub(f0, cpt))
    d1 = sub(f1, cpt)
    yv = sub(d1, mul(xv, dot(d1, xv)))
    if norm(yv) < 1e-7 * r:
        return 0.0
    yv = unit(yv)
    return math.atan2(dot(d1, yv), dot(d1, xv))


def _curve_point_distance(nc: geom.NurbsCurve, p: Vec) -> float:
    """Distance from p to a NURBS curve (dense sampling + golden-section refinement)."""
    n = 256
    t0, t1 = nc.t0, nc.t1

    def f(t: float) -> float:
        q = nc.eval(t)
        return dist((q[0], q[1], q[2]), p)

    best = min(range(n + 1), key=lambda i: f(t0 + (t1 - t0) * i / n))
    t = t0 + (t1 - t0) * best / n
    lo = max(t0, t - (t1 - t0) / n)
    hi = min(t1, t + (t1 - t0) / n)
    return f(geom.golden(f, lo, hi))


class _CurveSupport(geom.Surface):
    """A curve standing in for a blend support (cliff-edge blends).

    project(p) returns (t, 0) of the nearest curve point; eval gives that point;
    normal is the direction from the foot towards the last projected point, so
    _OffsetEval(self, r) evaluates the tube of radius r around the curve.
    """

    def __init__(self, nc: geom.NurbsCurve) -> None:
        self.nc = nc
        self._last_p: Optional[Vec] = None
        self._samples = None

    def _grid(self):
        if self._samples is None:
            n = 512
            self._samples = [(self.nc.t0 + (self.nc.t1 - self.nc.t0) * i / n, self.nc.eval(self.nc.t0 + (self.nc.t1 - self.nc.t0) * i / n)) for i in range(n + 1)]
        return self._samples

    def eval(self, u: float, v: float) -> Vec:
        q = self.nc.eval(min(max(u, self.nc.t0), self.nc.t1))
        return (q[0], q[1], q[2])

    def normal(self, u: float, v: float) -> Vec:
        foot = self.eval(u, v)
        if self._last_p is None:
            return geom.perp(unit(self.nc.eval_deriv(u)[1]))
        d = sub(self._last_p, foot)
        if norm(d) < 1e-300:
            return geom.perp(unit(self.nc.eval_deriv(u)[1]))
        return unit(d)

    def project(self, p: Vec) -> Tuple[float, float]:
        self._last_p = p
        grid = self._grid()
        best = min(range(len(grid)), key=lambda i: dist(grid[i][1], p))
        t = grid[best][0]
        h = (self.nc.t1 - self.nc.t0) / (len(grid) - 1)

        def f(tt: float) -> float:
            q = self.nc.eval(tt)
            return dist((q[0], q[1], q[2]), p)

        t = geom.golden(f, max(self.nc.t0, t - h), min(self.nc.t1, t + h))
        return t, 0.0


def _degenerate_arc(pt: Vec, nseg: int) -> geom.NurbsCurve:
    """A 'row' collapsed to one point, with the knot/weight structure of arc_nurbs(nseg)."""
    w = math.cos(math.pi / 4)
    ctrl: List[Tuple[float, ...]] = [pt]
    weights: List[float] = [1.0]
    for _ in range(nseg):
        ctrl.append(pt)
        weights.append(w)
        ctrl.append(pt)
        weights.append(1.0)
    knots = [0.0, 0.0, 0.0]
    for s_ in range(1, nseg):
        knots.extend([float(s_) / nseg] * 2)
    knots.extend([1.0, 1.0, 1.0])
    return geom.NurbsCurve(2, ctrl, knots, weights)


def _curve_param_of_point(fn, t0: float, t1: float, p: Vec, periodic: bool) -> float:
    """Parameter of the closest curve point to p (sampling + golden refinement)."""
    n = 720
    best = min(range(n + 1), key=lambda i: dist(fn(t0 + (t1 - t0) * i / n)[0], p))
    t = t0 + (t1 - t0) * best / n
    lo, hi = t - (t1 - t0) / n, t + (t1 - t0) / n
    return geom.golden(lambda tt: dist(fn(tt)[0], p), lo, hi)


def _polyline_len(pts: Sequence[Sequence[float]]) -> float:
    return sum(dist(tuple(pts[i][:3]) + (0.0,) * (3 - len(pts[i][:3])), tuple(pts[i + 1][:3]) + (0.0,) * (3 - len(pts[i + 1][:3])))
               for i in range(len(pts) - 1))


def _normalise_curve_knots(nc: geom.NurbsCurve) -> geom.NurbsCurve:
    span = nc.t1 - nc.t0
    length_mm = _polyline_len(nc.ctrl) * SCALE
    if span <= 0 or length_mm <= 0:
        return nc
    f = length_mm / span
    if 0.5 <= f <= 2.0:
        return nc
    knots = [(k - nc.t0) * f for k in nc.knots]
    return geom.NurbsCurve(nc.degree, nc.ctrl, knots, nc.weights)


def _normalise_surface_knots(ns: geom.NurbsSurface) -> geom.NurbsSurface:
    du = ns.u1 - ns.u0
    dv = ns.v1 - ns.v0
    lu = sum(_polyline_len([ns.ctrl[i][j] for i in range(ns.n_u)]) for j in range(ns.n_v)) / max(1, ns.n_v) * SCALE
    lv = sum(_polyline_len(ns.ctrl[i]) for i in range(ns.n_u)) / max(1, ns.n_u) * SCALE
    fu = lu / du if du > 0 and lu > 0 else 1.0
    fv = lv / dv if dv > 0 and lv > 0 else 1.0
    if 0.5 <= fu <= 2.0:
        fu = 1.0
    if 0.5 <= fv <= 2.0:
        fv = 1.0
    if fu == 1.0 and fv == 1.0:
        return ns
    uk = [(k - ns.u0) * fu for k in ns.u_knots] if fu != 1.0 else list(ns.u_knots)
    vk = [(k - ns.v0) * fv for k in ns.v_knots] if fv != 1.0 else list(ns.v_knots)
    return geom.NurbsSurface(ns.p, ns.q, ns.ctrl, uk, vk, ns.weights)


def _reverse_curve(nc: geom.NurbsCurve) -> geom.NurbsCurve:
    t0, t1 = nc.knots[0], nc.knots[-1]
    knots = [t0 + t1 - k for k in reversed(nc.knots)]
    ctrl = list(reversed(nc.ctrl))
    weights = list(reversed(nc.weights)) if nc.weights else None
    return geom.NurbsCurve(nc.degree, ctrl, knots, weights)


def _careful_project(ev: geom.Surface, p: Vec) -> Vec:
    """Projection with a denser search for NURBS adapters (blend support feet)."""
    if isinstance(ev, geom.NurbsSurfaceAdapter):
        s = ev.s
        u0, u1, v0, v1 = ev.ext
        g = 40
        best = None
        for i in range(g + 1):
            u = u0 + (u1 - u0) * i / g
            for j in range(g + 1):
                v = v0 + (v1 - v0) * j / g
                d = dist(s.eval(u, v), p)
                if best is None or d < best[0]:
                    best = (d, u, v)
        u, v, d = geom.newton_project(s, p, best[1], best[2], ev.ext)
        ev._last = (u, v)
        return s.eval(u, v)
    u, v = ev.project(p)
    return ev.eval(u, v)


class _OffsetEval(geom.Surface):
    def __init__(self, base: geom.Surface, off: float) -> None:
        self.base = base
        self.off = off

    def eval(self, u: float, v: float) -> Vec:
        return add(self.base.eval(u, v), mul(self.base.normal(u, v), self.off))

    def normal(self, u: float, v: float) -> Vec:
        return self.base.normal(u, v)

    def project(self, p: Vec) -> Tuple[float, float]:
        return self.base.project(p)


class _ev_nurbs:
    """Callable s in [0,1] -> point on a NURBS curve (with an `approx` flag)."""

    def __init__(self, nc: geom.NurbsCurve) -> None:
        self.nc = nc
        self.approx = False

    def __call__(self, s: float) -> Vec:
        p = self.nc.eval(self.nc.t0 + (self.nc.t1 - self.nc.t0) * s)
        return (p[0], p[1], p[2]) if len(p) == 3 else (p[0], p[1], 0.0)


def _restrict_curve(nc: geom.NurbsCurve, prange: Tuple[float, float]) -> geom.NurbsCurve:
    """Restrict a NURBS curve to [a, b] by knot insertion (exact)."""
    a, b = prange
    a = max(a, nc.t0)
    b = min(b, nc.t1)
    if b <= a or (abs(a - nc.t0) < 1e-12 and abs(b - nc.t1) < 1e-12):
        return nc
    p = nc.degree
    h = geom._homog(nc.ctrl, nc.weights)
    knots = list(nc.knots)
    for t in (a, b):
        m = sum(1 for k in knots if abs(k - t) < 1e-14)
        if m < p + 1:
            h, knots = geom._insert_knot(p, h, knots, t, p + 1 - m)
    # locate
    ia = next(i for i, k in enumerate(knots) if abs(k - a) < 1e-14)
    ib = len(knots) - 1 - next(i for i, k in enumerate(reversed(knots)) if abs(k - b) < 1e-14)
    knots2 = knots[ia:ib + 1]
    h2 = h[ia:ib - p]
    pts, ws = geom._dehomog(h2, bool(nc.weights))
    return geom.NurbsCurve(p, pts, knots2, ws)


def write_step(xt_bodies: List[Tuple[XtFile, Node, str]], file_name: str, warnings: List[str]) -> Tuple[str, int, int]:
    """Write one STEP file holding all solids of all given (xt, body, name) triples.

    Returns (text, solid_count, face_count).
    """
    w = StepWriter()
    first = XtStepMapper(xt_bodies[0][0], warnings) if xt_bodies else None
    if first is None:
        raise ConvertError("empty_step", "no bodies to write")
    first.w = w
    ctx = first.header_entities()
    product_name = file_name.rsplit(".", 1)[0].replace("'", "''") if file_name else "part"
    prod = w.add("PRODUCT('%s','%s','',(#%d))" % (product_name, product_name, ctx["pc"]))
    pdf = w.add("PRODUCT_DEFINITION_FORMATION('','',#%d)" % prod)
    pd = w.add("PRODUCT_DEFINITION('design','',#%d,#%d)" % (pdf, ctx["pdc"]))
    pds = w.add("PRODUCT_DEFINITION_SHAPE('','',#%d)" % pd)
    solid_ids: List[int] = []
    faces = 0
    for xt, body, name in xt_bodies:
        m = XtStepMapper(xt, warnings, name)
        m.w = w
        m._warned = first._warned
        solid_ids.extend(m.convert_body(body, name))
        faces += m.face_count
    first.finish_warnings()
    origin = w.add("AXIS2_PLACEMENT_3D('',#%d,#%d,#%d)" % (w.point((0.0, 0.0, 0.0)), w.direction((0.0, 0.0, 1.0)), w.direction((1.0, 0.0, 0.0))))
    rep = w.add("ADVANCED_BREP_SHAPE_REPRESENTATION('',(#%d,%s),#%d)" % (origin, ",".join("#%d" % s for s in solid_ids), ctx["geo"]))
    w.add("SHAPE_DEFINITION_REPRESENTATION(#%d,#%d)" % (pds, rep))
    head = [
        "ISO-10303-21;",
        "HEADER;",
        "FILE_DESCRIPTION(('sldprt2step conversion of Parasolid XT B-rep'),'2;1');",
        # no time stamp: the output is a pure function of the input (deterministic)
        "FILE_NAME('%s','',(''),(''),'sldprt2step','','');" % file_name.replace("'", "''"),
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));",
        "ENDSEC;",
        "DATA;",
    ]
    text = "\n".join(head + w.lines + ["ENDSEC;", "END-ISO-10303-21;", ""])
    return text, len(solid_ids), faces
