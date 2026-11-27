import os, sqlite3, threading, time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
import requests, uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

DB_PATH = Path(__file__).resolve().parent / "scribe_users.db"
FREE_DAILY_LIMIT, REFERRAL_BONUS = 5, 10
PACKS = {
    "pack_100": {"title": "100 Audio Credits", "description": "100 fast voice transcriptions, summaries & translations (never expire).", "stars": 50, "credits": 100, "pro_days": 0},
    "pro_30d": {"title": "Pro Unlimited (30 Days)", "description": "Unlimited voice-to-text, AI summaries & translations for 30 days.", "stars": 150, "credits": 0, "pro_days": 30},
}
SESSION = requests.Session()

def api(t, m): return f"https://api.telegram.org/bot{t}/{m}"

def init_db():
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, free_date TEXT, free_used INTEGER DEFAULT 0, paid_credits INTEGER DEFAULT 0, pro_until TEXT, referred_by INTEGER, referrals INTEGER DEFAULT 0, total_uses INTEGER DEFAULT 0, stars_spent INTEGER DEFAULT 0, lang TEXT DEFAULT 'uz', created_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS transcripts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, created_at TEXT)")
    c.commit()
    return c

def send_msg(t, cid, txt, rm=None):
    p = {"chat_id": cid, "text": txt[:4000], "parse_mode": "Markdown"}
    if rm: p["reply_markup"] = rm
    r = SESSION.post(api(t, "sendMessage"), json=p, timeout=20).json()
    if not r.get("ok"):
        p.pop("parse_mode", None)
        SESSION.post(api(t, "sendMessage"), json=p, timeout=20)

def buy_kb():
    return {"inline_keyboard": [[{"text": "Buy 100 Credits - 50 Stars", "callback_data": "buy:pack_100"}], [{"text": "Pro Unlimited (30d) - 150 Stars", "callback_data": "buy:pro_30d"}]]}

def lang_kb():
    return {"inline_keyboard": [[{"text": "O'zbek (UZ)", "callback_data": "lang:uz"}, {"text": "Russian (RU)", "callback_data": "lang:ru"}], [{"text": "English (EN)", "callback_data": "lang:en"}, {"text": "Auto", "callback_data": "lang:auto"}]]}

def transcript_kb(tid, bot_u, uid, include_share):
    rows = [[{"text": "Summarize", "callback_data": f"act:sum:{tid}"}, {"text": "-> EN", "callback_data": f"act:en:{tid}"}, {"text": "-> RU", "callback_data": f"act:ru:{tid}"}, {"text": "-> UZ", "callback_data": f"act:uz:{tid}"}]]
    if include_share:
        link = f"https://t.me/{bot_u}?start=ref_{uid}"
        txt = "Ovozli xabarlarni 1 soniyada matnga aylantiruvchi + tarjima qiluvchi AI bot!"
        rows.append([{"text": "Share with a Friend (+10 Free Credits)", "url": f"https://t.me/share/url?url={quote(link)}&text={quote(txt)}"}])
    return {"inline_keyboard": rows}

def groq_llm(gk: str, sp: str, txt: str) -> str:
    r = SESSION.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {gk}", "Content-Type": "application/json"}, json={"model": "openai/gpt-oss-20b", "reasoning_effort": "low", "temperature": 0.2, "max_completion_tokens": 1024, "messages": [{"role": "system", "content": sp}, {"role": "user", "content": txt[:6000]}]}, timeout=25)
    if r.ok:
        return (r.json()["choices"][0]["message"].get("content") or "").strip()
    print(f"[render] groq_llm {r.status_code}: {r.text[:200]}", flush=True)
    return ""

