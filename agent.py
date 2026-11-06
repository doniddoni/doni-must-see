"""
Doni Live — 오라클 서버에서 계속 도는 실시간 시장 수집기.

- 2분봉 시세(선물·지수·금리·환율·원자재, 프리·애프터 포함): 5분마다
- 경제지표 일정·예상·실제치(야후 + 이번 주 Forex Factory): 30분마다, 발표 시각 전후에는 1분마다
- 실제치가 나오면 텔레그램 알림
- 급변 알림: Must See 마켓 '알림 설정'(지수·종목·기준값·알림 봇)을 5분마다 읽어 크게 움직이면 알림 (watch.py)
- 텔레그램 실시간 (tg.py, 이 프로그램이 띄우고 죽으면 다시 띄움): 키워드 즉시 알림 + Must See 새 글 → 수집 작업 실행
- 결과는 Must See 비밀번호로 암호화해 doni-must-see 저장소 live 브랜치에 올림 (기록이 쌓이지 않게 매번 새로 덮어씀)
- 상태(시각·건수·오류만, 내용 없음)는 같은 브랜치 status.json

설정: /etc/doni-live.env  (MUSTSEE_PASSWORD, GH_TOKEN, TG_TOKEN, TG_CHAT)
지표 이름·종목 목록은 bundle_lib.py (telegram-keyword-monitor/mustsee/market_bundle.py 사본)
"""

import base64
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bundle_lib as BL  # noqa: E402
import watch as W  # noqa: E402

VERSION = "2026-11-06.4"
KST = timezone(timedelta(hours=9))
HOME = Path(os.environ.get("DONI_HOME", "/opt/doni-live"))
WORK = HOME / "push"
STATE = HOME / "state.json"
ENV = Path(os.environ.get("DONI_ENV", "/etc/doni-live.env"))
META_URL = os.environ.get("DONI_META_URL", "https://doniddoni.github.io/doni-must-see/data/meta.json")
REPO = "github.com/doniddoni/doni-must-see.git"
FLAG = {"US": "🇺🇸", "KR": "🇰🇷", "CN": "🇨🇳", "JP": "🇯🇵", "EU": "🇪🇺", "UK": "🇬🇧", "DE": "🇩🇪"}


def now():
    return datetime.now(KST)


def log(*a):
    print(now().strftime("%m-%d %H:%M:%S"), *a, flush=True)


def read_env():
    env = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


# ---------------------------------------------------------------- 암호
class Box:
    def __init__(self, password: str):
        meta = json.loads(urllib.request.urlopen(META_URL + "?_=" + str(int(time.time())), timeout=30).read())
        self.salt = meta["salt"]
        key = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(meta["salt"]), int(meta.get("iter", 310000)), 32)
        self.aes = AESGCM(key)
        chk = base64.b64decode(meta["check"])
        if self.aes.decrypt(chk[:12], chk[12:], None) != b"mustsee-ok":
            raise SystemExit("Must See 비밀번호가 맞지 않음")

    def open(self, raw: bytes) -> bytes:
        return self.aes.decrypt(raw[:12], raw[12:], None)

    def seal(self, data: bytes) -> bytes:
        iv = os.urandom(12)
        return iv + self.aes.encrypt(iv, data, None)


# ---------------------------------------------------------------- 텔레그램
CONF = {"c": None}      # Must See '알림 설정' (봇 토큰·채팅이 있으면 그쪽으로 보냄)


def tg(env, text):
    c = (CONF["c"] or {}).get("tg") or {}
    token, chat = (c.get("token"), c.get("chat")) if c.get("token") and c.get("chat") else (env.get("TG_TOKEN"), env.get("TG_CHAT"))
    if not token or not chat:
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20).read()
        return True
    except Exception as e:
        log("텔레그램 실패", type(e).__name__)
        return False


# ---------------------------------------------------------------- 시세
def window_date(t=None):
    return ((t or now()) - timedelta(hours=8)).strftime("%Y-%m-%d")


def intraday():
    import yfinance as yf
    df = yf.download([t[0] for t in BL.INTRA], period="2d", interval="2m", prepost=True,
                     group_by="ticker", auto_adjust=False, progress=False, threads=True)
    wd, s = window_date(), {}
    for sym, key, name, kind in BL.INTRA:
        try:
            ser = df[sym]["Close"].dropna()
        except Exception:
            continue
        idx = ser.index.tz_convert("Asia/Seoul") if ser.index.tz is not None else ser.index.tz_localize("UTC").tz_convert("Asia/Seoul")
        pts = []
        for ts, v in zip(idx, ser.values):
            w = ts - timedelta(hours=8)
            if w.strftime("%Y-%m-%d") == wd:
                pts.append([int(w.hour * 60 + w.minute), round(float(v), 4)])
        if pts:
            s[key] = pts
    return {"date": wd, "s": s, "names": {k: [n, kd] for _, k, n, kd in BL.INTRA}}


