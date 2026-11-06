"""
Doni Live — 텔레그램 실시간 (agent.py 가 이 프로그램을 띄우고, 죽으면 다시 띄움)

1) 키워드 알림: 감시 방에 새 메시지가 올라오면 바로 키워드 확인 → 엔미레 뉴스 봇으로 알림
   (GitHub telegram_keyword_monitor.py 와 같은 규칙·모양. 놓친 업데이트는 3분마다 이어 읽기로 메움)
2) Must See: 채널에 새 글·수정·삭제가 생기면 1분 기다렸다가(연속 글 묶기) GitHub 'Must See 모아보기' 작업 실행

텔레그램 로그인은 이 서버 전용 (GitHub 의 TG_SESSION 과 따로 — 같은 로그인을 두 곳에서 동시에 쓰면 텔레그램이 끊음).
처음엔 QR 로그인: 새 알림 봇(Must See 마켓 '알림'에서 연결한 봇)으로 QR 사진을 보냄 → 폰 텔레그램 → 설정 → 기기 → 데스크톱 기기 연결로 스캔.
2단계 인증 비밀번호가 있으면 같은 봇 대화에 보내면 됨 (받자마자 지움).

설정: data/srv.bin (Must See 사이트, Must See 비밀번호로 암호화 — GitHub Actions 가 만듦)
  {api_id, api_hash, bot_token, bot_chat, kw: config.json, kw_state: state.json, ms_chat}
"""

import asyncio
import html
import io
import json
import os
import re
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from telethon import TelegramClient, events, utils
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.types import Channel, PeerChannel

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch as W  # noqa: E402  (알림 봇 설정 읽기)

KST = timezone(timedelta(hours=9))
HOME = Path(os.environ.get("DONI_HOME", "/opt/doni-live"))
ENV = Path(os.environ.get("DONI_ENV", "/etc/doni-live.env"))
META_URL = os.environ.get("DONI_META_URL", "https://doniddoni.github.io/doni-must-see/data/meta.json")
SESSION = HOME / "tg.session"
KW_STATE = HOME / "kw_state.json"
STATUS = HOME / "tg_status.json"
SITE = os.environ.get("DONI_SITE", "https://doniddoni.github.io/doni-must-see/")
SNIPPET_LEN = 700
POLL_SEC = 60           # 놓친 메시지 이어 읽기 간격 (텔레그램이 채널 업데이트를 안 보내 줄 때가 있어 직접 확인)
MS_QUIET = 60           # Must See: 마지막 글 뒤 이만큼 조용하면 실행
MS_GAP = 300            # Must See: 실행 사이 최소 간격

ST = {"state": "시작", "at": "", "kw_sent": 0, "kw_last": "", "kw_chats": 0, "ms_events": 0, "ms_runs": 0,
      "ms_last": "", "ms_code": None, "err": ""}


def now():
    return datetime.now(KST)


def read_env():
    env = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


class Box:
    """Must See 비밀번호 열쇠 (agent.py 와 같음 — pandas 등을 불러오지 않으려고 따로 둠)"""

    def __init__(self, password):
        import base64
        import hashlib
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        meta = json.loads(urllib.request.urlopen(META_URL + "?_=" + str(int(time.time())), timeout=30).read())
        key = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(meta["salt"]), int(meta.get("iter", 310000)), 32)
        self.aes = AESGCM(key)
        chk = base64.b64decode(meta["check"])
        if self.aes.decrypt(chk[:12], chk[12:], None) != b"mustsee-ok":
            raise SystemExit("Must See 비밀번호가 맞지 않음")

    def open(self, raw):
        return self.aes.decrypt(raw[:12], raw[12:], None)


def log(*a):
    print(now().strftime("%m-%d %H:%M:%S"), "[tg]", *a, flush=True)


def save_status(**kw):
    ST.update(kw)
    ST["at"] = now().strftime("%Y-%m-%d %H:%M")
    try:
        STATUS.write_text(json.dumps(ST, ensure_ascii=False))
    except Exception:
        pass


def fetch_bin(box, rel):
    raw = urllib.request.urlopen(SITE + rel + "?_=" + str(int(time.time())), timeout=30).read()
    return json.loads(box.open(raw).decode())


