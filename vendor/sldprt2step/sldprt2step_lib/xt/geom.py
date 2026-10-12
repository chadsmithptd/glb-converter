"""Small geometry kernel for the XT -> STEP mapping (pure Python, 3.9+).

Provides vectors, NURBS curve/surface evaluation with derivatives, knot
insertion (to clamp unclamped XT B-splines), the analytic XT surfaces with
closest-point projection, exact rational arcs, and a cubic interpolating spline
used to approximate intersection / SP curves and rolling-ball blends.
All lengths are in the XT unit (metres); the STEP writer scales to mm.
"""
from __future__ import annotations

import math
from typing import Callable, List, Optional, Sequence, Tuple

Vec = Tuple[float, float, float]


class GeomError(Exception):
    pass


# --- vectors -----------------------------------------------------------------

def add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def mul(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a: Vec) -> float:
    return math.sqrt(dot(a, a))


def unit(a: Vec) -> Vec:
    n = norm(a)
    if n < 1e-300:
        raise GeomError("cannot normalise a zero vector")
    return (a[0] / n, a[1] / n, a[2] / n)


def dist(a: Vec, b: Vec) -> float:
    return norm(sub(a, b))


def perp(a: Vec) -> Vec:
    """Some unit vector perpendicular to a (a need not be unit)."""
    ax, ay, az = abs(a[0]), abs(a[1]), abs(a[2])
    if ax <= ay and ax <= az:
        o: Vec = (1.0, 0.0, 0.0)
    elif ay <= az:
        o = (0.0, 1.0, 0.0)
    else:
        o = (0.0, 0.0, 1.0)
    return unit(cross(a, o))


def lerp(a: Vec, b: Vec, t: float) -> Vec:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


# --- B-spline basics -----------------------------------------------------------

def expand_knots(distinct: Sequence[float], mults: Sequence[int]) -> List[float]:
    out: List[float] = []
    for k, m in zip(distinct, mults):
        out.extend([float(k)] * int(m))
    return out


def find_span(degree: int, t: float, knots: List[float], n_ctrl: int) -> int:
    """Index i with knots[i] <= t < knots[i+1] within the valid domain."""
    lo = degree
    hi = n_ctrl  # valid domain is [knots[degree], knots[n_ctrl]]
    if t >= knots[hi]:
        i = hi - 1
        while i > lo and knots[i] == knots[hi]:
            i -= 1
        return i
    if t <= knots[lo]:
        return lo
    a, b = lo, hi
    while b - a > 1:
        m = (a + b) // 2
        if t < knots[m]:
            b = m
        else:
            a = m
    return a


def basis_funs(span: int, t: float, degree: int, knots: List[float]) -> List[float]:
    N = [1.0] + [0.0] * degree
    left = [0.0] * (degree + 1)
    right = [0.0] * (degree + 1)
    for j in range(1, degree + 1):
        left[j] = t - knots[span + 1 - j]
        right[j] = knots[span + j] - t
        saved = 0.0
        for r in range(j):
            den = right[r + 1] + left[j - r]
            temp = N[r] / den if den != 0.0 else 0.0
            N[r] = saved + right[r + 1] * temp
            saved = left[j - r] * temp
        N[j] = saved
    return N


def basis_funs_derivs(span: int, t: float, degree: int, knots: List[float]) -> Tuple[List[float], List[float]]:
    """Basis functions and first derivatives (Piegl & Tiller A2.3, n=1)."""
    p = degree
    ndu = [[0.0] * (p + 1) for _ in range(p + 1)]
    ndu[0][0] = 1.0
    left = [0.0] * (p + 1)
    right = [0.0] * (p + 1)
    for j in range(1, p + 1):
        left[j] = t - knots[span + 1 - j]
        right[j] = knots[span + j] - t
        saved = 0.0
        for r in range(j):
            ndu[j][r] = right[r + 1] + left[j - r]
            temp = ndu[r][j - 1] / ndu[j][r] if ndu[j][r] != 0.0 else 0.0
            ndu[r][j] = saved + right[r + 1] * temp
            saved = left[j - r] * temp
        ndu[j][j] = saved
    N = [ndu[j][p] for j in range(p + 1)]
    D = [0.0] * (p + 1)
    for r in range(p + 1):
        d = 0.0
        rk = r - 1
        pk = p - 1
        if r >= 1:
            a0 = 1.0 / ndu[pk + 1][rk] if ndu[pk + 1][rk] != 0.0 else 0.0
            d += a0 * ndu[rk][pk]
        if r <= pk:
            a1 = -1.0 / ndu[pk + 1][r] if ndu[pk + 1][r] != 0.0 else 0.0
            d += a1 * ndu[r][pk]
        D[r] = d * p
    return N, D


