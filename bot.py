"""Just Chatting (ENG): a private English speaking-practice bot for Telegram.

CODE MAP (search for these section titles):
  settings ........ environment variables and constants
  styles/prompt ... chat styles and the rules sent to the AI
  storage ......... Postgres (Supabase) with an in-memory fallback
  streak .......... streak counting, pinning, morning message
  ai .............. Gemini (main) and Groq (fallback), answer parsing and filters
  voice ........... Gemini (main) and Groq Whisper (fallback)
  handlers ........ commands, settings menu, messages
  web server ...... webhook, health check, story page
"""
import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import random
import re
import time
from datetime import date, datetime, time as dtime, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo

import asyncpg
import httpx
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           WebAppInfo)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import story

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ---- settings (all secrets come from environment variables) ----
BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-large-v3-turbo")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me-please-123")
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

MAX_VOICE_SECONDS = 60
COOLDOWN_SECONDS = 5  # only for people the admin allowed, not for the admin
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "30"))  # messages (user + bot)
MAX_TOKENS = 1500
AI_ATTEMPTS = 2  # attempts per AI provider
RETRY_STATUSES = {400, 408, 422, 500, 502, 503, 504}
STREAK_MESSAGES = 3  # messages needed to count a day
MORNING_WINDOW_HOURS = 6  # a late morning message is skipped after this long

LEVELS = {"a": "A1-A2 (beginner)", "b": "B1-B2 (intermediate)", "c": "C1-C2 (advanced)"}
TOPICS = ["Games", "Sport", "Music", "Movies", "Travel", "Food"]
PRAISE = ["🔥", "❤️", "✨"]
TIMEZONES = [
    [("🇷🇺 Moscow", "Europe/Moscow"), ("🇺🇦 Kyiv", "Europe/Kyiv"), ("🇧🇾 Minsk", "Europe/Minsk")],
    [("🇦🇲 Yerevan", "Asia/Yerevan"), ("🇬🇪 Tbilisi", "Asia/Tbilisi"), ("🇦🇿 Baku", "Asia/Baku")],
    [("🇰🇿 Almaty", "Asia/Almaty"), ("🇺🇿 Tashkent", "Asia/Tashkent"), ("🇦🇪 Dubai", "Asia/Dubai")],
    [("🇩🇪 Berlin", "Europe/Berlin"), ("🇬🇧 London", "Europe/London"),
     ("🇺🇸 New York", "America/New_York")],
]
MORNING_TIMES = [["07:00", "08:00", "09:00"], ["10:00", "11:00", "12:00"]]

# ---- chat styles: key -> (button label, instruction for the AI) ----
FILLER_TEXT = (
    " Filler words (kinda, sorta, you know, I mean, like, uh, well) are allowed ONLY when you are "
    "genuinely thrown off: the conversation gets confusing, you don't know how to answer, or the topic "
    "is awkward. Then react like a real friend who is lost for words ('uh, I mean... that's kinda "
    "messed up, you know?', 'ugh, well, damn', 'I mean, I don't even know what to say'). "
    "In normal, smooth replies use no filler words at all."
)

PROFANITY_TEXT = (
    " Casual swearing (damn, shit, hell, fuck, pissed off, 'that's some bullshit', 'holy shit') is "
    "allowed when it fits the moment naturally (surprise, frustration, a funny rant) or when the user "
    "swears first. Match the user's level and never be more vulgar than they are. Use it rarely, never "
    "in every message, never as an insult aimed at the user, never slurs or hate, and not when the "
    "topic is serious or the user is upset or hurting."
)

STYLES = {
    "friend": (
        "😊 Friend",
        "Talk like a close friend texting: relaxed, warm, natural contractions, light humor, "
        "short reactions (like 'oh nice', 'haha', 'wait, really?', 'that sounds rough', 'honestly'). "
        "Share a quick opinion or reaction of your own." + FILLER_TEXT + PROFANITY_TEXT,
    ),
    "slang": (
        "😎 Slang",
        "Slang is your default voice. Talk like a young American friend texting, and use natural slang "
        "or casual expressions in almost EVERY message, usually two or three (lowkey, no way, that's wild, "
        "dude, bro, for real, deadass, I'm dead, bet, vibe, tbh, ngl, fr, kinda, gonna, wanna). "
        "Let the whole sentence sound casual, not just one word. Pick slang a real 20-something would "
        "actually say; avoid outdated or meme-spam slang so it never feels cringe or forced. "
        "Keep it understandable for the learner's level." + FILLER_TEXT + PROFANITY_TEXT,
    ),
    "classic": (
        "🎩 Classic",
        "Talk in clear, polite, standard conversational English, like a warm, well-educated colleague "
        "or tutor. Full sentences, no slang, no abbreviations. Friendly, not stiff.",
    ),
    "curious": (
        "🤔 Curious",
        "Act like a genuinely curious person getting to know the user. React to the specifics of their "
        "message, then ask ONE follow-up about a detail they mentioned (never a generic question). "
        "Show interest so they want to write more. Keep your own comments brief.",
    ),
}
DEFAULT_STYLE = "friend"

