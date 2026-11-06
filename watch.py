"""
급변 알림 — Must See 마켓 '알림 설정'을 읽어 지수·종목이 크게 움직이면 텔레그램으로 알림.

설정: doni-must-see 저장소 stars 브랜치 alert_conf.bin (Must See 비밀번호로 암호화, 화면에서 저장)
  {v, updated, on, macro, tg: {token, chat, bot}, base: {키: true/false}, rules: {묶음: [하루, 30분]},
   watch: [{c: "005930" | "NVDA" | "7203.T", n: "이름(선택)"}]}
판단 (5분마다)
  - 하루: 전일 종가 대비. 기준을 넘으면 1번, 기준의 2배·3배를 넘을 때마다 1번 더 (같은 날·같은 방향)
  - 30분: 최근 30분 사이 변동. 같은 방향은 45분에 1번만
  - 시세가 45분 넘게 멈춰 있으면(장 마감·휴장) 판단하지 않음
"""

import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
CONF_URL = os.environ.get("DONI_CONF_URL", "https://api.github.com/repos/doniddoni/doni-must-see/contents/alert_conf.bin?ref=stars")

# 기본 감시 대상: (키, 야후 기호, 이름, 묶음)
BASE = [
    ("ES", "ES=F", "S&P 선물", "idx"), ("NQ", "NQ=F", "나스닥 선물", "idx"),
    ("SPX", "^GSPC", "S&P 500", "idx"), ("IXIC", "^IXIC", "나스닥", "idx"),
    ("KOSPI", "^KS11", "코스피", "idx"), ("KOSDAQ", "^KQ11", "코스닥", "idx"),
    ("N225", "^N225", "니케이", "idx"), ("HSI", "^HSI", "항셍", "idx"),
    ("US10Y", "^TNX", "미 10년", "rate"), ("USDKRW", "KRW=X", "원/달러", "fx"),
    ("DXY", "DX-Y.NYB", "달러인덱스", "fx"), ("WTI", "CL=F", "WTI", "cmd"),
    ("GOLD", "GC=F", "금", "cmd"), ("VIX", "^VIX", "VIX", "vix"),
]
DEFAULT_ON = {"ES", "NQ", "KOSPI", "KOSDAQ", "US10Y", "USDKRW", "WTI", "VIX"}
RULES = {"idx": [1.5, 0.7], "stock": [5.0, 2.5], "fx": [1.0, 0.5], "cmd": [3.0, 1.5], "rate": [10.0, 5.0], "vix": [15.0, 10.0]}
CONTEXT = ["ES", "NQ", "KOSPI", "USDKRW", "US10Y"]     # 알림 아래 '같은 시각' 줄
FRESH = timedelta(minutes=45)
COOL30 = timedelta(minutes=45)


def now():
    return datetime.now(KST)


# ---------------------------------------------------------------- 설정
def _get(url, gh_token, accept):
    req = urllib.request.Request(url, headers={"Accept": accept, "X-GitHub-Api-Version": "2022-11-28",
                                               **({"Authorization": "Bearer " + gh_token} if gh_token else {})})
    return urllib.request.urlopen(req, timeout=30).read()


def load_conf(box, gh_token):
    """→ (설정 dict 또는 None, 상태 문자열)
    contents API 는 암호화된 바이트를 글자로 오인해 UTF-8로 바꿔 보낼 때가 있음 → sha 만 받고 원본은 git blob 으로"""
    try:
        meta = json.loads(_get(CONF_URL + ("&" if "?" in CONF_URL else "?") + "_=" + str(int(time.time())), gh_token, "application/vnd.github+json"))
        raw = _get(CONF_URL.split("/contents/")[0] + "/git/blobs/" + meta["sha"], gh_token, "application/vnd.github.raw")
    except urllib.error.HTTPError as e:
        return None, "없음" if e.code == 404 else f"읽기 실패 {e.code}"
    except Exception as e:
        return None, "읽기 실패 " + type(e).__name__
    try:
        return json.loads(box.open(raw).decode()), "ok"
    except Exception:
        return None, "풀기 실패"


def targets(conf, resolved):
    """→ [(키, 기호, 이름, 묶음)]  (설정이 없으면 기본 대상)"""
    base = (conf or {}).get("base") or {}
    out = [b for b in BASE if base.get(b[0], b[0] in DEFAULT_ON)]
    for w in (conf or {}).get("watch") or []:
        c = str(w.get("c") or "").strip()
        if not c:
            continue
        sym = resolved.get(c) or c
        out.append(("W:" + c, sym, (w.get("n") or "").strip() or c, "stock"))
    return out


def candidates(code):
    """사용자가 적은 코드 → 야후 기호 후보. 6자리 숫자면 코스피(.KS) 먼저, 없으면 코스닥(.KQ)."""
    c = code.strip().upper()
    if c.isdigit() and len(c) == 6:
        return [c + ".KS", c + ".KQ"]
    return [c]