# ---------------------------------------------------------------- 키워드 규칙 (telegram_keyword_monitor.py 와 같음)
def _kw_pattern(k):
    esc = re.escape(k)
    if k.isdigit():
        return re.compile(rf"(?<!\d){esc}(?!\d)")
    if k.isascii():
        return re.compile(rf"(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(esc, re.IGNORECASE)


def build_patterns(keywords):
    return [(k, _kw_pattern(k)) for k in keywords]


def strip_ignored(text, ignore):
    for ph in ignore:
        text = re.sub(re.escape(ph), lambda m: " " * len(m.group(0)), text, flags=re.IGNORECASE)
    return text


def find_keywords(text, patterns):
    return [k for k, p in patterns if p.search(text)]


def hit_spans(text, patterns, hits):
    spans = []
    for k, p in patterns:
        if k in hits:
            spans += [m.span() for m in p.finditer(text)]
    spans.sort()
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def make_snippet(text, clean, patterns, hits, maxlen=SNIPPET_LEN):
    if len(text) <= maxlen:
        return text, clean, ""
    spans = hit_spans(clean, patterns, hits)
    pos = spans[0][0] if spans else 0
    start = max(0, pos - maxlen // 3)
    end = min(len(text), start + maxlen)
    start = max(0, end - maxlen)
    return text[start:end], clean[start:end], ("…" if start > 0 else "", "…" if end < len(text) else "")


def highlight(text, clean, patterns, hits):
    out, last = [], 0
    for s, e in hit_spans(clean, patterns, hits):
        out.append(html.escape(text[last:s]))
        out.append(f"<b>{html.escape(text[s:e])}</b>")
        last = e
    out.append(html.escape(text[last:]))
    return "".join(out)


def message_link(entity, msg_id):
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    if isinstance(entity, Channel):
        return f"https://t.me/c/{entity.id}/{msg_id}"
    return None


def alert_html(a, patterns):
    t, c, pp = make_snippet(a["text"], a["clean"], patterns, a["hits"])
    body = highlight(t, c, patterns, a["hits"])
    if pp:
        body = pp[0] + body + pp[1]
    header = f"🔔 <b>{html.escape(', '.join(a['hits']))}</b>\n"
    meta = f"📢 {html.escape(a['chat'])}"
    if a["sender"]:
        meta += f" · {html.escape(a['sender'])}"
    meta += f"\n🕒 {a['date']}\n\n"
    footer = f'\n\n🔗 <a href="{a["link"]}">원문 보기</a>' if a["link"] else ""
    return header + meta + body + footer


# ---------------------------------------------------------------- 봇
def bot_call(token, method, data=None, files=None, timeout=30):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=timeout)
    j = r.json()
    if not j.get("ok"):
        if j.get("error_code") == 429:
            time.sleep(j.get("parameters", {}).get("retry_after", 5) + 1)
            return bot_call(token, method, data, files, timeout)
        raise RuntimeError(f"봇 {method} 실패: {j.get('error_code')} {str(j.get('description'))[:200]}")
    return j["result"]


def send_html(token, chat, text):
    for i in range(3):
        try:
            return bot_call(token, "sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                                   "disable_web_page_preview": "true"})
        except Exception:
            if i == 2:
                raise
            time.sleep(3)


def qr_png(url):
    import qrcode
    img = qrcode.make(url, box_size=8, border=3)
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


# ---------------------------------------------------------------- 로그인 (QR)
class Login:
    """알림 봇으로 QR 사진을 보내고 25초마다 새 QR 로 바꿈. 2단계 비밀번호는 봇 대화로 받음."""

    def __init__(self, env, box):
        self.env, self.box = env, box

    def bot(self):
        c, _ = W.load_conf(self.box, self.env.get("GH_TOKEN"))
        t = (c or {}).get("tg") or {}
        if t.get("token") and t.get("chat"):
            return t["token"], t["chat"]
        if self.env.get("TG_TOKEN") and self.env.get("TG_CHAT"):
            return self.env["TG_TOKEN"], self.env["TG_CHAT"]
        return None, None

    async def ask_password(self, token, chat):
        upd = await asyncio.to_thread(bot_call, token, "getUpdates", {"timeout": 0, "offset": -1})
        offset = (upd[-1]["update_id"] + 1) if upd else 0
        await asyncio.to_thread(send_html, token, chat,
                                "🔐 <b>2단계 인증 비밀번호</b>가 필요해요.\n이 대화에 비밀번호만 보내 주세요. 받자마자 지울게요. (10분 안에)")
        end = time.time() + 600
        while time.time() < end:
            upd = await asyncio.to_thread(bot_call, token, "getUpdates", {"timeout": 25, "offset": offset}, None, 40)
            for u in upd:
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                if str((m.get("chat") or {}).get("id")) == str(chat) and m.get("text"):
                    try:
                        await asyncio.to_thread(bot_call, token, "deleteMessage", {"chat_id": chat, "message_id": m["message_id"]})
                    except Exception:
                        pass
                    return m["text"].strip()
        return None

    async def run(self, client):
        token, chat = self.bot()
        if not token:
            save_status(state="로그인 대기 — 알림 봇 없음",
                        err="Must See 마켓 '알림'에서 봇을 연결하면 QR 을 보내요")
            return False
        save_status(state="로그인 대기 — QR 스캔", err="")
        qr = await client.qr_login()
        cap = ("📱 <b>Doni Live 텔레그램 연결</b>\n폰 텔레그램 → 설정 → 기기 → <b>데스크톱 기기 연결</b> → 이 QR 스캔\n"
               "(25초마다 새 QR 로 바뀌어요. 15분 동안 유효)")
        msg = await asyncio.to_thread(bot_call, token, "sendPhoto", {"chat_id": chat, "caption": cap, "parse_mode": "HTML"},
                                      {"photo": ("qr.png", qr_png(qr.url), "image/png")})
        mid = msg["message_id"]
        end = time.time() + 15 * 60
        while time.time() < end:
            try:
                await qr.wait(timeout=25)
                break
            except asyncio.TimeoutError:
                await qr.recreate()
                try:
                    media = json.dumps({"type": "photo", "media": "attach://qr", "caption": cap, "parse_mode": "HTML"})
                    await asyncio.to_thread(bot_call, token, "editMessageMedia", {"chat_id": chat, "message_id": mid, "media": media},
                                            {"qr": ("qr.png", qr_png(qr.url), "image/png")})
                except Exception as e:
                    log("QR 바꾸기 실패", e)
            except SessionPasswordNeededError:
                save_status(state="로그인 대기 — 2단계 비밀번호")
                for _ in range(3):
                    pw = await self.ask_password(token, chat)
                    if not pw:
                        break
                    try:
                        await client.sign_in(password=pw)
                        break
                    except PasswordHashInvalidError:
                        await asyncio.to_thread(send_html, token, chat, "비밀번호가 맞지 않아요. 다시 보내 주세요.")
                break
        else:
            await asyncio.to_thread(bot_call, token, "deleteMessage", {"chat_id": chat, "message_id": mid})
            return False
        if not await client.is_user_authorized():
            return False
        try:
            await asyncio.to_thread(bot_call, token, "deleteMessage", {"chat_id": chat, "message_id": mid})
        except Exception:
            pass
        me = await client.get_me()
        await asyncio.to_thread(send_html, token, chat, f"✅ 텔레그램 연결 완료 ({html.escape(utils.get_display_name(me))}). 키워드 알림·Must See 실시간을 시작해요.")
        return True


# ---------------------------------------------------------------- 본체
class Live:
    def __init__(self, env, box, srv, client):
        self.env, self.box, self.srv, self.client = env, box, srv, client
        self.lock = asyncio.Lock()
        self.kw_state = json.loads(KW_STATE.read_text()) if KW_STATE.exists() else {}
        self.ents = {}
        self.ms_ent = None
        self.ms_pending, self.ms_last_ev, self.ms_last_run = False, 0.0, 0.0
        self.ms_sig = None
        self.apply_conf(srv)

    def apply_conf(self, srv):
        self.srv = srv
        kw = srv.get("kw") or {}
        self.keywords = list(dict.fromkeys(k.strip() for k in kw.get("keywords", []) if k.strip()))
        self.patterns = build_patterns(self.keywords)
        self.ex = build_patterns([k.strip() for k in kw.get("exclude_keywords", []) if k.strip()])
        self.ignore = sorted({k.strip() for k in kw.get("ignore_phrases", []) if k.strip()}, key=len, reverse=True)
        # GitHub 쪽 마지막 위치를 넘겨받음 (넘겨받은 뒤엔 서버 기록이 앞섬)
        for key, v in (srv.get("kw_state") or {}).items():
            if v.get("last_id") and v["last_id"] > self.kw_state.get(key, {}).get("last_id", 0):
                self.kw_state[key] = {"title": v.get("title", ""), "last_id": v["last_id"]}

    async def resolve(self):
        dialogs = await self.client.get_dialogs()
        by_id = {d.id: d.entity for d in dialogs}
        by_title = {d.name: d.entity for d in dialogs}
        ents = {}
        for t in (self.srv.get("kw") or {}).get("chats", []):
            e = None
            if isinstance(t, int) or (isinstance(t, str) and t.lstrip("-").isdigit()):
                e = by_id.get(int(t))
            elif t.startswith("@") or "t.me/" in t:
                try:
                    e = await self.client.get_entity(t)
                except Exception as x:
                    log("방 찾기 실패", t, x)
            else:
                e = by_title.get(t)
            if e is not None:
                ents[utils.get_peer_id(e)] = e
            else:
                log("방을 찾지 못함", t)
        self.ents = ents
        ms = self.srv.get("ms_chat")
        if ms:
            self.ms_ent = by_id.get(int(ms))
            if self.ms_ent is None:
                try:
                    s = str(abs(int(ms)))
                    self.ms_ent = await self.client.get_entity(PeerChannel(int(s[3:]) if s.startswith("100") else int(s)))
                except Exception as x:
                    log("Must See 채널 찾기 실패", x)
        save_status(kw_chats=len(ents))
        log(f"감시 방 {len(ents)}곳 · Must See {'연결' if self.ms_ent else '없음'}")

    def save_kw(self):
        KW_STATE.write_text(json.dumps(self.kw_state, ensure_ascii=False))

    async def handle(self, ent, msgs):
        """메시지 목록(오래된 순) → 키워드 알림. 이미 본 번호는 건너뜀."""
        key = str(utils.get_peer_id(ent))
        title = utils.get_display_name(ent)
        async with self.lock:
            last = self.kw_state.get(key, {}).get("last_id", 0)
            for m in msgs:
                if m.id <= last:
                    continue
                text = m.message or ""
                if text:
                    clean = strip_ignored(text, self.ignore)
                    hits = find_keywords(clean, self.patterns)
                    if hits and not find_keywords(text, self.ex):
                        sender = ""
                        try:
                            s = await m.get_sender()
                            if s is not None and utils.get_display_name(s) != title:
                                sender = utils.get_display_name(s)
                        except Exception:
                            pass
                        a = {"chat": title, "sender": sender, "date": m.date.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
                             "hits": hits, "text": text, "clean": clean, "link": message_link(ent, m.id)}
                        await asyncio.to_thread(send_html, self.srv["bot_token"], self.srv["bot_chat"], alert_html(a, self.patterns))
                        ST["kw_sent"] += 1
                        save_status(kw_last=now().strftime("%m-%d %H:%M") + " " + ", ".join(hits))
                        log("키워드", title, hits)
                        await asyncio.sleep(1.1)
                last = m.id
                self.kw_state[key] = {"title": title, "last_id": last}
                self.save_kw()

    async def catch_up(self):
        for pid, ent in self.ents.items():
            key = str(pid)
            last = self.kw_state.get(key, {}).get("last_id")
            if last is None:   # 처음 보는 방: 지금부터
                latest = await self.client.get_messages(ent, limit=1)
                self.kw_state[key] = {"title": utils.get_display_name(ent), "last_id": latest[0].id if latest else 0}
                self.save_kw()
                continue
            msgs = [m async for m in self.client.iter_messages(ent, min_id=last, reverse=True, limit=1000)]
            if msgs:
                await self.handle(ent, msgs)

    async def ms_check(self):
        """Must See 최근 30개 글의 (번호, 수정 시각) → 바뀌었으면 새 글·수정·삭제로 봄"""
        if self.ms_ent is None:
            return
        msgs = await self.client.get_messages(self.ms_ent, limit=30)
        sig = [(m.id, int(m.edit_date.timestamp()) if m.edit_date else 0) for m in msgs]
        if self.ms_sig is not None and sig != self.ms_sig:
            self.ms_pending, self.ms_last_ev = True, time.time()
            ST["ms_events"] += 1
            log("Must See 변화 감지 (확인)")
        self.ms_sig = sig

    def run_must_see(self):
        sync = fetch_bin(self.box, "data/sync.bin")
        if not sync.get("run"):
            raise RuntimeError("실행 열쇠 없음 (sync.bin)")
        r = requests.post(f"https://api.github.com/repos/{sync['run_repo']}/actions/workflows/{sync.get('run_wf', 'mustsee.yml')}/dispatches",
                          headers={"Authorization": "Bearer " + sync["run"], "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28"}, json={"ref": "main"}, timeout=30)
        return r.status_code

    async def loop(self):
        t_poll = t_conf = time.time()
        while True:
            await asyncio.sleep(10)
            t = time.time()
            # Must See: 조용해지면 실행
            if self.ms_pending and t - self.ms_last_ev >= MS_QUIET and t - self.ms_last_run >= MS_GAP:
                self.ms_pending = False
                self.ms_last_run = t
                try:
                    code = await asyncio.to_thread(self.run_must_see)
                    ST["ms_runs"] += 1
                    save_status(ms_last=now().strftime("%m-%d %H:%M"), ms_code=code)
                    log("Must See 실행", code)
                    if code != 204:
                        self.ms_pending, self.ms_last_run = True, t + 600   # 10분 뒤 다시
                except Exception as e:
                    save_status(ms_code=str(e)[:120])
                    log("Must See 실행 실패", e)
                    self.ms_pending, self.ms_last_run = True, t + 600
            if t - t_poll >= POLL_SEC:
                t_poll = t
                try:
                    await self.catch_up()
                except Exception as e:
                    log("이어 읽기 실패", e)
                try:
                    await self.ms_check()
                except Exception as e:
                    log("Must See 확인 실패", e)
            if t - t_conf >= 1800:      # 키워드 설정 (srv.bin) 30분마다
                t_conf = t
                try:
                    srv = await asyncio.to_thread(fetch_bin, self.box, "data/srv.bin")
                    chats_changed = (srv.get("kw") or {}).get("chats") != (self.srv.get("kw") or {}).get("chats")
                    self.apply_conf(srv)
                    if chats_changed:
                        await self.resolve()
                        self.register()
                except Exception as e:
                    log("설정 새로 읽기 실패", e)
            save_status(state="연결됨")

    def register(self):
        for h in getattr(self, "_handlers", []):
            self.client.remove_event_handler(h)
        self._handlers = []

        async def on_kw(ev):
            ent = self.ents.get(ev.chat_id)
            if ent is not None:
                try:
                    await self.handle(ent, [ev.message])
                except Exception as e:
                    log("키워드 처리 실패", e)
        if self.ents:
            self.client.add_event_handler(on_kw, events.NewMessage(chats=list(self.ents.values())))
            self._handlers.append(on_kw)

        async def on_ms(ev):
            self.ms_pending, self.ms_last_ev = True, time.time()
            ST["ms_events"] += 1
            self.ms_sig = None      # 다음 확인 때 기준을 새로 잡음 (같은 변화로 두 번 실행하지 않게)
        if self.ms_ent is not None:
            for kind in (events.NewMessage, events.MessageEdited):
                self.client.add_event_handler(on_ms, kind(chats=[self.ms_ent]))
            self.client.add_event_handler(on_ms, events.MessageDeleted(chats=[self.ms_ent]))
            self._handlers.append(on_ms)


async def main():
    env = read_env()
    box = Box(env["MUSTSEE_PASSWORD"])
    while True:
        try:
            srv = await asyncio.to_thread(fetch_bin, box, "data/srv.bin")
            break
        except Exception as e:
            save_status(state="설정 대기", err="srv.bin: " + str(e)[:150])
            log("srv.bin 대기", e)
            await asyncio.sleep(600)
    sess = SESSION.read_text().strip() if SESSION.exists() else ""
    client = TelegramClient(StringSession(sess), int(srv["api_id"]), srv["api_hash"],
                            device_model="Doni Live (Oracle)", system_version="Ubuntu", app_version="1.0")
    await client.connect()
    while not await client.is_user_authorized():
        ok = await Login(env, box).run(client)
        if ok:
            break
        await asyncio.sleep(3600 if ST["state"].startswith("로그인 대기 — QR") else 600)
    SESSION.write_text(client.session.save())
    os.chmod(SESSION, 0o600)
    live = Live(env, box, srv, client)
    await live.resolve()
    live.register()
    save_status(state="연결됨", err="")
    await live.catch_up()
    await live.ms_check()
    log("실시간 시작")
    await asyncio.gather(client.run_until_disconnected(), live.loop())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        traceback.print_exc()
        save_status(state="오류", err=f"{type(e).__name__}: {str(e)[:200]}")
        sys.exit(1)