class NurbsCurve:
    """NURBS curve: ctrl points in R^dim (dim 2 or 3), optional weights."""

    def __init__(self, degree: int, ctrl: List[Tuple[float, ...]], knots: List[float],
                 weights: Optional[List[float]] = None) -> None:
        self.degree = degree
        self.ctrl = ctrl
        self.knots = knots
        self.weights = weights
        self.dim = len(ctrl[0]) if ctrl else 3
        if len(knots) != len(ctrl) + degree + 1:
            raise GeomError("B-curve: %d knots for %d poles of degree %d" % (len(knots), len(ctrl), degree))

    @property
    def t0(self) -> float:
        return self.knots[self.degree]

    @property
    def t1(self) -> float:
        return self.knots[len(self.ctrl)]

    def eval(self, t: float) -> Tuple[float, ...]:
        p = self.degree
        span = find_span(p, t, self.knots, len(self.ctrl))
        N = basis_funs(span, t, p, self.knots)
        acc = [0.0] * self.dim
        wsum = 0.0
        for k in range(p + 1):
            idx = span - p + k
            w = self.weights[idx] if self.weights else 1.0
            c = self.ctrl[idx]
            f = N[k] * w
            for d in range(self.dim):
                acc[d] += f * c[d]
            wsum += f
        if self.weights:
            return tuple(a / wsum for a in acc)
        return tuple(acc)

    def eval_deriv(self, t: float) -> Tuple[Tuple[float, ...], Tuple[float, ...]]:
        p = self.degree
        span = find_span(p, t, self.knots, len(self.ctrl))
        N, D = basis_funs_derivs(span, t, p, self.knots)
        A = [0.0] * self.dim
        Ad = [0.0] * self.dim
        w = 0.0
        wd = 0.0
        for k in range(p + 1):
            idx = span - p + k
            wk = self.weights[idx] if self.weights else 1.0
            c = self.ctrl[idx]
            for d in range(self.dim):
                A[d] += N[k] * wk * c[d]
                Ad[d] += D[k] * wk * c[d]
            w += N[k] * wk
            wd += D[k] * wk
        if self.weights:
            P = [a / w for a in A]
            Pd = [(Ad[d] - wd * P[d]) / w for d in range(self.dim)]
            return tuple(P), tuple(Pd)
        return tuple(A), tuple(Ad)

    def clamped(self) -> "NurbsCurve":
        """Return an equivalent curve whose knot vector is clamped to [t0, t1]."""
        return _clamp_curve(self)

    def sample(self, n: int, a: Optional[float] = None, b: Optional[float] = None) -> List[Tuple[float, ...]]:
        a = self.t0 if a is None else a
        b = self.t1 if b is None else b
        return [self.eval(a + (b - a) * i / (n - 1)) for i in range(n)]


def _insert_knot(degree: int, ctrl: List[Tuple[float, ...]], knots: List[float], t: float, times: int
                 ) -> Tuple[List[Tuple[float, ...]], List[float]]:
    """Boehm knot insertion on homogeneous control points (weights folded in)."""
    p = degree
    for _ in range(times):
        n = len(ctrl)
        k = find_span(p, t, knots, n)
        # current multiplicity of t
        s = sum(1 for kk in knots if kk == t)
        if s >= p + 1:
            break
        new_ctrl: List[Tuple[float, ...]] = []
        for i in range(n + 1):
            if i <= k - p:
                new_ctrl.append(ctrl[i])
            elif i <= k:
                den = knots[i + p] - knots[i]
                a = (t - knots[i]) / den if den != 0.0 else 0.0
                q = tuple(a * ctrl[i][d] + (1.0 - a) * ctrl[i - 1][d] for d in range(len(ctrl[0])))
                new_ctrl.append(q)
            else:
                new_ctrl.append(ctrl[i - 1])
        knots = knots[:k + 1] + [t] + knots[k + 1:]
        ctrl = new_ctrl
    return ctrl, knots


def _homog(ctrl: List[Tuple[float, ...]], weights: Optional[List[float]]) -> List[Tuple[float, ...]]:
    if not weights:
        return [tuple(c) + (1.0,) for c in ctrl]
    return [tuple(x * w for x in c) + (w,) for c, w in zip(ctrl, weights)]


def _dehomog(h: List[Tuple[float, ...]], rational: bool) -> Tuple[List[Tuple[float, ...]], Optional[List[float]]]:
    pts = []
    ws = []
    for c in h:
        w = c[-1]
        pts.append(tuple(x / w for x in c[:-1]))
        ws.append(w)
    return pts, (ws if rational else None)


def _clamp_curve(c: NurbsCurve) -> NurbsCurve:
    p = c.degree
    knots = list(c.knots)
    h = _homog(c.ctrl, c.weights)
    t0, t1 = c.t0, c.t1
    m0 = sum(1 for k in knots if k == t0)
    m1 = sum(1 for k in knots if k == t1)
    if m0 < p + 1:
        h, knots = _insert_knot(p, h, knots, t0, p + 1 - m0)
    if m1 < p + 1:
        h, knots = _insert_knot(p, h, knots, t1, p + 1 - m1)
    # drop knots/poles outside [t0, t1]
    first = knots.index(t0)
    last = len(knots) - 1 - knots[::-1].index(t1)
    knots2 = knots[first:last + 1]
    # poles: indices first .. last - p - 1
    h2 = h[first:last - p]
    if len(knots2) != len(h2) + p + 1:
        raise GeomError("clamping produced inconsistent knot vector")
    pts, ws = _dehomog(h2, bool(c.weights))
    return NurbsCurve(p, pts, knots2, ws)