HUMAN_RULES = (
    "Write like a real person in a chat, not like an assistant. Rules:\n"
    "- Match the length to the user's message: a quick message gets 1-2 short sentences; if they write "
    "more, go deeper, or raise a topic worth discussing, you may write a fuller reply (up to about 6 "
    "sentences, one short paragraph, never an essay or a list). React to what they said first. "
    "Ask a question only sometimes; do NOT end every message with a question.\n"
    "- Do not use filler openers out of habit ('Certainly', 'Absolutely', 'Great question', "
    "\"That's a great ...\"). 'Sure', 'Of course' and 'Got it' are fine when they genuinely fit the "
    "moment, but do not lean on them. Vary how you start; often just react directly.\n"
    "- Never use long dashes (— or –); use a comma or a period instead.\n"
    "- Do not start two replies in a row with the same word or phrase.\n"
    "- You do not know the user's local time or place; do not mention the time of day or the weather "
    "unless they did.\n"
    "- Remember everything the user has told you in this conversation. Never ask about something they "
    "already answered, and never repeat a question or an angle you already covered. Build on new details.\n"
    "- Most messages should NOT end with a question: ask at most every second message, otherwise just "
    "react, share a short opinion, or add a thought. Vary the questions; do not keep using 'Have you ever...'.\n"
    "- Do not repeat or summarize the user's words back to them. No lists, no headings, no emojis, "
    "no exclamation marks in every message.\n"
    "- Have opinions and small preferences, and keep a consistent personality. Do not claim human "
    "experiences like having a body or a job. If the user sincerely asks whether you are an AI, "
    "answer honestly that you are.\n"
    "- If the user asks what a word or phrase means, explain it simply in English with a short example. "
    "But if they ask you to translate something (into Russian by default, or into any language they name) "
    "or ask in Russian, give the translation in that language: "
    "first the translation, then the pronunciation of the English word in /slashes/, and mention any "
    "preposition that changes the meaning (for example 'look for' vs 'look after'); if there is none, "
    "say nothing about prepositions. For a single word "
    "give the 2-3 most common translations separated by commas, most common first, with no long "
    "explanations. Use the proper stress mark (ˈ) in the "
    "pronunciation. If you are not fully sure about a translation (especially into less common languages), "
    "say so briefly and give the closest word. Do not add an unrelated question after a translation; "
    "just stop, or comment briefly on the word itself.\n"
)

MISTAKE_RULES = (
    "Check the user's last message and list every SERIOUS English mistake, meaning one that a native "
    "speaker would immediately notice and that clearly sounds wrong: "
    "verb forms and tenses (for example, 'for/since + a period of time' with an ongoing action needs "
    "present perfect), prepositions, word choice, word order, subject-verb agreement, spelling.\n"
    "Do NOT report: informal spoken English and slang, including modern slang and memes (wanna, gonna, "
    "gotta, ain't, kinda, lemme, dunno, y'all, 'cause, \"I'm cooked\", \"he's cooking\", 'no cap', 'bet', "
    "'lowkey'), repetition for emphasis ('big big', 'very very', 'so so good'), contractions, sentence "
    "fragments, casual use of an adjective instead of an adverb ('it's going amazing'), capitalization, "
    "punctuation, commas, run-on "
    "sentences, comma splices, extra spaces, or anything that is acceptable in natural spoken English. "
    "The text may come from a voice message. Report something only when you are sure it is wrong. "
    "Do not put articles, subtle, stylistic or optional choices (a slightly less natural word like "
    "'meal' vs 'food', 'the' vs no article when both work) into mistakes; they go into tips (see below).\n"
    "Before answering, double-check that each correction and both example sentences are fully correct English.\n"
)


# ---- storage: Postgres (Supabase) with an in-memory fallback ----
pool = None  # asyncpg pool, stays None when there is no database
mem_users: dict[int, dict] = {}
allowed: set[int] = set()  # people the admin gave access to
background_tasks: set = set()

USER_DEFAULTS = {
    "level": None, "topic": None, "style": "friend", "explain_lang": "ru",
    "tz": "Europe/Moscow", "morning_time": "11:00",
    "emoji": True, "streak_on": True, "morning_on": True, "tips_on": True, "pin_on": True,
    "streak": 0, "best_streak": 0, "last_day": None, "prev_streak": 0, "prev_last_day": None,
    "blocked_day": None, "today_date": None, "today_count": 0, "last_morning": None,
    "pinned_msg_id": None, "history": [], "active": False, "awaiting": None,
}
COLUMN_TYPES = {
    "level": "text", "topic": "text", "style": "text", "explain_lang": "text", "tz": "text",
    "morning_time": "text", "emoji": "boolean", "streak_on": "boolean", "morning_on": "boolean",
    "tips_on": "boolean", "pin_on": "boolean", "streak": "integer", "best_streak": "integer",
    "last_day": "date", "prev_streak": "integer", "prev_last_day": "date", "blocked_day": "date",
    "today_date": "date", "today_count": "integer", "last_morning": "date",
    "pinned_msg_id": "bigint", "history": "jsonb", "active": "boolean", "awaiting": "text",
}


def track(coro) -> None:
    """Runs a coroutine in the background and keeps a reference so it is not garbage collected."""
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def init_db() -> None:
    global pool
    if not DATABASE_URL:
        log.warning("DATABASE_URL is not set: using memory only, data is lost on restart")
        return

    async def _init(conn):
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
                                  schema="pg_catalog")

    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3, ssl="require",
                                         statement_cache_size=0, init=_init, command_timeout=20)
        async with pool.acquire() as conn:
            await conn.execute("CREATE TABLE IF NOT EXISTS users (user_id bigint PRIMARY KEY)")
            for col, typ in COLUMN_TYPES.items():
                await conn.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {typ}")
            await conn.execute("CREATE TABLE IF NOT EXISTS allowed (user_id bigint PRIMARY KEY)")
            rows = await conn.fetch("SELECT user_id FROM allowed")
        allowed.update(r["user_id"] for r in rows)
        log.info("Database ready")
    except Exception:
        log.exception("Database connection failed, using memory only")
        pool = None


def merge_row(uid: int, row: dict | None) -> dict:
    u = {"user_id": uid}
    for key, default in USER_DEFAULTS.items():
        value = row.get(key) if row else None
        if value is None:
            value = list(default) if isinstance(default, list) else default
        u[key] = value
    return u


async def get_user(uid: int) -> dict:
    if pool is not None:
        try:
            row = await pool.fetchrow("SELECT * FROM users WHERE user_id = $1", uid)
            if row is None:
                await pool.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", uid)
            return merge_row(uid, dict(row) if row else None)
        except Exception:
            log.exception("get_user failed")
    if uid not in mem_users:
        mem_users[uid] = merge_row(uid, None)
    return mem_users[uid]