# ---------------------------------------------------------------- 계산
def _session_date(ts, sym):
    """거래일. 선물은 미 동부 18시에 다음 거래일이 시작되므로 6시간 당겨 셈."""
    return (ts + timedelta(hours=6)).date() if sym.endswith("=F") else ts.date()


def move(sym, daily, intra, t=None):
    """daily: [(date, close)], intra: [(tz-aware ts, close)] (거래소 시간대) → dict 또는 None"""
    t = t or now()
    pts = [(ts, v) for ts, v in intra if v is not None and not math.isnan(v)]
    if not pts:
        return None
    last_ts, last = pts[-1]
    if t - last_ts.astimezone(KST) > FRESH:
        return None
    d = _session_date(last_ts, sym)
    prevs = [v for dd, v in daily if dd < d and v is not None and not math.isnan(v)]
    if not prevs:
        return None
    prev = prevs[-1]
    ago = [(ts, v) for ts, v in pts if ts <= last_ts - timedelta(minutes=30)]
    p30 = ago[-1][1] if ago and last_ts - ago[-1][0] < timedelta(minutes=50) else None   # 중간에 끊긴 시세는 제외
    return {"d": d.isoformat(), "last": last, "prev": prev, "p30": p30, "ts": last_ts.astimezone(KST)}


def is_us(sym):
    return not any(c in sym for c in ".^=")


def quote(sym, daily, pts, t=None):
    """내 종목 시세 (프리·정규·애프터 구분). 미국 종목은 거래소(뉴욕) 시각으로 판단.
    → {last, t, sess: pre/reg/post, reg: 정규장 등락 %, ext: 시간외 등락 % 또는 None, stale}"""
    t = t or now()
    pts = [(ts, v) for ts, v in pts if v is not None and not math.isnan(v)]
    closes = [(dd, v) for dd, v in daily if v is not None and not math.isnan(v)]
    if not pts or not closes:
        return None
    ts, last = pts[-1]
    d = _session_date(ts, sym)
    before = [v for dd, v in closes if dd < d]
    today = next((v for dd, v in closes if dd == d), None)
    prev = before[-1] if before else None
    pct = lambda a, b: round((a / b - 1) * 100, 2) if a is not None and b else None   # noqa: E731
    q = {"last": round(last, 4), "t": ts.astimezone(KST).strftime("%m-%d %H:%M"), "stale": t - ts.astimezone(KST) > FRESH}
    if not is_us(sym):
        q.update(sess="reg", reg=pct(last, prev), ext=None)
        return q
    hm = ts.hour * 60 + ts.minute
    if hm < 570:                       # 9:30 전 = 프리장: 지난 정규장 종가 대비
        q.update(sess="pre", reg=pct(prev, before[-2] if len(before) > 1 else None), ext=pct(last, prev))
    elif hm < 960:                     # 정규장
        q.update(sess="reg", reg=pct(last, prev), ext=None)
    else:                              # 16:00 이후 = 애프터: 오늘 정규장 종가 대비
        rc = today
        if rc is None:
            regs = [v for x, v in pts if _session_date(x, sym) == d and 570 <= x.hour * 60 + x.minute < 960]
            rc = regs[-1] if regs else None
        q.update(sess="post", reg=pct(rc, prev), ext=pct(last, rc))
    return q


def chg(m, grp, base):
    """변동 크기: 금리는 bp, 나머지는 %"""
    if base is None or base == 0:
        return None
    if grp == "rate":
        k = 10 if m["last"] > 20 else 100      # 예전 표기(42.5 = 4.25%) 대비
        return (m["last"] - base) * k
    return (m["last"] / base - 1) * 100


def fmt_px(v):
    if v >= 1000:
        return f"{v:,.0f}" if v == int(v) else f"{v:,.1f}"
    if v >= 100:
        return f"{v:,.2f}"
    return f"{v:,.3f}".rstrip("0").rstrip(".") if v < 10 else f"{v:,.2f}"


def fmt_chg(x, grp):
    s = "+" if x > 0 else "−" if x < 0 else ""
    return f"{s}{abs(x):.0f}bp" if grp == "rate" else f"{s}{abs(x):.1f}%"


