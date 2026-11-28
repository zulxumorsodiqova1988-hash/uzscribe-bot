"""
@UzScribeBot — 24/7 Cloud Bot on Render Free Tier (~45 MB RAM)
Features:
  - Groq Whisper Large v3 transcription (UZ / RU / EN / Auto) + 120B Uzbek Latin reconstructor
  - 1-Tap inline buttons under every transcript: [Summarize] [-> EN] [-> RU] [-> UZ]
  - Cloud /tts & /tts_m neural voices (UZ / RU / EN) via edge-tts + optional LOCAL_TTS_URL hook
  - Native Telegram Stars (XTR) billing + viral /invite + rich /admin user dashboard
"""

import asyncio
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import edge_tts
import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

DB_PATH = Path(__file__).resolve().parent / "scribe_users.db"
FREE_DAILY_LIMIT, REFERRAL_BONUS = 5, 10

PACKS = {
    "pack_100": {
        "title": "100 Audio Credits",
        "description": "100 fast voice transcriptions, summaries & translations (never expire).",
        "stars": 50,
        "credits": 100,
        "pro_days": 0,
    },
    "pro_30d": {
        "title": "Pro Unlimited (30 Days)",
        "description": "Unlimited voice-to-text, AI summaries & translations for 30 days.",
        "stars": 150,
        "credits": 0,
        "pro_days": 30,
    },
}

SESSION = requests.Session()


def api(t: str, m: str) -> str:
    return f"https://api.telegram.org/bot{t}/{m}"