async def upd(u: dict, **fields) -> None:
    u.update(fields)
    if pool is None or not fields:
        return
    names = list(fields)
    sets = ", ".join(f"{name} = ${i + 2}" for i, name in enumerate(names))
    try:
        await pool.execute(f"UPDATE users SET {sets} WHERE user_id = $1", u["user_id"],
                           *[fields[name] for name in names])
    except Exception:
        log.exception("upd failed")


async def morning_users() -> list[dict]:
    if pool is None:
        return list(mem_users.values())
    try:
        rows = await pool.fetch("SELECT * FROM users WHERE COALESCE(streak_on, true) "
                                "AND COALESCE(morning_on, true)")
        return [merge_row(r["user_id"], dict(r)) for r in rows]
    except Exception:
        log.exception("morning_users failed")
        return []


async def set_allowed(uid: int, on: bool) -> None:
    if on:
        allowed.add(uid)
    else:
        allowed.discard(uid)
    if pool is None:
        return
    try:
        if on:
            await pool.execute("INSERT INTO allowed (user_id) VALUES ($1) ON CONFLICT DO NOTHING", uid)
        else:
            await pool.execute("DELETE FROM allowed WHERE user_id = $1", uid)
    except Exception:
        log.exception("set_allowed failed")


# ---- time helpers ----
def get_tz(u: dict) -> ZoneInfo:
    try:
        return ZoneInfo(u["tz"])
    except Exception:
        return ZoneInfo("Europe/Moscow")


def local_now(u: dict) -> datetime:
    return datetime.now(get_tz(u))


def local_today(u: dict) -> date:
    return local_now(u).date()


def parse_tz(text: str) -> str | None:
    """Accepts a name like Europe/Berlin or an offset like +3, UTC+3, -5."""
    text = text.strip()
    m = re.fullmatch(r"(?:utc|gmt)?\s*([+-]?)(\d{1,2})", text, re.I)
    if m:
        hours = int(m.group(2))
        if hours > 14:
            return None
        if hours == 0:
            return "UTC"
        sign = m.group(1) or "+"
        name = f"Etc/GMT{'-' if sign == '+' else '+'}{hours}"  # Etc zones have an inverted sign
    else:
        name = text
    try:
        ZoneInfo(name)
    except Exception:
        return None
    return name


def tz_label(name: str) -> str:
    m = re.fullmatch(r"Etc/GMT([+-])(\d+)", name)
    if m:
        return f"UTC{'+' if m.group(1) == '-' else '-'}{m.group(2)}"
    return name


def parse_hhmm(text: str) -> str | None:
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*", text)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    if hh > 23 or mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


async def reset_chat(u: dict) -> None:
    # style and settings stay between conversations
    await upd(u, level=None, topic=None, history=[], awaiting=None, active=False)


def build_prompt(u: dict) -> str:
    level = LEVELS.get(u["level"]) if u["level"] else None
    explain_lang = "English" if u.get("explain_lang") == "en" else "Russian"
    style = STYLES.get(u.get("style"), STYLES[DEFAULT_STYLE])[1]
    topic = f"Topic to talk about: {u['topic']} (stay on it unless the user changes it)." if u["topic"] \
        else "Topic: free talk."
    return (
        'You are "Just Chatting", a chat partner for English speaking practice in Telegram. '
        "Use American English (spelling and vocabulary), never British.\n"
        f"Learner level: {level or 'unknown, adapt to the person as the chat goes'}. {topic}\n"
        f"Style: {style}\n"
        + HUMAN_RULES
        + "Match your vocabulary and sentence complexity to the learner's level.\n"
        "If the user's message is not in English and is not a request for a translation or a meaning, "
        "reply in English, kindly ask them to switch to English, "
        "and return an empty list for mistakes. Do not treat non-English text as a mistake.\n"
        + MISTAKE_RULES
        + 'Return ONLY a JSON object: {"reply": string, "mistakes": [{"quote": string, '
        '"explanation": string, "examples": [string, string]}], "tips": [string]}.\n'
        "Each mistake is a separate item. quote = the exact wrong phrase taken from the user's message, "
        "as short as possible. "
        f"explanation = why it is wrong, written in {explain_lang}, short and clear. "
        "examples = two short correct example sentences in English that reuse the same corrected word or "
        "phrase in a new context, so the user sees the same fix again.\n"
        "If there are no real mistakes, return an empty list for mistakes.\n"
        "tips: missing or wrong articles and less natural wording are NOT mistakes, put them here instead. "
        f"Each tip is ONE short line in {explain_lang} that says what is missing or offers the more natural "
        "phrase, for example 'Не хватает a: get a girlfriend' or 'Звучит естественнее: new food'. "
        "Typos and spelling slips (like 'lile' instead of 'like') also go to tips, never to mistakes, "
        "for example 'Опечатка: lile → like'. "
        "(examples are in Russian). At most 2 tips; use an empty list when the sentence is fine. "
        "Articles ALWAYS go to tips, never to mistakes: if one phrase has a verb problem and an article "
        "problem, explain only the verb problem in mistakes and put the article in tips. Only suggest "
        "what is truly missing; never claim something is missing that the user already wrote. "
        "Never suggest formal alternatives for informal words like wanna or gonna."
    )

class AIError(Exception):
    def __init__(self, code: str):
        self.code = code


TRIVIAL_WORDS = ("запят", "comma", "пунктуац", "punctuat", "заглавн", "прописн",
                 "capitaliz", "апостроф", "apostroph",
                 "wanna", "gonna", "gotta", "ain't", "kinda", "lemme", "dunno", "gimme", "y'all",
                 "формальн", "разговорн", "informal", "colloquial", "slang", "сленг")


def clean_reply(text) -> str:
    """Remove JSON debris that sometimes leaks into the reply text."""
    text = str(text)
    text = re.sub(r'[\s,"]*"mistakes"\s*:.*$', "", text, flags=re.S)
    text = re.sub(r'^\s*\{?\s*"reply"\s*:\s*"', "", text)
    text = text.replace("\u2011", "-")
    text = re.sub(r"\s*—\s*", ", ", text)
    text = re.sub(r"\s+–\s+", ", ", text)
    return text.strip().rstrip('"').strip()