class NurbsSurface:
    """Tensor-product NURBS surface; ctrl[i][j], i along u (n_u), j along v (n_v)."""

    def __init__(self, u_degree: int, v_degree: int, ctrl: List[List[Vec]], u_knots: List[float],
                 v_knots: List[float], weights: Optional[List[List[float]]] = None) -> None:
        self.p = u_degree
        self.q = v_degree
        self.ctrl = ctrl
        self.u_knots = u_knots
        self.v_knots = v_knots
        self.weights = weights
        self.n_u = len(ctrl)
        self.n_v = len(ctrl[0]) if ctrl else 0
        if len(u_knots) != self.n_u + self.p + 1 or len(v_knots) != self.n_v + self.q + 1:
            raise GeomError("B-surface: inconsistent knot counts")

    @property
    def u0(self) -> float:
        return self.u_knots[self.p]

    @property
    def u1(self) -> float:
        return self.u_knots[self.n_u]

    @property
    def v0(self) -> float:
        return self.v_knots[self.q]

    @property
    def v1(self) -> float:
        return self.v_knots[self.n_v]

    def eval(self, u: float, v: float) -> Vec:
        return self.eval_derivs(u, v)[0]

    def eval_derivs(self, u: float, v: float) -> Tuple[Vec, Vec, Vec]:
        su = find_span(self.p, u, self.u_knots, self.n_u)
        sv = find_span(self.q, v, self.v_knots, self.n_v)
        Nu, Du = basis_funs_derivs(su, u, self.p, self.u_knots)
        Nv, Dv = basis_funs_derivs(sv, v, self.q, self.v_knots)
        A = [0.0, 0.0, 0.0]
        Au = [0.0, 0.0, 0.0]
        Av = [0.0, 0.0, 0.0]
        w = wu = wv = 0.0
        for a in range(self.p + 1):
            i = su - self.p + a
            row = self.ctrl[i]
            wrow = self.weights[i] if self.weights else None
            for b in range(self.q + 1):
                j = sv - self.q + b
                c = row[j]
                wk = wrow[j] if wrow else 1.0
                f = Nu[a] * Nv[b] * wk
                fu = Du[a] * Nv[b] * wk
                fv = Nu[a] * Dv[b] * wk
                A[0] += f * c[0]; A[1] += f * c[1]; A[2] += f * c[2]
                Au[0] += fu * c[0]; Au[1] += fu * c[1]; Au[2] += fu * c[2]
                Av[0] += fv * c[0]; Av[1] += fv * c[1]; Av[2] += fv * c[2]
                w += f; wu += fu; wv += fv
        if self.weights:
            P = (A[0] / w, A[1] / w, A[2] / w)
            Pu = ((Au[0] - wu * P[0]) / w, (Au[1] - wu * P[1]) / w, (Au[2] - wu * P[2]) / w)
            Pv = ((Av[0] - wv * P[0]) / w, (Av[1] - wv * P[1]) / w, (Av[2] - wv * P[2]) / w)
            return P, Pu, Pv
        return (A[0], A[1], A[2]), (Au[0], Au[1], Au[2]), (Av[0], Av[1], Av[2])

    def normal(self, u: float, v: float) -> Vec:
        _, Su, Sv = self.eval_derivs(u, v)
        n = cross(Su, Sv)
        if norm(n) < 1e-300:
            # degenerate point: nudge inside the domain
            du = (self.u1 - self.u0) * 1e-4
            dv = (self.v1 - self.v0) * 1e-4
            uu = min(max(u + du, self.u0), self.u1)
            vv = min(max(v + dv, self.v0), self.v1)
            _, Su, Sv = self.eval_derivs(uu, vv)
            n = cross(Su, Sv)
        return unit(n)

    def clamped(self) -> "NurbsSurface":
        """Equivalent surface with clamped knot vectors in both directions."""
        rational = bool(self.weights)
        # clamp along u: treat each v-column as a curve of 3D(+w) points
        # homogeneous coordinates: (w*x, w*y, w*z, w)
        rows_h = [[tuple(x * (self.weights[i][j] if rational else 1.0) for x in self.ctrl[i][j])
                   + ((self.weights[i][j],) if rational else (1.0,))
                   for j in range(self.n_v)] for i in range(self.n_u)]
        # u direction
        new_rows: List[List[Tuple[float, ...]]] = []
        uk_out: List[float] = []
        for j in range(self.n_v):
            col = [rows_h[i][j] for i in range(self.n_u)]
            h2, k2, first, last = _clamp_h(self.p, col, list(self.u_knots))
            if j == 0:
                uk_out = k2
            new_rows.append(h2)
        # new_rows[j][i] -> transpose to [i][j]
        n_u2 = len(new_rows[0])
        grid = [[new_rows[j][i] for j in range(self.n_v)] for i in range(n_u2)]
        # v direction
        vk_out: List[float] = []
        grid2: List[List[Tuple[float, ...]]] = []
        for i in range(n_u2):
            h2, k2, first, last = _clamp_h(self.q, grid[i], list(self.v_knots))
            if i == 0:
                vk_out = k2
            grid2.append(h2)
        ctrl = [[(c[0] / c[3], c[1] / c[3], c[2] / c[3]) for c in row] for row in grid2]
        weights = [[c[3] for c in row] for row in grid2] if rational else None
        return NurbsSurface(self.p, self.q, ctrl, uk_out, vk_out, weights)