def init_db() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            free_date TEXT, free_used INTEGER DEFAULT 0, paid_credits INTEGER DEFAULT 0,
            pro_until TEXT, referred_by INTEGER, referrals INTEGER DEFAULT 0,
            total_uses INTEGER DEFAULT 0, stars_spent INTEGER DEFAULT 0,
            lang TEXT DEFAULT 'uz', created_at TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            text TEXT,
            created_at TEXT
        )
        """
    )
    c.commit()
    return c


def send_msg(t: str, cid: int, txt: str, rm: dict | None = None):
    p = {"chat_id": cid, "text": txt[:4000], "parse_mode": "Markdown"}
    if rm:
        p["reply_markup"] = rm
    r = SESSION.post(api(t, "sendMessage"), json=p, timeout=20).json()
    if not r.get("ok"):
        p.pop("parse_mode", None)
        SESSION.post(api(t, "sendMessage"), json=p, timeout=20)


def buy_kb() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Buy 100 Credits — 50 Stars", "callback_data": "buy:pack_100"}],
            [{"text": "Pro Unlimited (30d) — 150 Stars", "callback_data": "buy:pro_30d"}],
        ]
    }


def lang_kb() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "O'zbek (UZ)", "callback_data": "lang:uz"}, {"text": "Русский (RU)", "callback_data": "lang:ru"}],
            [{"text": "English (EN)", "callback_data": "lang:en"}, {"text": "Auto", "callback_data": "lang:auto"}],
        ]
    }


def transcript_kb(tid: int, bot_u: str, uid: int, include_share: bool) -> dict:
    rows = [
        [
            {"text": "Summarize", "callback_data": f"act:sum:{tid}"},
            {"text": "-> EN", "callback_data": f"act:en:{tid}"},
            {"text": "-> RU", "callback_data": f"act:ru:{tid}"},
            {"text": "-> UZ", "callback_data": f"act:uz:{tid}"},
        ]
    ]
    if include_share:
        link = f"https://t.me/{bot_u}?start=ref_{uid}"
        txt = "Ovozli xabarlarni 1 soniyada matnga aylantiruvchi + tarjima qiluvchi AI bot!"
        rows.append([{"text": "Share with a Friend (+10 Free Credits)", "url": f"https://t.me/share/url?url={quote(link)}&text={quote(txt)}"}])
    return {"inline_keyboard": rows}


def groq_llm(gk: str, system_prompt: str, user_text: str, model: str = "openai/gpt-oss-120b") -> str:
    for m in (model, "openai/gpt-oss-20b"):
        r = SESSION.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {gk}", "Content-Type": "application/json"},
            json={
                "model": m,
                "reasoning_effort": "low",
                "temperature": 0.15,
                "max_completion_tokens": 1024,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"<transcript>\n{user_text[:6000]}\n</transcript>"},
                ],
            },
            timeout=25,
        )
        if r.ok:
            out = (r.json()["choices"][0]["message"].get("content") or "").strip()
            if out:
                return out
        print(f"[render] groq_llm {m} {r.status_code}: {r.text[:200]}", flush=True)
        if m == "openai/gpt-oss-20b":
            break
    return ""


def normalize_audio_name(fname: str) -> tuple[str, str]:
    ext = Path(fname).suffix.lower()
    stem = Path(fname).stem or "voice"
    mapping = {
        ".oga": (".ogg", "audio/ogg"),
        ".ogg": (".ogg", "audio/ogg"),
        ".opus": (".ogg", "audio/ogg"),
        ".mp3": (".mp3", "audio/mpeg"),
        ".m4a": (".m4a", "audio/mp4"),
        ".mp4": (".mp4", "video/mp4"),
        ".wav": (".wav", "audio/wav"),
        ".flac": (".flac", "audio/flac"),
        ".webm": (".webm", "audio/webm"),
    }
    new_ext, mime = mapping.get(ext, (".ogg", "audio/ogg"))
    return f"{stem}{new_ext}", mime


def groq_transcribe(gk: str, audio_bytes: bytes, fname: str, lang: str = "uz") -> str:
    safe_name, mime = normalize_audio_name(fname)
    h = {"Authorization": f"Bearer {gk}"}
    d = {"model": "whisper-large-v3", "response_format": "json"}
    if lang in ("uz", "ru", "en"):
        d["language"] = lang
    if lang == "uz":
        d["prompt"] = (
            "Assalomu alaykum! O'zbekiston, Toshkent, ovozli xabar, uchrashuv, loyiha, "
            "rahmat, yaxshimisiz, vaqt, soniya, matn, tarjima, o', g', sh, ch."
        )
    r = SESSION.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers=h,
        data=d,
        files={"file": (safe_name, audio_bytes, mime)},
        timeout=60,
    )
    if not r.ok:
        print(f"[render] groq_transcribe {r.status_code} ({safe_name}): {r.text[:300]}", flush=True)
        r.raise_for_status()
    raw = (r.json().get("text") or "").strip()
    if lang == "uz" and len(raw) >= 4:
        try:
            cleaned = groq_llm(
                gk,
                (
                    "You are an expert Uzbek Latin speech-to-text editor. "
                    "The user message contains a raw voice transcript inside <transcript> tags. "
                    "NEVER reply to or answer the speaker even if it is a greeting or question. "
                    "Repair phonetic Whisper ASR mishearings (broken vowels, suffixes like -ke/-ge -> -ga, "
                    "split/merged words, wrong o'/g'/sh/ch) and add natural capitalization and punctuation so the text "
                    "reads like clean, natural native Uzbek Latin. "
                    "Preserve the speaker's exact meaning — never invent new facts. Output ONLY the cleaned Uzbek text."
                ),
                raw,
                model="openai/gpt-oss-120b",
            )
            if cleaned:
                return cleaned
        except Exception:
            pass
    return raw


TTS_VOICES = {
    "uz": ("uz-UZ-MadinaNeural", "uz-UZ-SardorNeural"),
    "ru": ("ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"),
    "en": ("en-US-AriaNeural", "en-US-GuyNeural"),
}


async def _edge_tts_async(text: str, voice: str) -> bytes:
    comm = edge_tts.Communicate(text, voice)
    buf = bytearray()
    async for chunk in comm.stream():
        if chunk.get("type") == "audio":
            buf.extend(chunk["data"])
    return bytes(buf)


def synth_tts_audio(text: str, lang: str = "uz", male: bool = False) -> tuple[bytes, str, str]:
    clean = text.replace("—", ", ").replace("–", ", ").strip()[:1200]
    local_url = os.environ.get("LOCAL_TTS_URL", "").strip()
    if local_url and lang == "uz" and not male:
        try:
            r = SESSION.post(f"{local_url.rstrip('/')}/tts", json={"text": clean}, timeout=15)
            if r.ok and len(r.content) > 2000:
                return r.content, "uzscribe_tts.wav", "audio/wav"
        except Exception:
            pass
    v_pair = TTS_VOICES.get(lang, TTS_VOICES["uz"])
    voice = v_pair[1] if male else v_pair[0]
    mp3_bytes = asyncio.run(_edge_tts_async(clean, voice))
    return mp3_bytes, "uzscribe_tts.mp3", "audio/mpeg"


def format_admin_report(conn: sqlite3.Connection, today: str, limit: int = 25) -> str:
    uc, jc, sc, rc = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_uses),0), COALESCE(SUM(stars_spent),0), COALESCE(SUM(referrals),0) FROM users"
    ).fetchone()
    active_today = conn.execute("SELECT COUNT(*) FROM users WHERE free_date = ? AND free_used > 0", (today,)).fetchone()[0]
    now_iso = datetime.now().isoformat(timespec="seconds")
    pro_cnt = conn.execute("SELECT COUNT(*) FROM users WHERE pro_until IS NOT NULL AND pro_until > ?", (now_iso,)).fetchone()[0]
    rows = conn.execute(
        """
        SELECT user_id, username, first_name, lang, total_uses, free_used, paid_credits, referrals, stars_spent, pro_until, created_at
        FROM users
        ORDER BY total_uses DESC, created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    lines = [
        "*24/7 Cloud Admin Dashboard*",
        f"Users: `{uc}` (Active today: `{active_today}` | Pro: `{pro_cnt}`)",
        f"Jobs: `{jc}` | Referrals: `{rc}` | Stars: `{sc}` (~${sc * 0.013:.2f})",
        "",
        f"*Users (Top {min(len(rows), limit)}):*",
    ]
    for idx, r in enumerate(rows, 1):
        uid_, un_, fn_, lg_, tu_, fu_, pc_, rf_, st_, pu_, cr_ = r
        nick = f"@{un_}" if un_ else "no_nick"
        name = (fn_ or "").replace("*", "").replace("_", "").replace("`", "")[:18]
        pro_tag = " [PRO]" if (pu_ and pu_ > now_iso) else ""
        joined = (cr_ or "")[:10]
        lines.append(
            f"{idx}. {nick} ({name}) `id:{uid_}`{pro_tag}\n"
            f"   uses:{tu_} | paid:{pc_} | refs:{rf_} | stars:{st_} | {(lg_ or 'uz').upper()} | {joined}"
        )
    return "\n".join(lines)