def groq_transcribe(gk: str, ab: bytes, fname: str, lang: str = "uz") -> str:
    ext = Path(fname).suffix.lower()
    stem = Path(fname).stem or "voice"
    mmap = {".oga": (".ogg", "audio/ogg"), ".ogg": (".ogg", "audio/ogg"), ".opus": (".ogg", "audio/ogg"), ".mp3": (".mp3", "audio/mpeg"), ".m4a": (".m4a", "audio/mp4"), ".mp4": (".mp4", "video/mp4"), ".wav": (".wav", "audio/wav"), ".flac": (".flac", "audio/flac"), ".webm": (".webm", "audio/webm")}
    next_ext, mime = mmap.get(ext, (".ogg", "audio/ogg"))
    safe_name = f"{stem}{next_ext}"
    d = {"model": "whisper-large-v3", "response_format": "json"}
    if lang in ("uz", "ru", "en"): d["language"] = lang
    if lang == "uz": d["prompt"] = "O'zbek tilidagi toza lotin yozuvi: o', g', sh, ch. Assalomu alaykum."
    r = SESSION.post("https://api.groq.com/openai/v1/audio/transcriptions", headers={"Authorization": f"Bearer {gk}"}, data=d, files={"file": (safe_name, ab, mime)}, timeout=60)
    if not r.ok:
        print(f"[render] groq_transcribe {r.status_code} ({safe_name}): {r.text[:200]}", flush=True)
        r.raise_for_status()
    raw = (r.json().get("text") or "").strip()
    if lang == "uz" and len(raw) >= 4:
        try:
            c = groq_llm(gk, "Fix ONLY Uzbek Latin speech-to-text spelling and punctuation (o', g', sh, ch). Output ONLY the cleaned Uzbek text.", raw)
            if c: return c
        except Exception: pass
    return raw