def check(conf, tg_list, data, st, t=None):
    """tg_list: targets() 결과, data: {기호: move() 결과}, st: 상태(dict, 고쳐 씀) → 보낼 메시지 목록"""
    t = t or now()
    rules = dict(RULES)
    for g, v in ((conf or {}).get("rules") or {}).items():
        try:
            rules[g] = [float(v[0]), float(v[1])]
        except Exception:
            pass
    day_st, cool = st.setdefault("day", {}), st.setdefault("c30", {})
    by_key = {k: (sym, n, g) for k, sym, n, g in [(b[0], b[1], b[2], b[3]) for b in BASE]}
    msgs = []
    for key, sym, name, grp in tg_list:
        m = data.get(sym)
        if not m:
            continue
        th_day, th30 = rules.get(grp, RULES["idx"])
        dc = chg(m, grp, m["prev"])
        lines = []
        if dc is not None and th_day > 0:
            lvl = int(abs(dc) / th_day + 1e-9)
            sk = f"{key}|{m['d']}|{'+' if dc > 0 else '-'}"
            if lvl >= 1 and lvl > day_st.get(sk, 0):
                day_st[sk] = lvl
                lines.append(("📈 " if dc > 0 else "📉 ") + f"{name} {fmt_chg(dc, grp)} · {fmt_px(m['last'])}  (전일 대비"
                             + (f", 기준의 {lvl}배" if lvl >= 2 else "") + ")")
        c30 = chg(m, grp, m["p30"])
        if c30 is not None and th30 > 0 and abs(c30) >= th30:
            ck = f"{key}|{'+' if c30 > 0 else '-'}"
            last = cool.get(ck)
            if not last or t - datetime.fromisoformat(last) >= COOL30:
                cool[ck] = t.isoformat(timespec="minutes")
                txt = ("⚡ " if not lines else "   ") + f"30분 새 {fmt_chg(c30, grp)}" + ("" if lines else f" · {name} {fmt_px(m['last'])}")
                if dc is not None:
                    txt += f"  (오늘 {fmt_chg(dc, grp)})" if not lines else ""
                lines.append(txt)
        if not lines:
            continue
        ctx = []
        for ck in CONTEXT:
            if ck == key or ck not in by_key:
                continue
            s2, n2, g2 = by_key[ck]
            m2 = data.get(s2)
            x = chg(m2, g2, m2["prev"]) if m2 else None
            if x is not None:
                ctx.append(f"{n2} {fmt_chg(x, g2)}")
        msgs.append({"k": key, "text": f"[급변] {m['ts'].strftime('%H:%M')} 기준\n" + "\n".join(lines) + ("\n같은 시각 · " + " · ".join(ctx) if ctx else "")})
    # 오래된 상태 정리
    cutoff = (t - timedelta(days=4)).date().isoformat()
    for k in [k for k in day_st if k.split("|")[1] < cutoff]:
        day_st.pop(k)
    for k in [k for k, v in cool.items() if t - datetime.fromisoformat(v) > timedelta(days=1)]:
        cool.pop(k)
    return msgs


# ---------------------------------------------------------------- 야후
def _col(df, sym):
    try:
        if hasattr(df.columns, "levels"):
            return df[sym]["Close"].dropna() if sym in df.columns.get_level_values(0) else None
        return df["Close"].dropna() if "Close" in df.columns else None
    except Exception:
        return None


def fetch(symbols, daily_cache, t0):
    """→ ({기호: move}, 일봉 캐시, {기호: quote}). 일봉은 30분마다, 2분봉은 매번."""
    import yfinance as yf
    syms = sorted(set(symbols))
    if not syms:
        return {}, daily_cache, {}
    need = [s for s in syms if s not in daily_cache.get("d", {})]
    if t0 - daily_cache.get("at", 0) > 1800 or need:
        df = yf.download(syms, period="10d", interval="1d", group_by="ticker", auto_adjust=False, progress=False, threads=True)
        d = {}
        for s in syms:
            ser = _col(df, s)
            if ser is not None and len(ser):
                d[s] = [(ix.date() if hasattr(ix, "date") else ix, float(v)) for ix, v in ser.items()]
        daily_cache = {"at": t0, "d": d}
    df = yf.download(syms, period="2d", interval="2m", prepost=True, group_by="ticker", auto_adjust=False, progress=False, threads=True)
    out, quotes = {}, {}
    for s in syms:
        ser = _col(df, s)
        if ser is None or not len(ser) or s not in daily_cache["d"]:
            continue
        idx = ser.index if ser.index.tz is not None else ser.index.tz_localize("UTC")
        pts = list(zip(idx.to_pydatetime(), [float(v) for v in ser.values]))
        m = move(s, daily_cache["d"][s], pts)
        if m:
            out[s] = m
        try:
            q = quote(s, daily_cache["d"][s], pts)
            if q:
                quotes[s] = q
        except Exception:
            pass
    return out, daily_cache, quotes


def resolve(codes, resolved):
    """6자리 코드 → .KS / .KQ (일봉이 있는 쪽). 못 찾으면 bad 목록."""
    import yfinance as yf
    todo = [c for c in codes if c not in resolved]
    bad = []
    if not todo:
        return resolved, bad
    cands = sorted({x for c in todo for x in candidates(c)})
    df = yf.download(cands, period="10d", interval="1d", group_by="ticker", auto_adjust=False, progress=False, threads=True)
    for c in todo:
        hit = next((x for x in candidates(c) if (lambda s: s is not None and len(s) > 0)(_col(df, x))), None)
        if hit:
            resolved[c] = hit
        else:
            bad.append(c)
    return resolved, bad