def _clamp_h(p: int, h: List[Tuple[float, ...]], knots: List[float]):
    t0 = knots[p]
    t1 = knots[len(h)]
    m0 = sum(1 for k in knots if k == t0)
    m1 = sum(1 for k in knots if k == t1)
    if m0 < p + 1:
        h, knots = _insert_knot(p, h, knots, t0, p + 1 - m0)
    if m1 < p + 1:
        h, knots = _insert_knot(p, h, knots, t1, p + 1 - m1)
    first = knots.index(t0)
    last = len(knots) - 1 - knots[::-1].index(t1)
    knots2 = knots[first:last + 1]
    h2 = h[first:last - p]
    if len(knots2) != len(h2) + p + 1:
        raise GeomError("clamping produced inconsistent knot vector")
    return h2, knots2, first, last


# --- exact arcs as rational quadratic NURBS -------------------------------------

def arc_nurbs(centre: Vec, x_axis: Vec, y_axis: Vec, radius: float, a0: float, a1: float,
              nseg: int = 0) -> NurbsCurve:
    """Exact circular arc from angle a0 to a1 (radians, a1 > a0, span <= 2*pi).

    `nseg` forces the number of quadratic segments (each must span < pi);
    0 picks the minimum with segments <= 90 degrees.
    """
    span = a1 - a0
    if span <= 0:
        raise GeomError("arc with non-positive span")
    if nseg <= 0:
        nseg = max(1, int(math.ceil(span / (math.pi / 2) - 1e-9)))
    if span / nseg >= math.pi - 1e-9:
        raise GeomError("arc segment spans >= pi")
    seg = span / nseg
    w = math.cos(seg / 2.0)
    ctrl: List[Tuple[float, ...]] = []
    weights: List[float] = []

    def pt(a: float, r: float) -> Vec:
        return add(centre, add(mul(x_axis, r * math.cos(a)), mul(y_axis, r * math.sin(a))))

    for s in range(nseg):
        b0 = a0 + s * seg
        b1 = b0 + seg
        bm = 0.5 * (b0 + b1)
        if s == 0:
            ctrl.append(pt(b0, radius))
            weights.append(1.0)
        ctrl.append(pt(bm, radius / w))
        weights.append(w)
        ctrl.append(pt(b1, radius))
        weights.append(1.0)
    knots = [0.0, 0.0, 0.0]
    for s in range(1, nseg):
        knots.extend([float(s) / nseg] * 2)
    knots.extend([1.0, 1.0, 1.0])
    return NurbsCurve(2, ctrl, knots, weights)


# --- cubic interpolation through points ----------------------------------------

def interpolate_cubic(points: List[Vec], tangents: Optional[List[Vec]] = None,
                      closed: bool = False) -> NurbsCurve:
    """C1 piecewise-cubic Bezier spline through `points` as a degree-3 B-spline.

    Tangent directions default to Catmull-Rom (chord-length) estimates. The
    parameterisation is cumulative chord length; interior knots have
    multiplicity 3 (Bezier segments), ends are clamped.
    """
    pts = list(points)
    # drop consecutive duplicates
    clean: List[Vec] = []
    for p in pts:
        if not clean or dist(clean[-1], p) > 1e-12:
            clean.append(p)
    pts = clean
    n = len(pts)
    if n < 2:
        raise GeomError("interpolation needs at least two distinct points")
    chords = [dist(pts[i], pts[i + 1]) for i in range(n - 1)]
    if tangents is None:
        tangents = []
        for i in range(n):
            if closed and n > 2:
                prv = pts[i - 1] if i > 0 else pts[n - 2]
                nxt = pts[i + 1] if i < n - 1 else pts[1]
                tangents.append(unit(sub(nxt, prv)))
            elif i == 0:
                if n > 2:
                    # one-sided second-order estimate
                    d = sub(mul(pts[1], 4.0), add(pts[2], mul(pts[0], 3.0)))
                else:
                    d = sub(pts[1], pts[0])
                tangents.append(unit(d) if norm(d) > 1e-15 else unit(sub(pts[1], pts[0])))
            elif i == n - 1:
                if n > 2:
                    d = sub(add(mul(pts[n - 1], 3.0), pts[n - 3]), mul(pts[n - 2], 4.0))
                else:
                    d = sub(pts[n - 1], pts[n - 2])
                tangents.append(unit(d) if norm(d) > 1e-15 else unit(sub(pts[n - 1], pts[n - 2])))
            else:
                d = sub(pts[i + 1], pts[i - 1])
                tangents.append(unit(d) if norm(d) > 1e-15 else unit(sub(pts[i + 1], pts[i])))
    ctrl: List[Tuple[float, ...]] = [pts[0]]
    knots: List[float] = [0.0, 0.0, 0.0, 0.0]
    s = 0.0
    for i in range(n - 1):
        c = chords[i]
        ctrl.append(add(pts[i], mul(tangents[i], c / 3.0)))
        ctrl.append(sub(pts[i + 1], mul(tangents[i + 1], c / 3.0)))
        ctrl.append(pts[i + 1])
        s += c
        if i < n - 2:
            knots.extend([s, s, s])
    knots.extend([s, s, s, s])
    return NurbsCurve(3, ctrl, knots)