def bot_loop():
    t = os.environ.get("SCRIBE_BOT_TOKEN", "").strip()
    gk = os.environ.get("GROQ_API_KEY", "").strip()
    oid = int(os.environ.get("OWNER_CHAT_ID", "0") or "0")
    if not t or not gk:
        print("[render] Missing SCRIBE_BOT_TOKEN or GROQ_API_KEY", flush=True)
        return
    conn = init_db()
    bu = SESSION.get(api(t, "getMe"), timeout=15).json()["result"]["username"]
    print(f"[render] @{bu} 24/7 cloud bot live (Summarize + Translate enabled)!", flush=True)
    offset = 0
    while True:
        try:
            r = SESSION.get(api(t, "getUpdates"), params={"timeout": 45, "offset": offset}, timeout=55).json()
        except Exception:
            time.sleep(3); continue
        if not r.get("ok"):
            time.sleep(3); continue
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            if upd.get("pre_checkout_query"):
                SESSION.post(api(t, "answerPreCheckoutQuery"), json={"pre_checkout_query_id": upd["pre_checkout_query"]["id"], "ok": True}, timeout=15)
                continue
            cb = upd.get("callback_query")
            if cb:
                data, cid = cb.get("data", ""), cb["message"]["chat"]["id"]
                if data.startswith("buy:") and data[4:] in PACKS:
                    SESSION.post(api(t, "answerCallbackQuery"), json={"callback_query_id": cb["id"]}, timeout=10)
                    p = PACKS[data[4:]]
                    SESSION.post(api(t, "sendInvoice"), json={"chat_id": cid, "title": p["title"], "description": p["description"], "payload": data[4:], "provider_token": "", "currency": "XTR", "prices": [{"label": p["title"], "amount": p["stars"]}]}, timeout=20)
                elif data.startswith("lang:") and data[5:] in ("uz", "ru", "en", "auto"):
                    SESSION.post(api(t, "answerCallbackQuery"), json={"callback_query_id": cb["id"]}, timeout=10)
                    conn.execute("UPDATE users SET lang = ? WHERE user_id = ?", (data[5:], cb["from"]["id"]))
                    conn.commit()
                    send_msg(t, cid, f"Language updated to `{data[5:].upper()}`.")
                elif data.startswith("act:"):
                    parts = data.split(":")
                    if len(parts) == 3 and parts[2].isdigit():
                        act, tid = parts[1], int(parts[2])
                        SESSION.post(api(t, "answerCallbackQuery"), json={"callback_query_id": cb["id"], "text": "Processing..."}, timeout=10)
                        rt = conn.execute("SELECT text FROM transcripts WHERE id = ?", (tid,)).fetchone()
                        if not rt: send_msg(t, cid, "Transcript expired - forward the voice note again.")
                        else:
                            SESSION.post(api(t, "sendChatAction"), json={"chat_id": cid, "action": "typing"}, timeout=10)
                            prompts = {
                                "sum": ("*Summary:*\n", "Summarize this voice transcript into 3-5 concise bullet points in the same language as the input. Include any action items."),
                                "en": ("*English Translation:*\n", "Translate accurately into natural English. Output ONLY the translation."),
                                "ru": ("*Russian Translation:*\n", "Translate accurately into natural Russian. Output ONLY the translation."),
                                "uz": ("*Uzbek Translation:*\n", "Translate accurately into natural Uzbek Latin (o', g', sh, ch). Output ONLY the translation."),
                            }
                            if act in prompts:
                                hdr, sp = prompts[act]
                                send_msg(t, cid, f"{hdr}{groq_llm(gk, sp, rt[0]) or 'Could not process.'}")
                continue
            msg = upd.get("message")
            if not msg: continue
            cid = msg["chat"]["id"]
            u = msg.get("from", {})
            uid, uname, fname, today = u.get("id", cid), u.get("username", ""), u.get("first_name", ""), date.today().isoformat()
            row = conn.execute("SELECT free_date, free_used, paid_credits, pro_until, referrals, lang FROM users WHERE user_id = ?", (uid,)).fetchone()
            if row is None:
                t0, ref_by, bonus = (msg.get("text") or "").strip(), None, 0
                if t0.startswith("/start ref_") and t0[11:].isdigit():
                    ref_by = int(t0[11:])
                    if ref_by != uid and conn.execute("SELECT 1 FROM users WHERE user_id = ?", (ref_by,)).fetchone():
                        bonus = REFERRAL_BONUS
                        conn.execute("UPDATE users SET paid_credits = paid_credits + ?, referrals = referrals + 1 WHERE user_id = ?", (REFERRAL_BONUS, ref_by))
                conn.execute("INSERT INTO users (user_id, username, first_name, free_date, free_used, paid_credits, referred_by, created_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?)", (uid, uname, fname, today, bonus, ref_by, datetime.now().isoformat(timespec="seconds")))
                conn.commit()
                row = (today, 0, bonus, None, 0, "uz")
            elif row[0] != today:
                conn.execute("UPDATE users SET free_date = ?, free_used = 0 WHERE user_id = ?", (today, uid))
                conn.commit()
                row = (today, 0, row[2], row[3], row[4], row[5])
            fu, pc, pu, refs, ulang = row[1], row[2], row[3], row[4], row[5] or "uz"
            pro = bool(pu and datetime.fromisoformat(pu) > datetime.now())
            if msg.get("successful_payment"):
                sp = msg["successful_payment"]
                pid, stars = sp.get("invoice_payload", "pack_100"), int(sp.get("total_amount", 0))
                p = PACKS.get(pid, PACKS["pack_100"])
                if p["credits"]: conn.execute("UPDATE users SET paid_credits = paid_credits + ?, stars_spent = stars_spent + ? WHERE user_id = ?", (p["credits"], stars, uid))
                if p["pro_days"]: conn.execute("UPDATE users SET pro_until = ?, stars_spent = stars_spent + ? WHERE user_id = ?", ((datetime.now() + timedelta(days=p["pro_days"])).isoformat(timespec="seconds"), stars, uid))
                conn.commit()
                send_msg(t, cid, f"Payment received! Activated *{p['title']}*.")
                if oid: send_msg(t, oid, f"CHA-CHING! +{stars} Stars ({p['title']}) from @{uname or uid}!")
                continue
            txt = (msg.get("text") or "").strip()
            if txt.startswith("/start"):
                send_msg(t, cid, f"*Welcome to @{bu}!* (24/7 Cloud)\n\nForward any voice note, audio, or video note (UZ / RU / EN) -> instant transcript + 1-tap *Summarize* & *Translate (UZ/EN/RU)*!\n- Free: `{FREE_DAILY_LIMIT}/day`\n- `/lang` switch language\n- `/invite` +{REFERRAL_BONUS} free credits/friend\n- `/buy` Telegram Stars packs", buy_kb()); continue
            if txt.startswith("/lang"): send_msg(t, cid, f"Current language: `{ulang.upper()}`", lang_kb()); continue
            if txt.startswith("/buy"): send_msg(t, cid, "*Upgrade with Telegram Stars:*", buy_kb()); continue
            if txt.startswith("/invite"): send_msg(t, cid, f"Share your link (+{REFERRAL_BONUS} credits each):\n`https://t.me/{bu}?start=ref_{uid}`"); continue
            if txt.startswith("/balance"): send_msg(t, cid, f"Free today: `{max(0, FREE_DAILY_LIMIT - fu)}/{FREE_DAILY_LIMIT}`\nPaid: `{pc}`\nPro: `{'Active' if pro else 'No'}`", buy_kb()); continue
            if txt.startswith("/admin") and uid == oid:
                uc, jc, sc = conn.execute("SELECT COUNT(*), COALESCE(SUM(total_uses),0), COALESCE(SUM(stars_spent),0) FROM users").fetchone()
                send_msg(t, cid, f"*24/7 Cloud Admin*\nUsers: `{uc}` | Jobs: `{jc}` | Stars: `{sc}`"); continue
            media = msg.get("voice") or msg.get("audio") or msg.get("video_note")
            if media:
                if pro: tier = "pro"; conn.execute("UPDATE users SET total_uses = total_uses + 1 WHERE user_id = ?", (uid,))
                elif fu < FREE_DAILY_LIMIT: tier = "free"; conn.execute("UPDATE users SET free_used = free_used + 1, total_uses = total_uses + 1 WHERE user_id = ?", (uid,))
                elif pc > 0: tier = "paid"; conn.execute("UPDATE users SET paid_credits = paid_credits - 1, total_uses = total_uses + 1 WHERE user_id = ?", (uid,))
                else: send_msg(t, cid, "Daily free limit reached! Upgrade with Stars or `/invite` a friend:", buy_kb()); continue
                conn.commit()
                SESSION.post(api(t, "sendChatAction"), json={"chat_id": cid, "action": "typing"}, timeout=10)
                try:
                    fp = SESSION.get(api(t, "getFile"), params={"file_id": media["file_id"]}, timeout=20).json()["result"]["file_path"]
                    ab = SESSION.get(f"https://api.telegram.org/file/bot{t}/{fp}", timeout=60).content
                    tr = groq_transcribe(gk, ab, Path(fp).name or "voice.ogg", lang=ulang)
                    if not tr: send_msg(t, cid, "Could not detect speech in that audio.")
                    else:
                        cur = conn.execute("INSERT INTO transcripts (user_id, text, created_at) VALUES (?, ?, ?)", (uid, tr, datetime.now().isoformat(timespec="seconds")))
                        tid = cur.lastrowid
                        conn.commit()
                        kb = transcript_kb(tid, bu, uid, include_share=(tier == "free"))
                        if len(tr) > 3500:
                            SESSION.post(api(t, "sendDocument"), data={"chat_id": cid, "caption": f"Transcript ({len(tr)} chars) - @{bu}"}, files={"document": ("transcript.txt", tr.encode("utf-8"))}, timeout=60)
                            send_msg(t, cid, "Tap below to summarize or translate:", kb)
                        else:
                            send_msg(t, cid, f"{tr}" + (f"\n\n_Transcribed by @{bu}_" if tier == "free" else ""), kb)
                except Exception as e:
                    print(f"[render] err: {e}", flush=True)
                    send_msg(t, cid, "Transcription error - please try again.")

web_app = FastAPI(title="UzScribeBot 24/7 Cloud")
@web_app.get("/", response_class=HTMLResponse)
def health(): return "<h1>@UzScribeBot is Live 24/7 on Render</h1><p><a href='https://t.me/UzScribeBot'>Open @UzScribeBot</a></p>"
@web_app.get("/health")
def health_json(): return JSONResponse({"ok": True, "service": "UzScribeBot"})

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    uvicorn.run(web_app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