def has_trivial_words(text) -> bool:
    text = str(text).lower()
    return any(w in text for w in TRIVIAL_WORDS)


def norm_text(text) -> str:
    text = str(text).lower().replace("’", "'")
    return " ".join(re.sub(r"[^\w\s]", "", text).split())


def tip_in_text(tip: str, user_text: str) -> bool:
    """A tip like 'Не хватает a: a new game' is wrong if the user already wrote that phrase."""
    if ":" not in tip:
        return False
    phrase = norm_text(tip.split(":", 1)[1])
    return len(phrase) > 3 and phrase in norm_text(user_text)


def tip_same_sides(tip: str) -> bool:
    """Drop pointless tips like 'to the shop → to the shop'."""
    for arrow in ("→", "->", "=>"):
        if arrow in tip:
            left, right = tip.split(arrow, 1)
            left = norm_text(left.split(":")[-1])
            right = norm_text(right)
            return bool(left) and left == right
    return False


CASUAL_ADVERB_VERBS = re.compile(
    r"\b(go|goes|going|feel|feels|feeling|look|looks|looking|sound|sounds|taste|tastes|smell|smells)\b",
    re.I)
SPELLING_WORDS = ("написани", "орфограф", "spelling", "опечат", "typo", "misspell")


def is_spelling(m: dict) -> bool:
    text = str(m.get("explanation", "")).lower()
    return any(w in text for w in SPELLING_WORDS)


def is_trivial(m: dict) -> bool:
    expl = str(m.get("explanation", "")).lower()
    if has_trivial_words(expl):
        return True
    # "it's going amazing", "I feel great": an adjective after these verbs is normal speech
    if ("наречи" in expl or "adverb" in expl) and CASUAL_ADVERB_VERBS.search(str(m.get("quote", ""))):
        return True
    return False


def parse_ai(content: str) -> dict:
    data = json.loads(content)
    reply = clean_reply(data["reply"])
    if not reply:
        raise ValueError("empty reply")
    mistakes = data.get("mistakes") or []
    if not isinstance(mistakes, list):
        mistakes = []
    real, spelling_tips = [], []
    for m in mistakes:
        if not isinstance(m, dict) or not m.get("quote") or is_trivial(m):
            continue
        if is_spelling(m):
            spelling_tips.append(str(m.get("explanation", "")).strip())
        else:
            real.append(m)
    tips = data.get("tips") or []
    if not isinstance(tips, list):
        tips = []
    tips = [str(t).strip() for t in tips + spelling_tips
            if str(t).strip() and not has_trivial_words(t)][:2]
    return {"reply": reply, "mistakes": real, "tips": tips}


def salvage(response: httpx.Response) -> dict | None:
    """Groq answers 400 when the model's JSON is broken, but includes what it wrote. Try to use it."""
    try:
        text = response.json()["error"]["failed_generation"]
    except Exception:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return parse_ai(text)
    except Exception:
        pass
    cleaned = clean_reply(text)
    if cleaned and len(cleaned) < 1500:
        return {"reply": cleaned, "mistakes": [], "tips": []}
    return None



# ---- ai: Gemini first, Groq as a fallback ----
async def call_gemini(system: str, history: list[dict], user_text: str) -> str:
    contents: list[dict] = []

    def add(role: str, text: str) -> None:
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"][0]["text"] += "\n" + text
        else:
            contents.append({"role": role, "parts": [{"text": text}]})

    for m in history:
        add("user" if m["role"] == "user" else "model", m["content"])
    add("user", user_text)
    while contents and contents[0]["role"] == "model":
        contents.pop(0)

    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        # no temperature: Gemini 3.x models do not accept sampling parameters
        "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": MAX_TOKENS},
    }
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(GEMINI_URL.format(model=GEMINI_MODEL),
                              headers={"x-goog-api-key": GEMINI_API_KEY}, json=payload)
    r.raise_for_status()
    data = r.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError(f"Gemini returned no answer: {str(data.get('promptFeedback'))[:200]}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise ValueError("Gemini returned empty text")
    return text


async def call_groq(system: str, history: list[dict], user_text: str) -> str:
    messages = [{"role": "system", "content": system}] + history + [{"role": "user", "content": user_text}]
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.8,
        "response_format": {"type": "json_object"},
    }
    if "gpt-oss" in GROQ_MODEL:
        payload["reasoning_effort"] = os.environ.get("REASONING_EFFORT", "low")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(GROQ_URL, headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, json=payload)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


async def ask_ai(u: dict, user_text: str, save_user_text: bool = True) -> dict:
    system = build_prompt(u)
    history = u["history"][-HISTORY_LIMIT:]
    style_text = STYLES.get(u.get("style"), STYLES[DEFAULT_STYLE])[1]
    # the reminder keeps the style strong even when earlier replies were written in another style
    final_user = f"{user_text}\n\n[Reply in this style (do not mention this note): {style_text}]"

    providers = []
    if GEMINI_API_KEY:
        providers.append(("gemini", call_gemini))
    if GROQ_API_KEY:
        providers.append(("groq", call_groq))
    if not providers:
        raise AIError("500")

    result = None
    last_code = "500"
    for name, fn in providers:
        for attempt in range(AI_ATTEMPTS):
            try:
                result = parse_ai(await fn(system, history, final_user))
                break
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                last_code = str(code)
                log.error("%s HTTP error (attempt %s): %s %s", name, attempt + 1, code,
                          e.response.text[:500])
                if code == 400 and name == "groq":
                    result = salvage(e.response)
                    if result:
                        break
                if code not in RETRY_STATUSES or (code == 400 and name == "gemini"):
                    break  # retrying will not help, try the next provider
            except (httpx.HTTPError, TimeoutError):
                last_code = "503"
                log.exception("%s network error", name)
            except Exception:
                last_code = "422"
                log.exception("%s bad answer", name)
            if attempt < AI_ATTEMPTS - 1:
                await asyncio.sleep(1.5)
        if result is not None:
            if name != providers[0][0]:
                log.warning("answered by the fallback provider: %s", name)
            break

    if result is None:
        raise AIError(last_code)
    result["tips"] = [t for t in result.get("tips", [])
                      if not tip_in_text(t, user_text) and not tip_same_sides(t)]

    new_history = list(history)
    if save_user_text:
        new_history.append({"role": "user", "content": user_text})
    new_history.append({"role": "assistant", "content": result["reply"]})
    await upd(u, history=new_history[-HISTORY_LIMIT:])
    return result


