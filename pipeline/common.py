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


# ── 기술적 신호 · 시장 폭 · 업종 RS ─────────────────────────────
def _ma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def tech(h, D, drop_pct=-7.0):
    """h: [(d,o,h,l,c,v)] 오래된 순, 마지막이 D. → 이동평균·고점 대비·신호 (대시보드 관심 종목용)"""
    rows = [r for r in h if r[0] <= D]
    if len(rows) < 25 or rows[-1][0] != D:
        return None
    c = [r[4] for r in rows]
    hi = [r[2] for r in rows]
    lo = [r[3] for r in rows]
    cur, prev = c[-1], c[-2]
    out = {"price": round(cur, 2), "chg": round((cur / prev - 1) * 100, 2) if prev else None}
    ma20, ma60, ma200 = _ma(c, 20), _ma(c, 60), _ma(c, 200)
    pma20 = _ma(c[:-1], 20)
    pma60 = _ma(c[:-1], 60)
    sig = []
    if ma20:
        out["ma20"] = round(ma20, 2); out["pMa20"] = round((cur / ma20 - 1) * 100, 1)
        if pma20 and prev >= pma20 and cur < ma20: sig.append("MA20_DN")
        if pma20 and prev < pma20 and cur >= ma20: sig.append("MA20_UP")
    if ma60:
        out["ma60"] = round(ma60, 2); out["pMa60"] = round((cur / ma60 - 1) * 100, 1)
        if pma60 and prev >= pma60 and cur < ma60: sig.append("MA60_DN")
    if ma200:
        out["ma200"] = round(ma200, 2); out["pMa200"] = round((cur / ma200 - 1) * 100, 1)
    if len(rows) >= 21:
        h60 = max(hi[-61:-1]) if len(rows) >= 61 else max(hi[:-1])
        out["hi60"] = round(h60, 2); out["dHi60"] = round((cur / h60 - 1) * 100, 1)
        if cur > h60: sig.append("NH60")
        l20 = min(lo[-21:-1])
        if cur < l20: sig.append("NL20")
        out["ret20"] = round((cur / c[-21] - 1) * 100, 1)
    if len(rows) >= 61:
        out["ret60"] = round((cur / c[-61] - 1) * 100, 1)
    if len(rows) >= 200:
        h52 = max(hi[-251:-1]); out["hi52"] = round(h52, 2); out["dHi52"] = round((cur / h52 - 1) * 100, 1)
        if cur > h52: sig.append("NH52")
        if cur < min(lo[-251:-1]): sig.append("NL52")
    if out["chg"] is not None and out["chg"] <= drop_pct:
        sig.append("DROP")
    # 최근 10거래일 중 고점 경신 일수(추세 지속성)
    if len(rows) >= 40:
        k = 0
        for i in range(len(rows) - 10, len(rows)):
            if hi[i] > max(hi[max(0, i - 30):i]): k += 1
        out["nh10"] = k
    out["sig"] = sig
    return out