# ---------------------------------------------------------------- 경제지표
def econ(ff_cache):
    t = now()
    lo, hi = (t - timedelta(days=1)).strftime("%Y-%m-%d"), (t + timedelta(days=2)).strftime("%Y-%m-%d")
    ya = BL.dedupe_windows([e for e in (BL.yahoo_event(r) for r in BL.fetch_yahoo(lo, hi)) if e])
    evs = {}
    for e in ya:
        evs[(e["date"], e["country"], e["title"])] = e
    # 이번 주 Forex Factory: 예상치 보충 + 야후에 없는 주요국 일정
    for e in ff_cache.get("ev", []):
        if not (lo <= e["date"] <= hi):
            continue
        k = (e["date"], e["country"], e["title"])
        if k in evs:
            if not evs[k].get("forecast") and e.get("forecast"):
                evs[k]["forecast"] = e["forecast"]
            if e["imp"] == "H":
                evs[k]["imp"] = "H"
        else:
            evs[k] = dict(e)
    out = sorted(evs.values(), key=lambda e: (e["date"], e.get("time") or "", e["country"]))
    for e in out:
        e.pop("per", None)
    return out


def ev_time(e):
    if not e.get("time"):
        return None
    return datetime.fromisoformat(f"{e['date']}T{e['time']}:00+09:00")


def num(x):
    m = re.search(r"-?\d+(?:\.\d+)?", str(x or "").replace(",", ""))
    return float(m.group()) if m else None


def alert_text(e):
    a, f, p = e.get("actual"), e.get("forecast"), e.get("previous")
    na, nf = num(a), num(f)
    verdict = ""
    if na is not None and nf is not None:
        verdict = " → 예상 상회" if na > nf else " → 예상 하회" if na < nf else " → 예상 부합"
    bits = [f"실제 {a}"] + ([f"예상 {f}"] if f else []) + ([f"이전 {p}"] if p else [])
    return f"[지표] {FLAG.get(e['country'], '')} {BL.COUNTRY_KO.get(e['country'], e['country'])} {e['title']} ({e.get('time') or ''})\n" + " · ".join(bits) + verdict


# ---------------------------------------------------------------- 올리기
def git(*a, check=True):
    return subprocess.run(["git", "-C", str(WORK), *a], check=check, capture_output=True, text=True)