# ---- voice: Gemini first, Groq Whisper as a fallback ----
VOICE_PROMPT = (
    "Transcribe the speech in this audio exactly as spoken, word for word, in the language spoken. "
    "Keep grammar mistakes, slang and informal forms (wanna, gonna, ain't) exactly as said. "
    "Do not correct, translate, summarize or add anything. Output only the transcript. "
    "If there is no speech, output NO_SPEECH."
)


async def transcribe_gemini(audio: bytes) -> str:
    payload = {
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": "audio/ogg", "data": base64.b64encode(audio).decode()}},
            {"text": VOICE_PROMPT},
        ]}],
        "generationConfig": {"maxOutputTokens": 1000},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(GEMINI_URL.format(model=GEMINI_MODEL),
                              headers={"x-goog-api-key": GEMINI_API_KEY}, json=payload)
    r.raise_for_status()
    data = r.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini returned no transcript")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    return "" if text.upper().startswith("NO_SPEECH") else text


async def transcribe_groq(audio: bytes) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            WHISPER_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": WHISPER_MODEL, "response_format": "json", "temperature": "0",
                  "prompt": "Transcribe exactly what was said, word for word, including grammar "
                            "mistakes, slang and informal forms like wanna, gonna, ain't. "
                            "Do not correct or add words."},
        )
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


async def transcribe(audio: bytes) -> str:
    last_code = "500"
    providers = []
    if GEMINI_API_KEY:
        providers.append(("gemini", transcribe_gemini))
    if GROQ_API_KEY:
        providers.append(("groq", transcribe_groq))
    for name, fn in providers:
        try:
            return await fn(audio)
        except httpx.HTTPStatusError as e:
            last_code = str(e.response.status_code)
            log.error("%s transcription HTTP error: %s %s", name, last_code, e.response.text[:300])
        except (httpx.HTTPError, TimeoutError):
            last_code = "503"
            log.exception("%s transcription network error", name)
        except Exception:
            last_code = "422"
            log.exception("%s transcription failed", name)
    raise AIError(last_code)


# ---- sending answers ----
async def send_ai_result(message: Message, u: dict, result: dict) -> None:
    mistakes = result["mistakes"][:5]
    reply = html.escape(result["reply"])
    if not mistakes and u["emoji"]:
        reply += " " + random.choice(PRAISE)
    await message.answer(reply)

    if mistakes:
        blocks = []
        per_mistake = 2 if len(mistakes) == 1 else 1  # keep it short when there are several
        for m in mistakes:
            block = f"<blockquote>{html.escape(str(m['quote']))}</blockquote>\n"
            block += html.escape(str(m.get("explanation", "")))
            examples = m.get("examples") or []
            if examples:
                block += "\n" + "\n".join(f"• {html.escape(str(x))}" for x in examples[:per_mistake])
            blocks.append(block)
        await message.answer("\n\n".join(blocks))

    tips = result.get("tips") or []
    if tips and u["tips_on"]:
        await message.answer("\n".join(f"💡 <i>{html.escape(t)}</i>" for t in tips))


async def safe_ai(message: Message, u: dict, text: str, save_user_text: bool = True) -> bool:
    try:
        result = await ask_ai(u, text, save_user_text)
    except AIError as e:
        await message.answer(f"Something went wrong (error {e.code}). Try again in a minute.")
        return False
    await send_ai_result(message, u, result)
    return True


# ---- streak ----
def fire(u: dict) -> str:
    return " 🔥" if u["emoji"] else ""


def story_sig(n: int) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), str(n).encode(), hashlib.sha256).hexdigest()[:16]


def story_url(n: int) -> str:
    return f"{BASE_URL}/story?n={n}&sig={story_sig(n)}"


async def prepare_story(n: int) -> None:
    try:
        await asyncio.to_thread(story.ensure_story, n)
    except Exception:
        log.exception("story preparation failed")


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def streak_kb(n: int, counted: bool) -> InlineKeyboardMarkup:
    if counted:
        row = []
        if BASE_URL.startswith("https://"):
            row.append(InlineKeyboardButton(text="📣 Publish story", web_app=WebAppInfo(url=story_url(n))))
        row.append(InlineKeyboardButton(text="↩️ Not today", callback_data="streak:undo"))
        return InlineKeyboardMarkup(inline_keyboard=[row])
    return kb([[("✅ Count today", "streak:count")]])


async def unpin_old(bot: Bot, u: dict) -> None:
    old = u.get("pinned_msg_id")
    if not old:
        return
    try:
        await bot.unpin_chat_message(u["user_id"], message_id=old)
    except Exception:
        log.warning("unpin failed")
    await upd(u, pinned_msg_id=None)


async def announce_day(bot: Bot, u: dict, n: int) -> None:
    msg = await bot.send_message(u["user_id"], f"Day {n}{fire(u)} Today is counted.",
                                 reply_markup=streak_kb(n, counted=True))
    if u["pin_on"]:
        await unpin_old(bot, u)
        try:
            await bot.pin_chat_message(u["user_id"], msg.message_id, disable_notification=False)
            await upd(u, pinned_msg_id=msg.message_id)
        except Exception:
            log.exception("pin failed")