def bot_loop():
    t = os.environ.get("SCRIBE_BOT_TOKEN", "").strip()
    gk = os.environ.get("GROQ_API_KEY", "").strip()
    oid = int(os.environ.get("OWNER_CHAT_ID", "0") or "0")
    if not t or not gk:
        print("[render] Missing SCRIBE_BOT_TOKEN or GROQ_API_KEY", flush=True)
        return
    conn = init_db()
    bu = SESSION.get(api(t, "getMe"), timeout=15).json()["result"]["username"]
    print(f"[render] @{bu} 24/7 cloud bot live (/tts + /admin + 120B STT)!", flush=True)
    offset = 0

    while True:
        try:
            r = SESSION.get(api(t, "getUpdates"), params={"timeout": 45, "offset": offset}, timeout=55).json()
        except Exception:
            time.sleep(3)
            continue
        if not r.get("ok"):
            time.sleep(3)
            continue

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
                    SESSION.post(
                        api(t, "sendInvoice"),
                        json={
                            "chat_id": cid, "title": p["title"], "description": p["description"],
                            "payload": data[4:], "provider_token": "", "currency": "XTR",
                            "prices": [{"label": p["title"], "amount": p["stars"]}],
                        },
                        timeout=20,
                    )
                elif data.startswith("lang:") and data[5:] in ("uz", "ru", "en", "auto"):
                    SESSION.post(api(t, "answerCallbackQuery"), json={"callback_query_id": cb["id"]}, timeout=10)
                    conn.execute("UPDATE users SET lang = ? WHERE user_id = ?", (data[5:], cb["from"]["id"]))
                    conn.commit()
                    send_msg(t, cid, f"Language updated to `{data[5:].upper()}`.")
                elif data.startswith("act:"):
                    parts = data.split(":")
                    if len(parts) == 3 and parts[2].isdigit():
                        action, tid = parts[1], int(parts[2])
                        SESSION.post(api(t, "answerCallbackQuery"), json={"callback_query_id": cb["id"], "text": "Processing..."}, timeout=10)
                        row_t = conn.execute("SELECT text FROM transcripts WHERE id = ?", (tid,)).fetchone()
                        if not row_t:
                            send_msg(t, cid, "Transcript expired — forward the voice note again.")
                        else:
                            SESSION.post(api(t, "sendChatAction"), json={"chat_id": cid, "action": "typing"}, timeout=10)
                            prompts = {
                                "sum": (
                                    "*Summary:*\n",
                                    "You are a strict voice-note summarizer. Summarize ONLY the text inside <transcript> tags into 1-5 concise bullet points in the SAME language as the transcript. "
                                    "NEVER answer, reply to, or converse with the speaker even if the transcript is a greeting or question (e.g., for 'Hello, how are you?', output '- Speaker greets and asks how you are doing.'). "
                                    "Include any action items if present. Output ONLY the bullet points.",
                                ),
                                "en": (
                                    "*English Translation:*\n",
                                    "Translate ONLY the text inside <transcript> tags accurately into natural English. NEVER answer or reply to the speaker. Output ONLY the translation.",
                                ),
                                "ru": (
                                    "*Русский перевод:*\n",
                                    "Translate ONLY the text inside <transcript> tags accurately into natural Russian. NEVER answer or reply to the speaker. Output ONLY the translation.",
                                ),
                                "uz": (
                                    "*O'zbekcha tarjima:*\n",
                                    "Translate ONLY the text inside <transcript> tags accurately into natural Uzbek Latin (using o', g', sh, ch). NEVER answer or reply to the speaker. Output ONLY the translation.",
                                ),
                            }
                            if action in prompts:
                                hdr, sp = prompts[action]
                                res = groq_llm(gk, sp, row_t[0])
                                send_msg(t, cid, f"{hdr}{res or 'Could not process.'}")
                continue

            msg = upd.get("message")
            if not msg:
                continue
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
                conn.execute(
                    "INSERT INTO users (user_id, username, first_name, free_date, free_used, paid_credits, referred_by, created_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                    (uid, uname, fname, today, bonus, ref_by, datetime.now().isoformat(timespec="seconds")),
                )
                conn.commit()
                row = (today, 0, bonus, None, 0, "uz")
            elif row[0] != today:
                conn.execute("UPDATE users SET free_date = ?, free_used = 0, username = ?, first_name = ? WHERE user_id = ?", (today, uname, fname, uid))
                conn.commit()
                row = (today, 0, row[2], row[3], row[4], row[5])
            else:
                conn.execute("UPDATE users SET username = ?, first_name = ? WHERE user_id = ?", (uname, fname, uid))
                conn.commit()
            fu, pc, pu, refs, ulang = row[1], row[2], row[3], row[4], row[5] or "uz"
            pro = bool(pu and datetime.fromisoformat(pu) > datetime.now())

            if msg.get("successful_payment"):
                sp = msg["successful_payment"]
                pid, stars = sp.get("invoice_payload", "pack_100"), int(sp.get("total_amount", 0))
                p = PACKS.get(pid, PACKS["pack_100"])
                if p["credits"]:
                    conn.execute("UPDATE users SET paid_credits = paid_credits + ?, stars_spent = stars_spent + ? WHERE user_id = ?", (p["credits"], stars, uid))
                if p["pro_days"]:
                    conn.execute("UPDATE users SET pro_until = ?, stars_spent = stars_spent + ? WHERE user_id = ?", ((datetime.now() + timedelta(days=p["pro_days"])).isoformat(timespec="seconds"), stars, uid))
                conn.commit()
                send_msg(t, cid, f"Payment received! Activated *{p['title']}*.")
                if oid:
                    send_msg(t, oid, f"CHA-CHING! +{stars} Stars ({p['title']}) from @{uname or uid}!")
                continue

            txt = (msg.get("text") or "").strip()
            if txt.startswith("/start"):
                send_msg(
                    t, cid,
                    f"*Welcome to @{bu}!* (24/7 Cloud)\n\n"
                    f"Forward any voice note, audio, or video note (UZ / RU / EN) -> instant transcript + 1-tap *Summarize* & *Translate (UZ/EN/RU)*!\n\n"
                    f"- `/tts <text>` natural AI voice (female) | `/tts_m <text>` (male)\n"
                    f"- Free: `{FREE_DAILY_LIMIT}/day`\n"
                    f"- `/lang` switch language\n"
                    f"- `/invite` +{REFERRAL_BONUS} free credits/friend\n"
                    f"- `/buy` Telegram Stars packs",
                    buy_kb(),
                )
                continue
            if txt.startswith("/lang"):
                send_msg(t, cid, f"Current language: `{ulang.upper()}`", lang_kb())
                continue
            if txt.startswith("/buy"):
                send_msg(t, cid, "*Upgrade with Telegram Stars:*", buy_kb())
                continue
            if txt.startswith("/invite"):
                send_msg(t, cid, f"Share your link (+{REFERRAL_BONUS} credits each):\n`https://t.me/{bu}?start=ref_{uid}`")
                continue
            if txt.startswith("/balance"):
                send_msg(t, cid, f"Free today: `{max(0, FREE_DAILY_LIMIT - fu)}/{FREE_DAILY_LIMIT}`\nPaid: `{pc}`\nPro: `{'Active' if pro else 'No'}`", buy_kb())
                continue
            if txt.startswith("/admin") and uid == oid:
                lim = 50 if "all" in txt.lower() else 25
                send_msg(t, cid, format_admin_report(conn, today, limit=lim))
                continue
            if txt.startswith("/tts"):
                is_male = txt.startswith("/tts_m")
                body = txt.split(None, 1)[1].strip() if " " in txt else ""
                if not body:
                    send_msg(t, cid, "Usage: `/tts Assalomu alaykum!` (or `/tts_m` for male voice)")
                    continue
                if pro:
                    conn.execute("UPDATE users SET total_uses = total_uses + 1 WHERE user_id = ?", (uid,))
                elif fu < FREE_DAILY_LIMIT:
                    conn.execute("UPDATE users SET free_used = free_used + 1, total_uses = total_uses + 1 WHERE user_id = ?", (uid,))
                elif pc > 0:
                    conn.execute("UPDATE users SET paid_credits = paid_credits - 1, total_uses = total_uses + 1 WHERE user_id = ?", (uid,))
                else:
                    send_msg(t, cid, "Daily free limit reached! Upgrade with Stars or `/invite` a friend:", buy_kb())
                    continue
                conn.commit()
                SESSION.post(api(t, "sendChatAction"), json={"chat_id": cid, "action": "upload_voice"}, timeout=10)
                try:
                    tts_lang = ulang if ulang in ("uz", "ru", "en") else "uz"
                    audio_b, afname, amime = synth_tts_audio(body, lang=tts_lang, male=is_male)
                    SESSION.post(
                        api(t, "sendAudio"),
                        data={"chat_id": cid, "title": "UzScribe AI Voice", "performer": f"@{bu}", "caption": body[:200]},
                        files={"audio": (afname, audio_b, amime)},
                        timeout=45,
                    )
                except Exception as e:
                    print(f"[render] tts err: {e}", flush=True)
                    send_msg(t, cid, "TTS generation error — please try again.")
                continue

            media = msg.get("voice") or msg.get("audio") or msg.get("video_note")
            if media:
                if pro:
                    tier = "pro"
                    conn.execute("UPDATE users SET total_uses = total_uses + 1 WHERE user_id = ?", (uid,))
                elif fu < FREE_DAILY_LIMIT:
                    tier = "free"
                    conn.execute("UPDATE users SET free_used = free_used + 1, total_uses = total_uses + 1 WHERE user_id = ?", (uid,))
                elif pc > 0:
                    tier = "paid"
                    conn.execute("UPDATE users SET paid_credits = paid_credits - 1, total_uses = total_uses + 1 WHERE user_id = ?", (uid,))
                else:
                    send_msg(t, cid, "Daily free limit reached! Upgrade with Stars or `/invite` a friend:", buy_kb())
                    continue
                conn.commit()
                SESSION.post(api(t, "sendChatAction"), json={"chat_id": cid, "action": "typing"}, timeout=10)
                try:
                    fp = SESSION.get(api(t, "getFile"), params={"file_id": media["file_id"]}, timeout=20).json()["result"]["file_path"]
                    ab = SESSION.get(f"https://api.telegram.org/file/bot{t}/{fp}", timeout=60).content
                    tr = groq_transcribe(gk, ab, Path(fp).name or "voice.ogg", lang=ulang)
                    if not tr:
                        send_msg(t, cid, "Could not detect speech in that audio.")
                    else:
                        cur = conn.execute(
                            "INSERT INTO transcripts (user_id, text, created_at) VALUES (?, ?, ?)",
                            (uid, tr, datetime.now().isoformat(timespec="seconds")),
                        )
                        tid = cur.lastrowid
                        conn.commit()
                        kb = transcript_kb(tid, bu, uid, include_share=(tier == "free"))
                        if len(tr) > 3500:
                            SESSION.post(
                                api(t, "sendDocument"),
                                data={"chat_id": cid, "caption": f"Full transcript ({len(tr):,} chars) — @{bu}"},
                                files={"document": ("transcript.txt", tr.encode("utf-8"))},
                                timeout=60,
                            )
                            send_msg(t, cid, "Tap below to summarize or translate:", kb)
                        else:
                            footer = f"\n\n_Transcribed by @{bu}_" if tier == "free" else ""
                            send_msg(t, cid, f"{tr}{footer}", kb)
                except Exception as e:
                    print(f"[render] err: {e}", flush=True)
                    send_msg(t, cid, "Transcription error — please try again.")


web_app = FastAPI(title="UzScribeBot 24/7 Cloud")


@web_app.get("/", response_class=HTMLResponse)
def health():
    return "<h1>@UzScribeBot is Live 24/7 on Render</h1><p><a href='https://t.me/UzScribeBot'>Open @UzScribeBot</a></p>"


@web_app.get("/health")
def health_json():
    return JSONResponse({"ok": True, "service": "UzScribeBot"})


if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    uvicorn.run(web_app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