def interpolate_cubic_surface_rows(rows: List[NurbsCurve], params: List[float]) -> NurbsSurface:
    """Loft rows (curves sharing degree/knots/weights) into a surface, cubic in u.

    Each row is a v-curve; rows are interpolated across u with the same
    Catmull-Rom/Bezier scheme as interpolate_cubic, applied to homogeneous
    control points so rational rows (arcs) stay exact.
    """
    if len(rows) < 2:
        raise GeomError("lofting needs at least two rows")
    q = rows[0].degree
    vk = rows[0].knots
    n_v = len(rows[0].ctrl)
    rational = bool(rows[0].weights)
    for r in rows:
        if r.degree != q or len(r.ctrl) != n_v or len(r.knots) != len(vk):
            raise GeomError("lofting rows must share degree and knot structure")
    H = [_homog([tuple(c) for c in r.ctrl], r.weights) for r in rows]  # H[i][j] 4-tuples
    n = len(rows)
    # u parameters: use given params (monotone); tangents by Catmull-Rom on each column
    ctrl_rows: List[List[Tuple[float, ...]]] = []
    uk: List[float] = [params[0]] * 4
    for i in range(n - 1):
        h = params[i + 1] - params[i]
        if h <= 0:
            raise GeomError("loft parameters must increase")
        P0 = H[i]
        P1 = H[i + 1]
        T0 = _col_tangent(H, params, i)
        T1 = _col_tangent(H, params, i + 1)
        if i == 0:
            ctrl_rows.append(P0)
        ctrl_rows.append([tuple(P0[j][d] + T0[j][d] * h / 3.0 for d in range(4)) for j in range(n_v)])
        ctrl_rows.append([tuple(P1[j][d] - T1[j][d] * h / 3.0 for d in range(4)) for j in range(n_v)])
        ctrl_rows.append(P1)
        if i < n - 2:
            uk.extend([params[i + 1]] * 3)
    uk.extend([params[-1]] * 4)
    ctrl = [[(c[0] / c[3], c[1] / c[3], c[2] / c[3]) for c in row] for row in ctrl_rows]
    weights = [[c[3] for c in row] for row in ctrl_rows] if rational else None
    return NurbsSurface(3, q, ctrl, uk, list(vk), weights)


def _col_tangent(H: List[List[Tuple[float, ...]]], params: List[float], i: int) -> List[Tuple[float, ...]]:
    """dH/du estimate at row i for every column (finite differences)."""
    n = len(H)
    out = []
    for j in range(len(H[0])):
        if n == 2:
            a, b, ha = H[0][j], H[1][j], params[1] - params[0]
            out.append(tuple((b[d] - a[d]) / ha for d in range(4)))
        elif i == 0:
            a, b = H[0][j], H[1][j]
            ha = params[1] - params[0]
            c = H[2][j]
            hb = params[2] - params[1]
            # second-order one-sided
            out.append(tuple(((b[d] - a[d]) / ha * (2 * ha + hb) / (ha + hb) - (c[d] - b[d]) / hb * ha / (ha + hb))
                             for d in range(4)))
        elif i == n - 1:
            a, b = H[n - 2][j], H[n - 1][j]
            hb = params[n - 1] - params[n - 2]
            c = H[n - 3][j]
            ha = params[n - 2] - params[n - 3]
            out.append(tuple(((b[d] - a[d]) / hb * (2 * hb + ha) / (ha + hb) - (a[d] - c[d]) / ha * hb / (ha + hb))
                             for d in range(4)))
        else:
            a, b, c = H[i - 1][j], H[i][j], H[i + 1][j]
            ha = params[i] - params[i - 1]
            hb = params[i + 1] - params[i]
            # weighted central difference (non-uniform)
            out.append(tuple(((c[d] - b[d]) / hb * ha + (b[d] - a[d]) / ha * hb) / (ha + hb) for d in range(4)))
    return out


# --- analytic surfaces -----------------------------------------------------------

class Surface:
    """Common interface: eval(u, v), normal(u, v) (natural, unit), project(p)."""

    def eval(self, u: float, v: float) -> Vec:  # pragma: no cover - abstract
        raise NotImplementedError

    def normal(self, u: float, v: float) -> Vec:
        raise NotImplementedError

    def project(self, p: Vec) -> Tuple[float, float]:
        raise NotImplementedError