async def count_day(bot: Bot, u: dict, today: date) -> bool:
    if u["last_day"] == today:
        return False
    prev_streak, prev_last = u["streak"], u["last_day"]
    new = u["streak"] + 1 if prev_last == today - timedelta(days=1) else 1
    await upd(u, streak=new, best_streak=max(u["best_streak"], new), last_day=today,
              prev_streak=prev_streak, prev_last_day=prev_last, blocked_day=None)
    track(prepare_story(new))  # render the story picture in advance
    await announce_day(bot, u, new)
    return True


async def undo_day(bot: Bot, u: dict, today: date) -> bool:
    if u["last_day"] != today:
        return False
    await upd(u, streak=u["prev_streak"], last_day=u["prev_last_day"], blocked_day=today)
    await unpin_old(bot, u)
    return True


async def register_message(bot: Bot, u: dict) -> None:
    today = local_today(u)
    count = u["today_count"] + 1 if u["today_date"] == today else 1
    await upd(u, today_date=today, today_count=count)
    if (u["streak_on"] and count >= STREAK_MESSAGES and u["last_day"] != today
            and u["blocked_day"] != today):
        await count_day(bot, u, today)


async def morning_one(bot: Bot, u: dict) -> None:
    now = local_now(u)
    today = now.date()
    if u["last_morning"] == today:
        return
    hh, mm = (int(x) for x in u["morning_time"].split(":"))
    target = datetime.combine(today, dtime(hh, mm), tzinfo=get_tz(u))
    if now < target or now > target + timedelta(hours=MORNING_WINDOW_HOURS):
        return
    await upd(u, last_morning=today)  # mark first, so a failure does not repeat every minute
    if u["streak"] > 0 and u["last_day"] is not None and (today - u["last_day"]).days >= 2:
        await unpin_old(bot, u)
        await upd(u, streak=0)
        text = "Your streak reset to 0. No stress, today is a fresh start. Ready to chat?"
    elif u["last_day"] == today:
        return  # already counted today
    elif u["streak"] > 0:
        text = f"Day {u['streak']}{fire(u)} Ready to chat?"
    else:
        text = "New day! Ready to chat?"
    await bot.send_message(u["user_id"], text, reply_markup=streak_kb(0, counted=False))


async def morning_loop(bot: Bot) -> None:
    while True:
        try:
            for u in await morning_users():
                try:
                    await morning_one(bot, u)
                except Exception:
                    log.exception("morning message failed for %s", u.get("user_id"))
        except Exception:
            log.exception("morning loop error")
        await asyncio.sleep(60)


# ---- keyboards ----
def level_kb():
    return kb([
        [("A1-A2", "lvl:a"), ("B1-B2", "lvl:b"), ("C1-C2", "lvl:c")],
        [("Skip", "lvl:skip")],
    ])


def topic_kb():
    rows = [[(t, f"top:{t}") for t in TOPICS[i:i + 3]] for i in range(0, len(TOPICS), 3)]
    rows.append([("✍️ Write your own", "top:own"), ("Skip", "top:skip")])
    return kb(rows)


def style_kb():
    items = [(label, f"sty:{key}") for key, (label, _) in STYLES.items()]
    return kb([items[i:i + 2] for i in range(0, len(items), 2)])


def tz_kb():
    rows = [[(label, f"tz:{name}") for label, name in row] for row in TIMEZONES]
    rows.append([("✍️ Other", "tz:other")])
    return kb(rows)


def time_kb():
    rows = [[(t, f"tm:{t}") for t in row] for row in MORNING_TIMES]
    rows.append([("✍️ Other", "tm:other")])
    return kb(rows)


def settings_kb(u: dict):
    def mark(value: bool) -> str:
        return "✅" if value else "❌"

    style_label = STYLES.get(u["style"], STYLES[DEFAULT_STYLE])[0]
    return kb([
        [(f"{mark(u['emoji'])} Emoji", "set:emoji"), (f"{mark(u['tips_on'])} Tips 💡", "set:tips")],
        [(f"{mark(u['streak_on'])} Streak", "set:streak"), (f"{mark(u['pin_on'])} Pin streak", "set:pin")],
        [(f"{mark(u['morning_on'])} Morning message", "set:morning")],
        [(f"🗣 Explanations: {'English' if u['explain_lang'] == 'en' else 'Русский'}", "set:lang")],
        [(f"🎭 Style: {style_label}", "set:style")],
        [(f"🕒 Time zone: {tz_label(u['tz'])}", "set:tz")],
        [(f"⏰ Morning time: {u['morning_time']}", "set:time")],
    ])


