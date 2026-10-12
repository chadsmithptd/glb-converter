from __future__ import annotations

import math
import struct
from functools import cmp_to_key
from typing import Any, Callable, Sequence, TypeVar, Union

T = TypeVar("T")

NAN: float = math.nan
INFINITY: float = math.inf
NEGATIVE_INFINITY: float = -math.inf

JS_UNDEFINED = object()

_LOCALE_RANK = {ch: idx for idx, ch in enumerate(":0123456789aAcdeilnopry")}


class RangeError(ValueError):
    """Raised when a buffer read would overrun, matching Node RangeError."""


def _double_bits(v: float) -> int:
    return struct.unpack(">Q", struct.pack(">d", v))[0]


def _from_bits(bits: int) -> float:
    return struct.unpack(">d", struct.pack(">Q", bits & ((1 << 64) - 1)))[0]


def _is_neg_zero(v: float) -> bool:
    return v == 0.0 and math.copysign(1.0, v) < 0.0


def _abs_fraction(v: float) -> tuple[int, int]:
    if v == 0.0:
        return 0, 1
    bits = _double_bits(v)
    exp = (bits >> 52) & 0x7FF
    frac = bits & ((1 << 52) - 1)
    if exp == 0x7FF:
        raise ValueError("non-finite")
    if exp == 0:
        e = -1022 - 52
        m = frac
        while m and (m & 1) == 0:
            m >>= 1
            e += 1
    else:
        e = exp - 1023 - 52
        m = (1 << 52) | frac
    if e >= 0:
        return m << e, 1
    return m, 1 << (-e)


def _ge10(num: int, den: int, exp: int) -> bool:
    if exp > 308:
        return True
    if exp < -324:
        return False
    if exp >= 0:
        return num >= den * (10 ** exp)
    return num * (10 ** (-exp)) >= den


def _ilog10_frac(num: int, den: int, v_abs: float) -> int:
    m, e = math.frexp(v_abs)
    est = int(math.floor(math.log10(m) + e * math.log10(2)))
    while not _ge10(num, den, est):
        est -= 1
    while _ge10(num, den, est + 1):
        est += 1
    return est


def _normalize_exponential(s: str) -> str:
    mant, exp = s.split("e", 1)
    if "." in mant:
        mant = mant.rstrip("0").rstrip(".")
    return mant + "e" + exp


def to_precision(v: float, digits: int = 15) -> str:
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "-Infinity" if v < 0 else "Infinity"
    if v == 0.0:
        return "0." + ("0" * (digits - 1))
    sign = "-" if v < 0 else ""
    num, den = _abs_fraction(v)
    exp10 = _ilog10_frac(num, den, abs(v))
    scale = digits - 1 - exp10
    if scale >= 0:
        scaled_num = num * (10 ** scale)
        scaled_den = den
    else:
        scaled_num = num
        scaled_den = den * (10 ** (-scale))
    q, r = divmod(scaled_num, scaled_den)
    if r * 2 >= scaled_den:
        q += 1
    digits_s = str(q)
    if len(digits_s) > digits:
        extra = len(digits_s) - digits
        exp10 += extra
        digits_s = digits_s[:digits]
    elif len(digits_s) < digits:
        digits_s = digits_s + ("0" * (digits - len(digits_s)))
    if exp10 < -6 or exp10 >= digits:
        mant = digits_s[0] + "." + digits_s[1:]
        return sign + mant + "e" + f"{exp10:+d}"
    decimal_point = exp10 + 1
    if decimal_point <= 0:
        body = "0." + ("0" * (-decimal_point)) + digits_s
    else:
        m = min(len(digits_s), decimal_point)
        body = digits_s[:m]
        if decimal_point < digits:
            if len(digits_s) > decimal_point:
                body += "." + digits_s[decimal_point:]
            else:
                body += "."
            extra = digits - len(digits_s)
            if extra > 0:
                body += "0" * extra
    return sign + body


def _round_to_fixed(v: float, digits: int) -> float:
    factor = 10.0 ** digits
    if v >= 0:
        return math.floor(v * factor + 0.5) / factor
    return math.ceil(v * factor - 0.5) / factor


def _format_large_fixed(v: float, digits: int, neg: bool) -> str:
    av = abs(v)
    for prec in range(15, 22):
        s = format(av, f".{prec}g")
        try:
            if float(s) == av:
                break
        except OverflowError:
            continue
    else:
        s = format(av, ".17g")
    if "e" in s.lower():
        mant, ep = s.lower().split("e")
        e = int(ep)
        return ("-" if neg else "") + mant + f"e{e:+d}"
    return ("-" if neg else "") + s


def to_fixed(v: float, digits: int) -> str:
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "-Infinity" if v < 0 else "Infinity"
    if v == 0.0 or _is_neg_zero(v):
        return "0." + ("0" * digits)
    neg = v < 0
    if abs(v) >= 1e21:
        return _format_large_fixed(v, digits, neg)
    num, den = _abs_fraction(v)
    scaled_num = num * (10 ** digits)
    q, r = divmod(scaled_num, den)
    if r * 2 >= den:
        q += 1
    if q == 0:
        if neg:
            return "-0." + ("0" * digits)
        return "0." + ("0" * digits)
    s = str(q)
    if len(s) <= digits:
        body = "0." + ("0" * (digits - len(s))) + s
    else:
        body = s[:-digits] + "." + s[-digits:]
    int_len = len(body.split(".", 1)[0].lstrip("-"))
    if int_len > 21:
        rounded = v if js_is_integer(v) else _round_to_fixed(v, digits)
        exp_s = to_precision(rounded, 17)
        if exp_s.startswith("-"):
            exp_s = exp_s[1:]
        else:
            neg = rounded < 0
        return ("-" if neg else "") + _normalize_exponential(exp_s)
    return ("-" if neg else "") + body