class Plane(Surface):
    def __init__(self, pvec: Vec, normal: Vec, x_axis: Vec) -> None:
        self.p = pvec
        self.n = unit(normal)
        self.x = unit(x_axis)
        self.y = cross(self.n, self.x)

    def eval(self, u: float, v: float) -> Vec:
        return add(self.p, add(mul(self.x, u), mul(self.y, v)))

    def normal(self, u: float, v: float) -> Vec:
        return self.n

    def project(self, p: Vec) -> Tuple[float, float]:
        d = sub(p, self.p)
        return dot(d, self.x), dot(d, self.y)


class Cylinder(Surface):
    def __init__(self, pvec: Vec, axis: Vec, radius: float, x_axis: Vec) -> None:
        self.p = pvec
        self.a = unit(axis)
        self.r = radius
        self.x = unit(x_axis)
        self.y = cross(self.a, self.x)

    def eval(self, u: float, v: float) -> Vec:
        return add(self.p, add(mul(add(mul(self.x, math.cos(u)), mul(self.y, math.sin(u))), self.r), mul(self.a, v)))

    def normal(self, u: float, v: float) -> Vec:
        return add(mul(self.x, math.cos(u)), mul(self.y, math.sin(u)))

    def project(self, p: Vec) -> Tuple[float, float]:
        d = sub(p, self.p)
        v = dot(d, self.a)
        u = math.atan2(dot(d, self.y), dot(d, self.x))
        return u, v


class Cone(Surface):
    """Cone as written by Parasolid: R(u,v) = P + vA + (X cos u + Y sin u)(r + v tan a).

    The XT Format Reference prints this with "- vA"; real SolidWorks data (and
    the ISO 10303-42 conical_surface, which is identical) use + vA: the apex lies
    on the -A side and the radius grows along +A. Natural normal points outward
    (radially out, tilted towards the apex).
    """

    def __init__(self, pvec: Vec, axis: Vec, radius: float, sin_a: float, cos_a: float, x_axis: Vec) -> None:
        self.p = pvec
        self.a = unit(axis)
        self.r = radius
        self.sin = sin_a
        self.cos = cos_a
        self.tan = sin_a / cos_a
        self.x = unit(x_axis)
        self.y = cross(self.a, self.x)

    def eval(self, u: float, v: float) -> Vec:
        rho = self.r + v * self.tan
        return add(add(self.p, mul(self.a, v)), mul(add(mul(self.x, math.cos(u)), mul(self.y, math.sin(u))), rho))

    def normal(self, u: float, v: float) -> Vec:
        e_r = add(mul(self.x, math.cos(u)), mul(self.y, math.sin(u)))
        # N = rho (e_r - tan a * A) normalised = cos a * e_r - sin a * A  (sign follows rho)
        n = sub(mul(e_r, self.cos), mul(self.a, self.sin))
        if self.r + v * self.tan < 0:
            return mul(n, -1.0)
        return n

    def project(self, p: Vec) -> Tuple[float, float]:
        d = sub(p, self.p)
        h = dot(d, self.a)
        radial = sub(d, mul(self.a, h))
        rr = norm(radial)
        u = math.atan2(dot(d, self.y), dot(d, self.x)) if rr > 1e-300 else 0.0
        # generator line through (r, 0) in (radial, axial) coordinates with unit
        # direction (sin a, cos a); closest point parameter s
        s = (rr - self.r) * self.sin + h * self.cos
        v = s * self.cos
        return u, v


class Sphere(Surface):
    def __init__(self, centre: Vec, radius: float, axis: Vec, x_axis: Vec) -> None:
        self.c = centre
        self.r = radius
        self.a = unit(axis)
        self.x = unit(x_axis)
        self.y = cross(self.a, self.x)

    def eval(self, u: float, v: float) -> Vec:
        e = add(mul(self.x, math.cos(u)), mul(self.y, math.sin(u)))
        return add(self.c, mul(add(mul(e, math.cos(v)), mul(self.a, math.sin(v))), self.r))

    def normal(self, u: float, v: float) -> Vec:
        e = add(mul(self.x, math.cos(u)), mul(self.y, math.sin(u)))
        return add(mul(e, math.cos(v)), mul(self.a, math.sin(v)))

    def project(self, p: Vec) -> Tuple[float, float]:
        d = sub(p, self.c)
        z = dot(d, self.a)
        px, py = dot(d, self.x), dot(d, self.y)
        u = math.atan2(py, px) if (abs(px) + abs(py)) > 1e-300 else 0.0
        v = math.atan2(z, math.hypot(px, py))
        return u, v