def breadth(techs):
    """techs: {code: tech()} → 시장 폭 한 줄"""
    t = [x for x in techs.values() if x]
    n = len(t)
    if not n:
        return None
    def share(key):
        v = [x for x in t if x.get(key) is not None]
        return round(100 * sum(1 for x in v if x[key] > 0) / len(v), 1) if v else None
    row = {"n": n, "a20": share("pMa20"), "a60": share("pMa60"), "a200": share("pMa200"),
           "nh60": sum(1 for x in t if "NH60" in x["sig"]), "nl20": sum(1 for x in t if "NL20" in x["sig"]),
           "nh52": sum(1 for x in t if "NH52" in x["sig"]), "nl52": sum(1 for x in t if "NL52" in x["sig"]),
           "drop": sum(1 for x in t if "DROP" in x["sig"])}
    r20 = sorted(x["ret20"] for x in t if x.get("ret20") is not None)
    if r20:
        row["medR20"] = round(r20[len(r20) // 2], 1)
    return row


def append_series(path, date, row, keep=260):
    """날짜별 한 줄씩 쌓는 파일(breadth, sector_rs). 같은 날짜는 덮어씀."""
    rows = []
    if os.path.exists(path):
        try:
            rows = json.load(open(path, encoding="utf-8")).get("rows", [])
        except Exception:
            rows = []
    rows = [r for r in rows if r.get("date") != date]
    row = dict(row); row["date"] = date
    rows.append(row)
    rows.sort(key=lambda r: r["date"])
    rows = rows[-keep:]
    write_json(path, {"rows": rows})
    return rows


def sector_rs(rows, techs, sector_of, min_n=3):
    """rows: 유니버스(code·name·rsr…), sector_of: code→업종. 업종별 중앙값·평균 RS."""
    by = {}
    for x in rows:
        s = sector_of.get(x["code"])
        t = techs.get(x["code"])
        if not s or not t:
            continue
        by.setdefault(s, []).append((x, t))
    out = []
    for s, lst in by.items():
        if len(lst) < min_n:
            continue
        r60 = sorted(t.get("ret60") for x, t in lst if t.get("ret60") is not None)
        r20 = sorted(t.get("ret20") for x, t in lst if t.get("ret20") is not None)
        rsr = [x.get("rsr") for x, t in lst if x.get("rsr") is not None]
        a20 = [t for x, t in lst if t.get("pMa20") is not None]
        top = sorted(lst, key=lambda p: -(p[0].get("rsr") or 0))[:3]
        out.append({"sector": s, "n": len(lst),
                    "r60": round(r60[len(r60) // 2], 1) if r60 else None,
                    "r20": round(r20[len(r20) // 2], 1) if r20 else None,
                    "rsr": round(sum(rsr) / len(rsr), 1) if rsr else None,
                    "a20": round(100 * sum(1 for t in a20 if t["pMa20"] > 0) / len(a20)) if a20 else None,
                    "nh60": sum(1 for x, t in lst if "NH60" in t["sig"]),
                    "top": [x["code"] for x, t in top]})
    out.sort(key=lambda r: -(r["rsr"] or 0))
    return out


# ── 업종 사이클 · 건강 신호 ──────────────────────────────────────
def _ew_index(members, min_bars):
    """members: [[(d,o,h,l,c,v)]…] → 동일가중 지수 [(date, value)] (각 종목을 공통 시작일 종가=1로 맞춘 평균)."""
    series = []
    for h in members:
        m = {r[0]: r[4] for r in h if r[4] and r[4] > 0}
        if len(m) >= min_bars:
            series.append(m)
    if len(series) < 3:
        return None
    dates = sorted(set.intersection(*[set(s.keys()) for s in series]))
    if len(dates) < min_bars:
        # 공통 날짜가 모자라면 날짜별로 있는 종목만 평균(상장 시점이 다른 경우)
        dates = sorted(set.union(*[set(s.keys()) for s in series]))
        dates = [d for d in dates if sum(1 for s in series if d in s) >= max(3, len(series) * 0.6)]
        if len(dates) < min_bars:
            return None
    out = []
    base = {}
    for d in dates:
        vals = []
        for i, s in enumerate(series):
            if d in s:
                if i not in base:
                    base[i] = s[d]
                vals.append(s[d] / base[i])
        out.append((d, sum(vals) / len(vals)))
    return out


def _swings(idx, up=15.0, dn=12.0):
    """지그재그: 저점에서 up% 오르면 상승 구간, 고점에서 dn% 빠지면 하락 구간. [(kind, from, to, ret)]"""
    if not idx or len(idx) < 20:
        return []
    segs = []
    dir_ = None
    piv_i, piv_v = 0, idx[0][1]
    ext_i, ext_v = 0, idx[0][1]
    for i in range(1, len(idx)):
        v = idx[i][1]
        if dir_ is None:
            if v >= piv_v * (1 + up / 100): dir_ = 1; ext_i, ext_v = i, v
            elif v <= piv_v * (1 - dn / 100): dir_ = -1; ext_i, ext_v = i, v
            else:
                if v > ext_v and v > piv_v: pass
            if dir_ is None:
                # 아직 방향 없음: 임시 극값 갱신
                if v > ext_v: ext_i, ext_v = i, v
                continue
        if dir_ == 1:
            if v > ext_v: ext_i, ext_v = i, v
            elif v <= ext_v * (1 - dn / 100):
                segs.append(("up", idx[piv_i][0], idx[ext_i][0], (ext_v / piv_v - 1) * 100))
                piv_i, piv_v = ext_i, ext_v; dir_ = -1; ext_i, ext_v = i, v
        else:
            if v < ext_v: ext_i, ext_v = i, v
            elif v >= ext_v * (1 + up / 100):
                segs.append(("down", idx[piv_i][0], idx[ext_i][0], (ext_v / piv_v - 1) * 100))
                piv_i, piv_v = ext_i, ext_v; dir_ = 1; ext_i, ext_v = i, v
    # 진행 중인 마지막 구간
    if dir_ is not None and ext_i > piv_i:
        segs.append(("up" if dir_ == 1 else "down", idx[piv_i][0], idx[ext_i][0], (ext_v / piv_v - 1) * 100, True))
    return segs


def sector_cycles(members_of, hist, D, min_bars=150, prev_nh=None):
    """members_of: {업종: [code…]}, hist: {code: rows}. 업종별 동일가중 지수의 상승·급락 구간과 현재 위치, 건강 신호.
    prev_nh: {업종: 5거래일 전 신고가 종목 수} (있으면 비교)."""
    out = {}
    for sec, codes in members_of.items():
        rows = [[r for r in hist.get(c, []) if r[0] <= D] for c in codes]
        rows = [h for h in rows if h and h[-1][0] == D]
        idx = _ew_index(rows, min_bars)
        if not idx:
            continue
        segs = _swings(idx)
        vals = [v for _, v in idx]
        cur = vals[-1]
        byd = {d: v for d, v in idx}
        # 최근 고점·저점 = 마지막 구간의 양 끝(상승 중이면 시작=저점·끝=고점, 하락 중이면 그 반대). 구간이 없으면 전체 극값.
        if segs:
            s = segs[-1]
            hi_d, lo_d = (s[2], s[1]) if s[0] == "up" else (s[1], s[2])
        else:
            hi_d = max(idx, key=lambda p: p[1])[0]; lo_d = min(idx, key=lambda p: p[1])[0]
        hi_v, lo_v = byd[hi_d], byd[lo_d]
        ma20 = sum(vals[-20:]) / 20 if len(vals) >= 20 else None
        # 건강: 업종 거래대금 20일 평균 대비, 20일선 위 비중, 오늘 신고가 수
        def dval(h, k):
            return h[k][4] * h[k][5] if len(h) > abs(k) else 0
        v_today = sum(dval(h, -1) for h in rows)
        v_avg = sum(sum(dval(h, -k) for k in range(2, 22)) / 20 for h in rows if len(h) >= 22)
        above = [1 if (len(h) >= 20 and h[-1][4] > sum(r[4] for r in h[-20:]) / 20) else 0 for h in rows]
        def nh_count(k):
            n = 0
            for h in rows:
                if len(h) < 61 + k: continue
                sub = h[:len(h) - k] if k else h
                if sub[-1][4] > max(r[2] for r in sub[-61:-1]): n += 1
            return n
        nh0, nh5 = nh_count(0), nh_count(5)
        out[sec] = {
            "n": len(rows),
            "swings": [{"k": s[0], "from": s[1], "to": s[2], "ret": round(s[3], 1), "live": len(s) > 4} for s in segs[-6:]],
            "now": {"offHi": round((cur / hi_v - 1) * 100, 1), "hiAt": hi_d,
                    "offLo": round((cur / lo_v - 1) * 100, 1), "loAt": lo_d,
                    "vsMa20": round((cur / ma20 - 1) * 100, 1) if ma20 else None,
                    "r20": round((cur / vals[-21] - 1) * 100, 1) if len(vals) > 21 else None},
            "health": {"valx": round(v_today / v_avg, 2) if v_avg else None,
                       "a20": round(100 * sum(above) / len(above)) if above else None,
                       "nh": nh0, "nh5ago": nh5},
            "span": [idx[0][0], idx[-1][0]],
        }
    return out