# ---- handlers ----
class AccessMiddleware(BaseMiddleware):
    """Only the admin and the people the admin allowed can use the bot."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
        if chat is not None and getattr(chat, "type", "private") != "private":
            return None
        if user is None or user.id == ADMIN_ID or user.id in allowed:
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer("This is a private bot.")
        elif isinstance(event, CallbackQuery):
            await event.answer("This is a private bot.", show_alert=True)
        return None


router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
router.message.outer_middleware(AccessMiddleware())
router.callback_query.outer_middleware(AccessMiddleware())

last_message_at: dict[int, float] = {}


def cooldown_ok(uid: int) -> bool:
    if uid == ADMIN_ID:
        return True
    now = time.time()
    if now - last_message_at.get(uid, 0.0) < COOLDOWN_SECONDS:
        return False
    last_message_at[uid] = now
    return True


HELP_TEXT = (
    "I'm your English speaking-practice buddy. Write or send voice messages in English, "
    "I'll chat with you and point out mistakes below my replies.\n\n"
    "/new - start a new conversation\n"
    "/stop - end the conversation\n"
    "/style - change the vibe\n"
    "/settings - emoji, tips, streak, time zone and more\n"
    "/streak - your streak\n"
    "/help - this message"
)
ADMIN_HELP = (
    "\n\nAdmin:\n"
    "/allow ID - give someone access\n"
    "/deny ID - take access away\n"
    "/allowed - list of allowed IDs\n"
    "/setstreak N - set the streak to N days (today counts)"
)


@router.message(Command("start", "new"))
async def cmd_start(message: Message):
    u = await get_user(message.from_user.id)
    await reset_chat(u)
    await message.answer(
        "Hey! 👋 I'm here to help you practice English. Just chat with me, "
        "and I'll gently point out mistakes.",
        reply_markup=kb([[("Start a conversation", "go")]]),
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    u = await get_user(message.from_user.id)
    await reset_chat(u)
    await message.answer("Chat ended. Send /new whenever you want to talk again 👋")


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = HELP_TEXT + (ADMIN_HELP if message.from_user.id == ADMIN_ID else "")
    await message.answer(text)


@router.message(Command("style"))
async def cmd_style(message: Message):
    await message.answer("Pick the vibe of our chat:", reply_markup=style_kb())


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    u = await get_user(message.from_user.id)
    await message.answer("⚙️ Settings", reply_markup=settings_kb(u))


@router.message(Command("streak"))
async def cmd_streak(message: Message):
    u = await get_user(message.from_user.id)
    if not u["streak_on"]:
        await message.answer("The streak is turned off. You can turn it on in /settings.")
        return
    counted = u["last_day"] == local_today(u)
    text = f"Streak: {u['streak']}{fire(u)} (best: {u['best_streak']})\n"
    text += "Today is counted." if counted else \
        f"Today is not counted yet: {STREAK_MESSAGES} messages in a conversation count the day."
    await message.answer(text, reply_markup=streak_kb(u["streak"], counted))


# ---- admin commands ----
@router.message(Command("allow"))
async def cmd_allow(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Usage: /allow 123456789")
        return
    await set_allowed(int(arg), True)
    await message.answer(f"Access granted to {arg}.")


@router.message(Command("deny"))
async def cmd_deny(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Usage: /deny 123456789")
        return
    await set_allowed(int(arg), False)
    await message.answer(f"Access removed for {arg}.")


@router.message(Command("allowed"))
async def cmd_allowed(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Allowed IDs: " + (", ".join(str(i) for i in sorted(allowed)) or "nobody yet"))


@router.message(Command("setstreak"))
async def cmd_setstreak(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) > 9999:
        await message.answer("Usage: /setstreak 30")
        return
    n = int(arg)
    u = await get_user(message.from_user.id)
    today = local_today(u)
    await upd(u, streak=n, best_streak=max(u["best_streak"], n), last_day=today if n > 0 else None,
              prev_streak=max(n - 1, 0), prev_last_day=(today - timedelta(days=1)) if n > 1 else None,
              blocked_day=None)
    if n > 0:
        track(prepare_story(n))
        await announce_day(message.bot, u, n)
    else:
        await unpin_old(message.bot, u)
        await message.answer("Streak set to 0.")


# ---- callbacks ----
@router.callback_query(F.data == "go")
async def cb_go(call: CallbackQuery):
    await call.message.answer("What's your level? You can skip this, I'll adapt as we chat.",
                              reply_markup=level_kb())
    await call.answer()


@router.callback_query(F.data.startswith("lvl:"))
async def cb_level(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    value = call.data.split(":", 1)[1]
    await upd(u, level=value if value in LEVELS else None)
    await call.message.answer("Pick a topic, write your own, or skip.", reply_markup=topic_kb())
    await call.answer()


async def begin_chat(message: Message, u: dict):
    await upd(u, active=True, awaiting=None)
    await safe_ai(message, u, "Start the conversation with a short, natural opener.",
                  save_user_text=False)


@router.callback_query(F.data.startswith("top:"))
async def cb_topic(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    value = call.data.split(":", 1)[1]
    await call.answer()
    if value == "own":
        await upd(u, awaiting="topic")
        await call.message.answer("Type the topic you want to talk about 👇")
        return
    await upd(u, topic=None if value == "skip" else value)
    await begin_chat(call.message, u)


@router.callback_query(F.data.startswith("sty:"))
async def cb_style(call: CallbackQuery):
    key = call.data.split(":", 1)[1]
    await call.answer()
    if key not in STYLES:
        return
    u = await get_user(call.from_user.id)
    await upd(u, style=key)
    await call.message.answer(f"Vibe set: {STYLES[key][0]}. Just keep chatting 🙂")


@router.callback_query(F.data.startswith("set:"))
async def cb_set(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    key = call.data.split(":", 1)[1]
    toggles = {"emoji": "emoji", "tips": "tips_on", "streak": "streak_on",
               "morning": "morning_on", "pin": "pin_on"}
    await call.answer()
    if key in toggles:
        field = toggles[key]
        await upd(u, **{field: not u[field]})
    elif key == "lang":
        await upd(u, explain_lang="en" if u["explain_lang"] == "ru" else "ru")
    elif key == "style":
        await call.message.answer("Pick the vibe of our chat:", reply_markup=style_kb())
        return
    elif key == "tz":
        await call.message.answer("Pick your time zone or type your own:", reply_markup=tz_kb())
        return
    elif key == "time":
        await call.message.answer("When should the morning message come?", reply_markup=time_kb())
        return
    try:
        await call.message.edit_reply_markup(reply_markup=settings_kb(u))
    except Exception:
        pass


@router.callback_query(F.data.startswith("tz:"))
async def cb_tz(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    value = call.data.split(":", 1)[1]
    await call.answer()
    if value == "other":
        await upd(u, awaiting="tz")
        await call.message.answer("Type a time zone like Europe/Berlin or an offset like +3 👇")
        return
    name = parse_tz(value)
    if name:
        await upd(u, tz=name)
        await call.message.answer(f"Time zone set: {tz_label(name)}")


@router.callback_query(F.data.startswith("tm:"))
async def cb_time(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    value = call.data.split(":", 1)[1]
    await call.answer()
    if value == "other":
        await upd(u, awaiting="time")
        await call.message.answer("Type the time like 09:30 👇")
        return
    hhmm = parse_hhmm(value)
    if hhmm:
        await upd(u, morning_time=hhmm)
        await call.message.answer(f"Morning message time set: {hhmm}")


@router.callback_query(F.data == "streak:count")
async def cb_streak_count(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    today = local_today(u)
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if u["last_day"] != today:
        await upd(u, blocked_day=None)
        await count_day(call.bot, u, today)


@router.callback_query(F.data == "streak:undo")
async def cb_streak_undo(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    ok = await undo_day(call.bot, u, local_today(u))
    await call.answer("Done" if ok else "Nothing to undo")
    if ok:
        try:
            await call.message.edit_text(f"Okay, today is not counted. Streak: {u['streak']}.",
                                         reply_markup=None)
        except Exception:
            pass


# ---- messages ----
@router.message(F.voice)
async def on_voice(message: Message):
    uid = message.from_user.id
    if not cooldown_ok(uid):
        return
    u = await get_user(uid)
    if u["awaiting"]:
        await message.answer("Please type that as text 👇")
        return
    if not u["active"]:
        await message.answer("Send /start to begin 🙂")
        return
    if message.voice.duration > MAX_VOICE_SECONDS:
        await message.answer("Please keep voice messages under 60 seconds 🎙️")
        return
    try:
        buf = BytesIO()
        await message.bot.download(message.voice, destination=buf)
        text = await transcribe(buf.getvalue())
    except AIError as e:
        await message.answer(f"Something went wrong (error {e.code}). Try again in a minute.")
        return
    except Exception:
        log.exception("Voice download failed")
        await message.answer("Something went wrong (error 500). Try again in a minute.")
        return
    if not text:
        await message.answer("I couldn't hear anything. Try again? 🎙️")
        return
    await message.answer(f"🎙 <i>{html.escape(text)}</i>")
    if await safe_ai(message, u, text):
        await register_message(message.bot, u)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    uid = message.from_user.id
    if not cooldown_ok(uid):
        return
    u = await get_user(uid)
    awaiting = u["awaiting"]
    if awaiting == "topic":
        await upd(u, topic=message.text.strip()[:100])
        await begin_chat(message, u)
        return
    if awaiting == "tz":
        name = parse_tz(message.text)
        if name:
            await upd(u, tz=name, awaiting=None)
            await message.answer(f"Time zone set: {tz_label(name)}")
        else:
            await message.answer("I don't know that one. Try Europe/Berlin or an offset like +3.")
        return
    if awaiting == "time":
        hhmm = parse_hhmm(message.text)
        if hhmm:
            await upd(u, morning_time=hhmm, awaiting=None)
            await message.answer(f"Morning message time set: {hhmm}")
        else:
            await message.answer("Please type the time like 09:30.")
        return
    if not u["active"]:
        await message.answer("Send /start to begin 🙂")
        return
    if await safe_ai(message, u, message.text):
        await register_message(message.bot, u)


@router.message(F.sticker | F.photo | F.video | F.animation | F.document | F.video_note | F.audio)
async def on_media(message: Message):
    uid = message.from_user.id
    u = await get_user(uid)
    if not u["active"] or not cooldown_ok(uid):
        return
    kind = message.content_type.value
    note = (f"[The user sent a {kind}. React to it in English like it is a meme or a funny situation, "
            "keep it short, and at the end say you can't see or read it. Return an empty list for mistakes.]")
    await safe_ai(message, u, note, save_user_text=False)


# ---- web server (webhook, health check for the pinger, story page) ----
STORY_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#1c1c1e;color:#fff;font-family:-apple-system,Segoe UI,Roboto,sans-serif;
text-align:center;padding:24px;box-sizing:border-box}</style></head>
<body><div id="m">Opening the story editor...</div>
<script>
var tg = window.Telegram.WebApp;
tg.ready();
try {
  tg.shareToStory(__MEDIA__, {text: __TEXT__});
  document.getElementById("m").textContent = "The story editor is open. You can close this window.";
} catch (e) {
  document.getElementById("m").textContent = "Your Telegram app cannot share to stories. Please update it.";
}
</script></body></html>"""