class Torus(Surface):
    def __init__(self, centre: Vec, axis: Vec, major: float, minor: float, x_axis: Vec) -> None:
        self.c = centre
        self.a = unit(axis)
        self.R = major
        self.rr = minor
        self.x = unit(x_axis)
        self.y = cross(self.a, self.x)

    def eval(self, u: float, v: float) -> Vec:
        e = add(mul(self.x, math.cos(u)), mul(self.y, math.sin(u)))
        return add(self.c, add(mul(e, self.R + self.rr * math.cos(v)), mul(self.a, self.rr * math.sin(v))))

    def normal(self, u: float, v: float) -> Vec:
        e = add(mul(self.x, math.cos(u)), mul(self.y, math.sin(u)))
        n = add(mul(e, math.cos(v)), mul(self.a, math.sin(v)))
        # natural normal = dP/du x dP/dv; sign follows (R + r cos v)
        if self.R + self.rr * math.cos(v) < 0:
            return mul(n, -1.0)
        return n

    def project(self, p: Vec) -> Tuple[float, float]:
        d = sub(p, self.c)
        z = dot(d, self.a)
        px, py = dot(d, self.x), dot(d, self.y)
        u = math.atan2(py, px) if (abs(px) + abs(py)) > 1e-300 else 0.0
        rho = math.hypot(px, py)
        # rho = R + r cos v, z = r sin v holds for doughnut, apple and lemon alike
        v = math.atan2(z, rho - self.R)
        return u, v


class SweptSurface(Surface):
    def __init__(self, section: Callable[[float], Tuple[Vec, Vec]], t0: float, t1: float, sweep: Vec) -> None:
        self.section = section  # t -> (point, tangent)
        self.t0 = t0
        self.t1 = t1
        self.d = unit(sweep)

    def eval(self, u: float, v: float) -> Vec:
        return add(self.section(u)[0], mul(self.d, v))

    def normal(self, u: float, v: float) -> Vec:
        return unit(cross(self.section(u)[1], self.d))

    def project(self, p: Vec) -> Tuple[float, float]:
        # minimise over u the distance from p to the line C(u) + v D
        best = None
        n = 64
        for i in range(n + 1):
            t = self.t0 + (self.t1 - self.t0) * i / n
            c, _ = self.section(t)
            dd = sub(p, c)
            v = dot(dd, self.d)
            e = norm(sub(dd, mul(self.d, v)))
            if best is None or e < best[0]:
                best = (e, t, v)
        _, t, v = best
        # refine by golden-section around t
        lo = max(self.t0, t - (self.t1 - self.t0) / n)
        hi = min(self.t1, t + (self.t1 - self.t0) / n)

        def f(tt: float) -> float:
            c, _ = self.section(tt)
            dd = sub(p, c)
            return norm(sub(dd, mul(self.d, dot(dd, self.d))))

        t = golden(f, lo, hi)
        c, _ = self.section(t)
        return t, dot(sub(p, c), self.d)


class SpunSurface(Surface):
    def __init__(self, profile: Callable[[float], Tuple[Vec, Vec]], t0: float, t1: float, base: Vec, axis: Vec) -> None:
        self.profile = profile
        self.t0 = t0
        self.t1 = t1
        self.base = base
        self.a = unit(axis)

    def eval(self, u: float, v: float) -> Vec:
        c, _ = self.profile(u)
        z = add(self.base, mul(self.a, dot(sub(c, self.base), self.a)))
        r = sub(c, z)
        return add(z, add(mul(r, math.cos(v)), mul(cross(self.a, r), math.sin(v))))

    def normal(self, u: float, v: float) -> Vec:
        c, tc = self.profile(u)
        z = add(self.base, mul(self.a, dot(sub(c, self.base), self.a)))
        r = sub(c, z)
        # dP/du = rotate(tc), dP/dv = rotate(A x r)
        Su = rotate(tc, self.a, v)
        Sv = rotate(cross(self.a, r), self.a, v)
        return unit(cross(Su, Sv))

    def project(self, p: Vec) -> Tuple[float, float]:
        d = sub(p, self.base)
        h = dot(d, self.a)
        radial = sub(d, mul(self.a, h))
        rr = norm(radial)
        # find u minimising distance in the (axial, radial) half-plane
        def f(t: float) -> float:
            c, _ = self.profile(t)
            dc = sub(c, self.base)
            hc = dot(dc, self.a)
            rc = norm(sub(dc, mul(self.a, hc)))
            return math.hypot(hc - h, rc - rr)

        n = 64
        best = min(range(n + 1), key=lambda i: f(self.t0 + (self.t1 - self.t0) * i / n))
        t = self.t0 + (self.t1 - self.t0) * best / n
        t = golden(f, max(self.t0, t - (self.t1 - self.t0) / n), min(self.t1, t + (self.t1 - self.t0) / n))
        c, _ = self.profile(t)
        dc = sub(c, self.base)
        rcv = sub(dc, mul(self.a, dot(dc, self.a)))
        if norm(rcv) < 1e-300 or rr < 1e-300:
            return t, 0.0
        x = unit(rcv)
        y = cross(self.a, x)
        v = math.atan2(dot(radial, y), dot(radial, x))
        return t, v