def push(env, box, live, status):
    WORK.mkdir(parents=True, exist_ok=True)
    if not (WORK / ".git").exists():
        subprocess.run(["git", "init", "-q", str(WORK)], check=True)
        git("config", "user.name", "doni-live")
        git("config", "user.email", "doni-live@users.noreply.github.com")
    url = os.environ.get("DONI_PUSH_URL") or f"https://x-access-token:{env['GH_TOKEN']}@{REPO}"
    (WORK / "live.bin").write_bytes(box.seal(gzip.compress(json.dumps(live, ensure_ascii=False, separators=(",", ":")).encode(), mtime=0)))
    (WORK / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    (WORK / "README.md").write_text("Doni Live 실시간 데이터 (암호화). 매번 새로 덮어씀.\n", encoding="utf-8")
    git("symbolic-ref", "HEAD", "refs/heads/live")
    git("update-ref", "-d", "refs/heads/live", check=False)      # 기록 없이 새로
    git("add", "-A")
    git("commit", "-q", "-m", "live " + status["at"])
    r = subprocess.run(["git", "-C", str(WORK), "push", "-q", "-f", url, "live"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("push 실패: " + r.stderr.replace(env["GH_TOKEN"], "***")[-300:])


def tg_status():
    """tg.py 상태 (건수·시각만)"""
    try:
        x = json.loads((HOME / "tg_status.json").read_text())
        return {k: x.get(k) for k in ("state", "at", "kw_sent", "kw_chats", "ms_events", "ms_runs", "ms_last", "ms_code", "err")}
    except Exception:
        return {"state": "없음"}


# ---------------------------------------------------------------- 본체
def main():
    env = read_env()
    box = Box(env["MUSTSEE_PASSWORD"])
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    announced = set(st.get("announced", []))
    alerts = st.get("alerts", [])
    wst = st.get("watch", {})                     # 급변 알림 상태 (보낸 단계·30분 쿨다운·종목 코드 해석)
    wst.setdefault("resolved", {})
    winfo, daily = {"conf": "아직", "n": 0, "bad": []}, {}
    log("시작", VERSION)
    tg(env, "[Doni Live] 실시간 수집을 시작했어요 (" + VERSION + ")") if not st else None

    intra, events, ff = {}, [], {"ev": [], "at": 0}
    t_intra = t_econ = t_push = t_gc = t_conf = t_watch = t_tg = 0.0
    tgp = None
    last_err, errs = "", 0
    while True:
        t0 = time.time()
        changed = False
        # 텔레그램 실시간 프로그램 지키기
        if (tgp is None or tgp.poll() is not None) and t0 - t_tg > 60 and not os.environ.get("DONI_NO_TG"):
            if tgp is not None:
                log("tg.py 종료됨 → 다시 시작", tgp.returncode)
            tgp = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve().parent / "tg.py")])
            t_tg = t0
        try:
            # Forex Factory 이번 주 (3시간마다)
            if t0 - ff["at"] > 3 * 3600:
                try:
                    ff = {"ev": [e for e in (BL.to_event(r) for r in BL.fetch_ff()) if e], "at": t0}
                except Exception as e:
                    log("FF 실패", e)
                    ff["at"] = t0 - 2 * 3600
            # 지표 발표가 가까우면 1분마다, 아니면 30분마다
            hot = False
            n = now()
            for e in events:
                et = ev_time(e)
                if et and e["imp"] in ("H", "M") and not e.get("actual") and et - timedelta(minutes=2) <= n <= et + timedelta(minutes=45):
                    hot = True
                    break
            if t0 - t_econ > (60 if hot else 1800):
                events = econ(ff)
                t_econ = t0
                changed = True
                for e in events:
                    k = f"{e['date']}|{e['country']}|{e['title']}"
                    et = ev_time(e)
                    if e.get("actual") and k not in announced:
                        announced.add(k)
                        if et and n - et < timedelta(hours=3) and e["imp"] in ("H", "M"):   # 오래된 것은 알림 없이 기록만
                            msg = alert_text(e)
                            if (CONF["c"] or {}).get("macro", True):
                                tg(env, msg)
                            alerts = (alerts + [{"at": n.strftime("%m-%d %H:%M"), "k": k, "type": "macro", "text": msg}])[-40:]
                            log("알림", msg.splitlines()[0])
            # 알림 설정 5분마다 읽기
            if t0 - t_conf > 290:
                c, cs = W.load_conf(box, env.get("GH_TOKEN"))
                t_conf = t0
                if c is not None or cs == "없음":
                    CONF["c"] = c
                winfo.update(conf=cs, read=now().strftime("%m-%d %H:%M"), updated=(c or {}).get("updated", ""),
                             bot=((c or {}).get("tg") or {}).get("bot", ""))
                codes = [str(w.get("c") or "").strip() for w in (CONF["c"] or {}).get("watch") or [] if str(w.get("c") or "").strip()]
                try:
                    wst["resolved"], bad = W.resolve(codes, wst["resolved"])
                    winfo["bad"] = bad
                except Exception as e:
                    log("종목 코드 확인 실패", e)
                changed = True
            # 급변 판단 5분마다 (설정에서 끄면 쉼)
            if t0 - t_watch > 290 and (CONF["c"] or {}).get("on", True):
                tl = W.targets(CONF["c"], wst["resolved"])
                ctx = [b[1] for b in W.BASE if b[0] in W.CONTEXT]
                data, daily = W.fetch([x[1] for x in tl] + ctx, daily, t0)
                t_watch = t0
                winfo["n"], winfo["live"] = len(tl), sum(1 for x in tl if x[1] in data)
                for m in W.check(CONF["c"], tl, data, wst):
                    sent = tg(env, m["text"])
                    alerts = (alerts + [{"at": now().strftime("%m-%d %H:%M"), "k": m["k"], "type": "move", "text": m["text"], "sent": sent}])[-40:]
                    log("급변", m["text"].replace("\n", " / ")[:120])
                    changed = True
            # 시세 5분마다
            if t0 - t_intra > 290:
                intra = intraday()
                t_intra = t0
                changed = True
            if changed and t0 - t_push > 50:
                live = {"v": 1, "at": now().strftime("%Y-%m-%d %H:%M"), "intra": intra, "macro": events, "alerts": alerts, "watch": winfo,
                        "tg": tg_status()}
                status = {"at": live["at"], "version": VERSION, "intra": {k: len(v) for k, v in intra.get("s", {}).items()},
                          "intra_date": intra.get("date"), "events": len(events), "hot": hot,
                          "actuals": sum(1 for e in events if e.get("actual")), "error": last_err,
                          "alert_conf": winfo.get("conf"), "watching": winfo.get("n", 0), "tg": tg_status()}
                push(env, box, live, status)
                t_push = t0
                STATE.write_text(json.dumps({"announced": sorted(announced)[-500:], "alerts": alerts, "watch": wst}, ensure_ascii=False))
                last_err, errs = "", 0
            if t0 - t_gc > 86400:
                git("gc", "-q", "--prune=now", check=False) if (WORK / ".git").exists() else None
                t_gc = t0
        except Exception as e:
            errs += 1
            last_err = f"{now().strftime('%m-%d %H:%M')} {type(e).__name__}: {str(e)[:200]}"
            log("오류", last_err)
            traceback.print_exc()
            if errs == 10:
                tg(env, "[Doni Live] 10번 연속 오류: " + last_err)
        time.sleep(max(5, 60 - (time.time() - t0)))


if __name__ == "__main__":
    main()