async def health(_request):
    return web.Response(text="ok")


async def story_page(request):
    try:
        n = int(request.query.get("n", ""))
    except ValueError:
        return web.Response(status=400)
    sig = request.query.get("sig", "")
    if not 0 <= n <= 9999 or not hmac.compare_digest(sig, story_sig(n)):
        return web.Response(status=403)
    try:
        path, _kind = await asyncio.wait_for(asyncio.to_thread(story.ensure_story, n), timeout=150)
    except Exception:
        log.exception("story page failed")
        return web.Response(text="Could not prepare the story, please try again.", status=500)
    media = f"{BASE_URL}/media/{os.path.basename(path)}?sig={sig}"
    text = f"{n} {'day' if n == 1 else 'days'} in a row 🔥"
    page = STORY_HTML.replace("__MEDIA__", json.dumps(media)).replace("__TEXT__", json.dumps(text))
    return web.Response(text=page, content_type="text/html")


async def story_media(request):
    name = request.match_info["name"]
    m = re.fullmatch(r"story_(\d{1,4})\.(mp4|png)", name)
    if not m:
        return web.Response(status=404)
    sig = request.query.get("sig", "")
    if not hmac.compare_digest(sig, story_sig(int(m.group(1)))):
        return web.Response(status=403)
    path = os.path.join(story.OUT_DIR, name)
    if not os.path.exists(path):
        return web.Response(status=404)
    return web.FileResponse(path)


async def on_startup(bot: Bot):
    await init_db()
    if ADMIN_ID == 0:
        log.warning("ADMIN_ID is not set: nobody can use the bot")
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", secret_token=WEBHOOK_SECRET,
                              drop_pending_updates=True)
        log.info("Webhook set")
    track(morning_loop(bot))


async def on_shutdown(bot: Bot):
    if pool is not None:
        await pool.close()


def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/story", story_page)
    app.router.add_get("/media/{name}", story_media)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))


if __name__ == "__main__":
    main()
