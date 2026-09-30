# 공통 도우미 — 대시보드가 읽는 JSON 형식에 맞춘 포맷터·순위 계산
import bisect
import datetime as dt
import json
import math
import os
import time

KST = dt.timezone(dt.timedelta(hours=9))


def now_kst():
    return dt.datetime.now(KST)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def isnum(v):
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def won_str(v):
    """원 단위 숫자 → '1.74조' / '9,800억'"""
    if not isnum(v):
        return ""
    v = float(v)
    if v >= 1e12:
        return f"{v/1e12:.2f}조"
    return f"{v/1e8:,.0f}억"


def usd_str(v):
    """달러 단위 숫자 → '1.20조달러' / '430억달러' / '9,800만달러'"""
    if not isnum(v):
        return ""
    v = float(v)
    if v >= 1e12:
        return f"{v/1e12:.2f}조달러"
    if v >= 1e8:
        return f"{v/1e8:,.0f}억달러"
    return f"{v/1e4:,.0f}만달러"


def usd_tag(v):
    """달러 단위 숫자 → 대시보드 저장 형식 '$1.20T' / '$269B' / '$3.1B' / '$84M' (화면에서는 억달러로 바꿔 표시됨)"""
    if not isnum(v):
        return ""
    v = float(v)
    if v >= 1e12:
        return f"${v/1e12:.2f}T"
    if v >= 1e10:
        return f"${v/1e9:.0f}B"
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e7:
        return f"${v/1e6:.0f}M"
    return f"${v/1e6:.1f}M"


def rsr_rank(returns):
    """{code: 60일 수익률} → {code: 1~99 등급}. 나보다 낮은 종목 비율 기반 백분위."""
    vals = sorted(float(v) for v in returns.values() if isnum(v))
    n = len(vals)
    out = {}
    if n < 20:
        return out
    for k, v in returns.items():
        if not isnum(v):
            continue
        below = bisect.bisect_left(vals, float(v))
        out[k] = int(round(1 + 98 * below / n))
    return out


def ohlc_str(rows, decimals=0):
    """[(YYYYMMDD, o, h, l, c, v), ...] 오래된 순 → 대시보드 ohlc 문자열(뒤 70개)"""
    rows = [r for r in rows if isnum(r[4]) and float(r[4]) > 0][-70:]
    if decimals:
        return " ".join(f"{r[0]},{r[1]:.{decimals}f},{r[2]:.{decimals}f},{r[3]:.{decimals}f},{r[4]:.{decimals}f},{int(r[5] or 0)}" for r in rows)
    return " ".join(f"{r[0]},{int(round(r[1]))},{int(round(r[2]))},{int(round(r[3]))},{int(round(r[4]))},{int(r[5] or 0)}" for r in rows)


def close_strength(h, l, c):
    if not (isnum(h) and isnum(l) and isnum(c)) or h <= l:
        return None
    return int(round((c - l) / (h - l) * 100))


def verdict(o, h, l, c):
    cs = close_strength(h, l, c)
    if cs is None:
        return None, ""
    parts = [f"종가강도 {cs}"]
    if cs >= 70:
        parts.append("고가권 마감")
    elif cs <= 30:
        parts.append("밀려서 마감")
    else:
        parts.append("중간권 마감")
    if h > l and (h - c) / (h - l) * 100 >= 40:
        parts.append("윗꼬리 김")
    if isnum(o) and o > 0 and c < o and (o - c) / o >= 0.03:
        parts.append("장대음봉")
    return cs, " · ".join(parts)


def ma_regime(closes):
    """종가 리스트(오래된 순) → index, ma200, ma200Falling, ret60"""
    out = {}
    if not closes:
        return out
    out["index"] = round(float(closes[-1]), 2)
    if len(closes) >= 200:
        out["ma200"] = round(sum(closes[-200:]) / 200, 2)
        if len(closes) >= 220:
            prev = sum(closes[-220:-20]) / 200
            out["ma200Falling"] = bool(out["ma200"] < prev)
    if len(closes) >= 61 and closes[-61]:
        out["ret60"] = round((closes[-1] / closes[-61] - 1) * 100, 2)
    return out


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    log("wrote", path, os.path.getsize(path), "bytes")


def write_status(market, ok, msg, extra=None):
    st = {"market": market, "ok": bool(ok), "msg": msg, "at": now_kst().isoformat(timespec="seconds")}
    if extra:
        st.update(extra)
    write_json(f"data/status_{market.lower()}.json", st)