class NurbsSurfaceAdapter(Surface):
    """Surface interface over a NurbsSurface with a cached sample grid for projection.

    `ext` optionally widens the parameter domain used for projection (Parasolid
    B-surfaces may be implicitly extended beyond their knot range).
    """

    def __init__(self, s: NurbsSurface, ext: Optional[Tuple[float, float, float, float]] = None,
                 grid: int = 20) -> None:
        self.s = s
        du = s.u1 - s.u0
        dv = s.v1 - s.v0
        if ext is None:
            ext = (s.u0 - 0.25 * du, s.u1 + 0.25 * du, s.v0 - 0.25 * dv, s.v1 + 0.25 * dv)
        self.ext = ext
        self.grid = grid
        self._samples: Optional[List[Tuple[float, float, Vec]]] = None
        self._last: Optional[Tuple[float, float]] = None

    def eval(self, u: float, v: float) -> Vec:
        return self.s.eval(u, v)

    def normal(self, u: float, v: float) -> Vec:
        return self.s.normal(u, v)

    def _grid(self) -> List[Tuple[float, float, Vec]]:
        if self._samples is None:
            s = self.s
            g = self.grid
            out = []
            for i in range(g + 1):
                u = s.u0 + (s.u1 - s.u0) * i / g
                for j in range(g + 1):
                    v = s.v0 + (s.v1 - s.v0) * j / g
                    out.append((u, v, s.eval(u, v)))
            self._samples = out
        return self._samples

    def project(self, p: Vec) -> Tuple[float, float]:
        best = None
        for u, v, q in self._grid():
            d = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 + (q[2] - p[2]) ** 2
            if best is None or d < best[0]:
                best = (d, u, v)
        cands = [(best[1], best[2])]
        if self._last is not None:
            cands.append(self._last)
        res = None
        for u0, v0 in cands:
            u, v, d = newton_project(self.s, p, u0, v0, self.ext)
            if res is None or d < res[2]:
                res = (u, v, d)
        self._last = (res[0], res[1])
        return res[0], res[1]


def newton_project(s: NurbsSurface, p: Vec, u: float, v: float,
                   dom: Tuple[float, float, float, float]) -> Tuple[float, float, float]:
    """Damped Gauss-Newton closest-point iteration from (u, v) within dom."""
    u0, u1, v0, v1 = dom
    S, Su, Sv = s.eval_derivs(u, v)
    d = dist(S, p)
    for _ in range(60):
        r = sub(S, p)
        a11 = dot(Su, Su)
        a12 = dot(Su, Sv)
        a22 = dot(Sv, Sv)
        b1 = -dot(r, Su)
        b2 = -dot(r, Sv)
        det = a11 * a22 - a12 * a12
        if abs(det) < 1e-300:
            break
        du = (b1 * a22 - b2 * a12) / det
        dv = (a11 * b2 - a12 * b1) / det
        step = 1.0
        improved = False
        for _h in range(12):
            un = min(max(u + step * du, u0), u1)
            vn = min(max(v + step * dv, v0), v1)
            Sn, Sun, Svn = s.eval_derivs(un, vn)
            dn = dist(Sn, p)
            if dn <= d:
                u, v, S, Su, Sv, d = un, vn, Sn, Sun, Svn, dn
                improved = True
                break
            step *= 0.5
        if not improved or (abs(du) < 1e-13 and abs(dv) < 1e-13):
            break
    return u, v, d


def rotate(v: Vec, axis: Vec, ang: float) -> Vec:
    c, s = math.cos(ang), math.sin(ang)
    return add(add(mul(v, c), mul(cross(axis, v), s)), mul(axis, dot(axis, v) * (1 - c)))


def golden(f: Callable[[float], float], a: float, b: float, iters: int = 60) -> float:
    g = (math.sqrt(5.0) - 1) / 2
    c = b - g * (b - a)
    d = a + g * (b - a)
    fc, fd = f(c), f(d)
    for _ in range(iters):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = f(d)
    return 0.5 * (a + b)


def project_newton(s: NurbsSurface, p: Vec, grid: int = 12) -> Tuple[float, float]:
    """Closest-point projection onto a NURBS surface: coarse grid + Newton."""
    best = None
    for i in range(grid + 1):
        u = s.u0 + (s.u1 - s.u0) * i / grid
        for j in range(grid + 1):
            v = s.v0 + (s.v1 - s.v0) * j / grid
            d = dist(s.eval(u, v), p)
            if best is None or d < best[0]:
                best = (d, u, v)
    _, u, v = best
    for _ in range(40):
        S, Su, Sv = s.eval_derivs(u, v)
        r = sub(S, p)
        # Gauss-Newton step on f(u,v) = |S - p|^2 / 2
        a11 = dot(Su, Su)
        a12 = dot(Su, Sv)
        a22 = dot(Sv, Sv)
        b1 = -dot(r, Su)
        b2 = -dot(r, Sv)
        det = a11 * a22 - a12 * a12
        if abs(det) < 1e-30:
            break
        du = (b1 * a22 - b2 * a12) / det
        dv = (a11 * b2 - a12 * b1) / det
        u = min(max(u + du, s.u0), s.u1)
        v = min(max(v + dv, s.v0), s.v1)
        if abs(du) < 1e-13 and abs(dv) < 1e-13:
            break
    return u, v