def js_is_integer(v: float) -> bool:
    return math.isfinite(v) and v == math.trunc(v)


def fmt_float(v: float) -> str:
    if js_is_integer(v):
        return to_fixed(v, 1)
    s = to_precision(v, 15)
    return s if "." in s else s + "."


def locale_compare(a: str, b: str) -> int:
    length = max(len(a), len(b))
    for i in range(length):
        ca = a[i] if i < len(a) else None
        cb = b[i] if i < len(b) else None
        if ca is None:
            return -1
        if cb is None:
            return 1
        ra = _LOCALE_RANK.get(ca)
        rb = _LOCALE_RANK.get(cb)
        if ra is None or rb is None:
            if ca == cb:
                continue
            return -1 if ca < cb else 1
        if ra != rb:
            return -1 if ra < rb else 1
    return 0


def buf_to_ascii(data: bytes) -> str:
    return "".join(chr(b & 0x7F) for b in data)


def buf_to_utf16le(data: bytes) -> str:
    if len(data) % 2:
        data = data + b"\x00"
    out = []
    for i in range(0, len(data), 2):
        out.append(chr(data[i] | (data[i + 1] << 8)))
    return "".join(out)


def _require_length(buf: bytes, offset: int, size: int) -> None:
    if offset < 0 or offset + size > len(buf):
        max_off = max(0, len(buf) - size)
        raise RangeError(
            'The value of "offset" is out of range. It must be >= 0 and <= '
            f"{max_off}. Received {offset}"
        )


def read_u32le(buf: bytes, offset: int) -> int:
    _require_length(buf, offset, 4)
    return struct.unpack_from("<I", buf, offset)[0]


def read_i32le(buf: bytes, offset: int) -> int:
    _require_length(buf, offset, 4)
    return struct.unpack_from("<i", buf, offset)[0]


def read_u16le(buf: bytes, offset: int) -> int:
    _require_length(buf, offset, 2)
    return struct.unpack_from("<H", buf, offset)[0]


def read_f64be(buf: bytes, offset: int) -> float:
    _require_length(buf, offset, 8)
    return struct.unpack_from(">d", buf, offset)[0]


def js_round(v: float) -> float:
    if math.isnan(v):
        return NAN
    if math.isinf(v):
        return v
    if v == 0.0:
        return _from_bits(0x8000000000000000) if _is_neg_zero(v) else 0.0
    if -0.5 <= v < 0.0:
        return _from_bits(0x8000000000000000)
    return math.floor(v + 0.5)


def to_int32(v: float) -> int:
    v = _from_bits(_double_bits(v))
    if not math.isfinite(v) or v == 0.0:
        return 0
    v = math.trunc(v) & 0xFFFFFFFF
    if v >= 0x80000000:
        v -= 0x100000000
    return v


def to_uint32(v: float) -> int:
    v = _from_bits(_double_bits(v))
    if not math.isfinite(v) or v == 0.0:
        return 0
    return int(math.trunc(v)) & 0xFFFFFFFF


def js_shr(a: int, b: int) -> int:
    return to_int32(a) >> (to_uint32(b) & 0x1F)


def js_shl(a: int, b: int) -> int:
    return to_int32(to_int32(a) << (to_uint32(b) & 0x1F))


def js_and(a: int, b: int) -> int:
    return to_int32(a) & to_int32(b)


def parse_int(s: str, radix: int = 10) -> float:
    if radix != 10:
        raise ValueError("parse_int supports radix 10 only")
    stripped = s.lstrip()
    if not stripped:
        return NAN
    sign = 1
    if stripped[0] in "+-":
        if stripped[0] == "-":
            sign = -1
        stripped = stripped[1:]
    if stripped.startswith("0x") or stripped.startswith("0X"):
        return 0.0
    digits = []
    for ch in stripped:
        if "0" <= ch <= "9":
            digits.append(ch)
        else:
            break
    if not digits:
        return NAN
    return float(sign * int("".join(digits), 10))


def js_div(a: float, b: float) -> float:
    if b == 0.0:
        if a == 0.0 or math.isnan(a):
            return NAN
        return INFINITY if a > 0 else NEGATIVE_INFINITY
    return a / b


def at(seq: Sequence[T], index: int) -> Union[T, object]:
    if isinstance(index, float):
        if not math.isfinite(index):
            return JS_UNDEFINED
        index = to_int32(index)
    if index < 0 or index >= len(seq):
        return JS_UNDEFINED
    return seq[index]


def cmp_key(cmp: Callable[[T, T], float]) -> Callable[[T], T]:
    def normalized(x: T, y: T) -> float:
        result = cmp(x, y)
        if result != result:
            return 0.0
        if result > 0:
            return 1.0
        if result < 0:
            return -1.0
        return 0.0

    return cmp_to_key(lambda x, y: normalized(x, y))
