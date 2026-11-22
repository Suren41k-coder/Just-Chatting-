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
import urllib.parse
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
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, WebAppInfo)
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

GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts")
GEMINI_TTS_LEGACY_MODEL = os.environ.get("GEMINI_TTS_LEGACY_MODEL", "gemini-3.1-flash-tts-preview")
STATS_KEEP_DAYS = 200
GOAL_REPORT_HOUR = 21  # local hour of the final goal report on the last day
NUDGE_TICK_SECONDS = 30
GEMINI_VOICES = ["Kore", "Puck", "Charon", "Aoede"]
EDGE_VOICES = ["en-US-AvaNeural", "en-US-AndrewNeural", "en-US-AriaNeural", "en-US-GuyNeural"]
VOICE_SAMPLE = "Hey, it's good to hear from you. So, what did you get up to today?"
VOICE_MODE_LABELS = {"off": "off", "button": "button", "always": "always"}

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
FRIEND_STYLE = (
    "Your personality is described above. Let it show in how you write, but keep replies natural, "
    "never over-the-top, and never turn into a caricature."
)

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
    "Check ONLY the user's last message: quote only words that appear in it, and never repeat a correction "
    "for something they wrote in an earlier message.\n"
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
    # stage 4
    "hints_on": True, "hint_min": 5, "hint_sent": False, "hint_ts": None, "silence_end": False,
    "last_user_ts": None, "last_bot_ts": None, "remind_on": True, "remind1_h": 2, "remind2_h": 24,
    "remind_sent": 0, "remind_ts": None, "quiet_from": "23:00", "quiet_to": "09:00",
    "voice_mode": "off", "tts_voice": "gemini:Kore", "weak_on": True, "last_weak": None,
    "stats": {}, "goals": [], "pending_goal": None, "last_hint": None, "last_hint_ts": None,
    "pending_friend": None, "main_thread": None, "cmd_thread": None,
}
COLUMN_TYPES = {
    "level": "text", "topic": "text", "style": "text", "explain_lang": "text", "tz": "text",
    "morning_time": "text", "emoji": "boolean", "streak_on": "boolean", "morning_on": "boolean",
    "tips_on": "boolean", "pin_on": "boolean", "streak": "integer", "best_streak": "integer",
    "last_day": "date", "prev_streak": "integer", "prev_last_day": "date", "blocked_day": "date",
    "today_date": "date", "today_count": "integer", "last_morning": "date",
    "pinned_msg_id": "bigint", "history": "jsonb", "active": "boolean", "awaiting": "text",
    "hints_on": "boolean", "hint_min": "integer", "hint_sent": "boolean", "hint_ts": "double precision",
    "silence_end": "boolean", "last_user_ts": "double precision", "last_bot_ts": "double precision",
    "remind_on": "boolean", "remind1_h": "integer", "remind2_h": "integer", "remind_sent": "integer",
    "remind_ts": "double precision", "quiet_from": "text", "quiet_to": "text", "voice_mode": "text",
    "tts_voice": "text", "weak_on": "boolean", "last_weak": "date", "stats": "jsonb", "goals": "jsonb",
    "pending_goal": "jsonb", "last_hint": "text", "last_hint_ts": "double precision",
    "pending_friend": "jsonb", "main_thread": "bigint", "cmd_thread": "bigint",
}


def track(coro) -> None:
    """Runs a coroutine in the background and keeps a reference so it is not garbage collected."""
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


# ---- topics and friends ----
class Typing:
    """Keeps a chat action (typing / record_voice) alive in a chat or topic while the block runs."""

    def __init__(self, bot, chat_id: int, action: str = "typing", thread_id: int | None = None):
        self.bot, self.chat_id, self.action, self.thread_id = bot, chat_id, action, thread_id
        self.task = None

    async def _loop(self) -> None:
        kw = {"message_thread_id": self.thread_id} if self.thread_id else {}
        while True:
            try:
                await self.bot.send_chat_action(self.chat_id, self.action, **kw)
            except Exception:
                pass
            await asyncio.sleep(4.5)  # Telegram shows an action for about 5 seconds

    async def __aenter__(self):
        self.task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc):
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        return False


def thread_of(message) -> int | None:
    return getattr(message, "message_thread_id", None)


async def say(message, text: str, thread_id: int | None = None, **kw):
    """Sends text to the chat of `message`, into the same topic (or the given one)."""
    tid = thread_id if thread_id is not None else thread_of(message)
    if tid:
        kw["message_thread_id"] = tid
    return await message.bot.send_message(message.chat.id, text, **kw)


mem_friends: dict[int, dict] = {}  # used when there is no database
mem_friend_seq = 0


def friend_row(row) -> dict:
    f = dict(row)
    f["history"] = f.get("history") or []
    return f


async def friends_of(uid: int) -> list[dict]:
    if pool is not None:
        try:
            rows = await pool.fetch("SELECT * FROM friends WHERE user_id = $1 ORDER BY id", uid)
            return [friend_row(r) for r in rows]
        except Exception:
            log.exception("friends_of failed")
    return [f for f in mem_friends.values() if f["user_id"] == uid]


async def friend_by_thread(uid: int, thread_id: int | None) -> dict | None:
    if not thread_id:
        return None
    for f in await friends_of(uid):
        if f["thread_id"] == thread_id:
            return f
    return None


async def friend_here(message) -> dict | None:
    return await friend_by_thread(message.from_user.id, thread_of(message))


def topics_on(u: dict) -> bool:
    """The Main and Commands topics exist (made by /start)."""
    return bool(u.get("main_thread") and u.get("cmd_thread"))


def main_kw(u: dict) -> dict:
    """Where the bot writes by itself (streak, morning message, reminders, hints): the Main topic."""
    return {"message_thread_id": u["main_thread"]} if topics_on(u) else {}


async def route(message, u: dict) -> tuple[str, dict | None]:
    """Which chat a message belongs to: 'friend', 'main', 'cmd' (Commands topic) or 'other' (any stray topic)."""
    tid = thread_of(message)
    friend = await friend_by_thread(u["user_id"], tid)
    if friend:
        return "friend", friend
    if not topics_on(u) or not tid or tid == u["main_thread"]:
        return "main", None
    if tid == u["cmd_thread"]:
        return "cmd", None
    return "other", None


WRONG_TOPIC = {
    "cmd": "This topic is for commands. Let's chat in Main, open it in the sidebar 👉",
    "other": "Let's chat in Main, open it in the sidebar 👉 (commands live in Commands).",
}
CHAT_COMMANDS = {"new", "stop", "hint"}  # allowed inside Main and friend topics; the rest live in Commands


async def topic_alive(bot, chat_id: int, thread_id: int) -> bool:
    try:
        await bot.send_chat_action(chat_id, "typing", message_thread_id=thread_id)
        return True
    except Exception as e:
        return "thread" not in str(e).lower()  # only a missing topic counts, not a network hiccup


async def ensure_topics(bot, u: dict) -> None:
    """Creates the Main and Commands topics (or makes them again if one was deleted)."""
    for field, name in (("main_thread", "Main"), ("cmd_thread", "Commands")):
        if u.get(field) and not await topic_alive(bot, u["user_id"], u[field]):
            await upd(u, **{field: None})
        if u.get(field):
            continue
        try:
            topic = await bot.create_forum_topic(chat_id=u["user_id"], name=name)
        except Exception:
            log.warning("could not create the %s topic (are topics on in BotFather?)", name)
            return
        await upd(u, **{field: topic.message_thread_id})


async def add_friend(uid: int, name: str, about: str, voice: str, accent: str | None, thread_id: int) -> dict:
    global mem_friend_seq
    if pool is not None:
        try:
            row = await pool.fetchrow(
                "INSERT INTO friends (user_id, name, about, voice, accent, thread_id, history) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
                uid, name, about, voice, accent, thread_id, [])
            return friend_row(row)
        except Exception:
            log.exception("add_friend failed")
    mem_friend_seq += 1
    f = {"id": mem_friend_seq, "user_id": uid, "name": name, "about": about, "voice": voice,
         "accent": accent, "thread_id": thread_id, "history": [], "mem": True,
         "last_user_ts": None, "last_bot_ts": None, "hint_sent": False, "hint_ts": None}
    mem_friends[f["id"]] = f
    return f


async def upd_friend(f: dict, **fields) -> None:
    f.update(fields)
    if pool is None or f.get("mem") or not fields:
        return
    names = list(fields)
    sets = ", ".join(f"{name} = ${i + 2}" for i, name in enumerate(names))
    try:
        await pool.execute(f"UPDATE friends SET {sets} WHERE id = $1", f["id"], *[fields[n] for n in names])
    except Exception:
        log.exception("upd_friend failed")


async def del_friend(f: dict) -> None:
    mem_friends.pop(f["id"], None)
    if pool is not None and not f.get("mem"):
        try:
            await pool.execute("DELETE FROM friends WHERE id = $1", f["id"])
        except Exception:
            log.exception("del_friend failed")


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
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS friends (id bigserial PRIMARY KEY, user_id bigint, name text, "
                "about text, voice text, accent text, thread_id bigint, history jsonb DEFAULT '[]'::jsonb)")
            for col, typ in (("last_user_ts", "double precision"), ("last_bot_ts", "double precision"),
                             ("hint_sent", "boolean"), ("hint_ts", "double precision")):
                await conn.execute(f"ALTER TABLE friends ADD COLUMN IF NOT EXISTS {col} {typ}")
            for table in ("users", "allowed", "friends"):  # keep the Supabase API closed for every table
                try:
                    await conn.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
                except Exception:
                    log.warning("could not enable RLS on %s", table)
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
            value = list(default) if isinstance(default, list) else (dict(default) if isinstance(default, dict) else default)
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
    await upd(u, level=None, topic=None, history=[], awaiting=None, active=False,
              hint_sent=False, silence_end=False, pending_goal=None, pending_friend=None)


def build_prompt(u: dict, friend: dict | None = None) -> str:
    level = LEVELS.get(u["level"]) if u["level"] else None
    explain_lang = "English" if u.get("explain_lang") == "en" else "Russian"
    style = STYLES.get(u.get("style"), STYLES[DEFAULT_STYLE])[1]
    topic = f"Topic to talk about: {u['topic']} (stay on it unless the user changes it)." if u["topic"] \
        else "Topic: free talk."
    intro = 'You are "Just Chatting", a chat partner for English speaking practice in Telegram. '
    if friend:
        topic = "Topic: free talk."
        style = FRIEND_STYLE
        intro = (f"You are {friend['name']}, a person the learner chats with in Telegram to practice English. "
                 f"Who you are and how you behave (written by the learner, may be in any language): "
                 f"{friend['about']}\n"
                 "Stay in this character in every reply: background, interests, humor and way of speaking. ")
    return (
        intro +
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
        "Never suggest formal alternatives for informal words like wanna or gonna. "
        "Tips, like mistakes, are ONLY about the user's last message: never bring back a tip or a correction "
        "from an earlier message."
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


TIP_STOPWORDS = {"the", "an", "to", "on", "in", "of", "my", "you", "it", "is", "am", "are", "and", "or", "for",
                 "at", "with", "me", "he", "she", "we", "they", "do", "be", "so", "as", "by", "your", "his", "her"}


def content_words(text) -> set[str]:
    return {w for w in norm_text(text).split() if len(w) >= 2 and w not in TIP_STOPWORDS}


def tip_repeats_old(tip: str, user_text: str, history: list) -> bool:
    """A tip that has nothing to do with the current message but matches an earlier message of this
    chat is an old tip coming back."""
    if ":" not in tip:
        return False
    words = content_words(tip.split(":", 1)[1])
    if not words or words & content_words(user_text):
        return False
    return any(words & content_words(m.get("content", ""))
               for m in history if m.get("role") == "user")


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


async def ask_ai(u: dict, user_text: str, save_user_text: bool = True, friend: dict | None = None) -> dict:
    system = build_prompt(u, friend)
    history = (friend or u)["history"][-HISTORY_LIMIT:]
    if friend:
        style_text = f"Stay fully in character as {friend['name']}: {friend['about'][:300]}"
    else:
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
                      if not tip_in_text(t, user_text) and not tip_same_sides(t)
                      and not tip_repeats_old(t, user_text, history)]
    # a mistake must quote words from this very message, otherwise the model is repeating an old one
    result["mistakes"] = [m for m in result.get("mistakes", [])
                          if norm_text(m["quote"]) and norm_text(m["quote"]) in norm_text(user_text)]

    new_history = list(history)
    if save_user_text:
        new_history.append({"role": "user", "content": user_text})
    new_history.append({"role": "assistant", "content": result["reply"]})
    if friend:
        await upd_friend(friend, history=new_history[-HISTORY_LIMIT:])
    else:
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
async def send_ai_result(message: Message, u: dict, result: dict, friend: dict | None = None,
                         thread_id: int | None = None) -> None:
    tid = thread_id if thread_id is not None else thread_of(message)
    mistakes = result["mistakes"][:5]
    voice_sent = False
    if u["voice_mode"] == "always":
        voice_sent = await send_voice_reply(message.bot, u, message.chat.id, result["reply"], friend, tid)
    if not voice_sent:  # text goes out unless the voice reply was sent (or voice failed)
        reply = html.escape(result["reply"])
        if not mistakes and u["emoji"]:
            reply += " " + random.choice(PRAISE)
        markup = kb([[("🔊 Listen", "tts:go")]]) if u["voice_mode"] == "button" else None
        await say(message, reply, tid, reply_markup=markup)

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
        await say(message, "\n\n".join(blocks), tid)

    tips = result.get("tips") or []
    if tips and u["tips_on"]:
        await say(message, "\n".join(f"💡 <i>{html.escape(t)}</i>" for t in tips), tid)
    if friend:  # a friend's silence is tracked separately from the main chat
        await upd_friend(friend, last_bot_ts=time.time(), hint_sent=False)
    else:
        await upd(u, last_bot_ts=time.time(), hint_sent=False)


async def safe_ai(message: Message, u: dict, text: str, save_user_text: bool = True,
                  friend: dict | None = None, thread_id: int | None = None):
    tid = thread_id if thread_id is not None else thread_of(message)
    action = "record_voice" if u["voice_mode"] == "always" else "typing"
    try:
        async with Typing(message.bot, message.chat.id, action, tid):
            result = await ask_ai(u, text, save_user_text, friend)
    except AIError as e:
        await say(message, f"Something went wrong (error {e.code}). Try again in a minute.", tid)
        return None
    await send_ai_result(message, u, result, friend, tid)
    return result


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


def kb(rows: list[list[tuple]]) -> InlineKeyboardMarkup:
    """Rows of (text, callback_data) or (text, callback_data, style); style: success=green, danger=red, primary=blue."""
    def button(t, d, style=None):
        extra = {"style": style} if style else {}
        return InlineKeyboardButton(text=t, callback_data=d, **extra)

    return InlineKeyboardMarkup(inline_keyboard=[[button(*item) for item in row] for row in rows])


def streak_kb(n: int, counted: bool) -> InlineKeyboardMarkup:
    if counted:
        row = []
        if BASE_URL.startswith("https://"):
            row.append(InlineKeyboardButton(text="📣 Publish story", web_app=WebAppInfo(url=story_url(n))))
        row.append(InlineKeyboardButton(text="↩️ Not today", callback_data="streak:undo"))
        file_row = [InlineKeyboardButton(text="💾 Get file", callback_data="streak:file")]
        return InlineKeyboardMarkup(inline_keyboard=[row, file_row])
    return kb([[("✅ Count today", "streak:count")]])


async def send_story_file(bot: Bot, chat_id: int, n: int, thread_id: int | None = None) -> None:
    """Sends the rendered story to the chat, so it can be posted as a story by hand."""
    path, kind = await asyncio.to_thread(story.ensure_story, n)
    caption = f"{n} {'day' if n == 1 else 'days'} in a row. Save it and post it as a story."
    kw = {"message_thread_id": thread_id} if thread_id else {}
    if kind == "mp4":
        await bot.send_video(chat_id, FSInputFile(path), caption=caption, supports_streaming=True, **kw)
    else:
        await bot.send_photo(chat_id, FSInputFile(path), caption=caption, **kw)


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
                                 reply_markup=streak_kb(n, counted=True), **main_kw(u))
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
    note = weak_note(u)
    if note:
        text += "\n" + note
        await upd(u, last_weak=today)
    await bot.send_message(u["user_id"], text, reply_markup=streak_kb(0, counted=False), **main_kw(u))


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


# =====================================================================
# stage 4: stats, weak spots, goals, hints, reminders, voice replies
# =====================================================================
CATEGORY_NAMES = {"a": "articles", "t": "tenses", "p": "prepositions", "w": "word order",
                  "v": "verb forms", "s": "spelling", "c": "word choice", "o": "other"}


def categorize(text) -> str:
    t = str(text).lower()
    if re.search(r"(не хватает|лишн|missing|extra|нужен|нужна)\s+(артикл\w*|a|an|the)\b", t) or "артикл" in t or "article" in t:
        return "a"
    for code, keys in (("s", ("написани", "орфограф", "опечат", "spelling", "typo")),
                       ("p", ("предлог", "preposition")),
                       ("w", ("порядок слов", "word order")),
                       ("t", ("врем", "tense", "present perfect", "past simple", "future")),
                       ("v", ("форм", "глагол", "verb", "agreement", "согласован")),
                       ("c", ("слово", "словоупотреб", "word choice", "естественн", "natural"))):
        if any(k in t for k in keys):
            return code
    return "o"


# ---- generic "ask the AI for JSON" helper (used by goals, hints, nudges) ----
def ai_providers():
    out = []
    if GEMINI_API_KEY:
        out.append(("gemini", call_gemini))
    if GROQ_API_KEY:
        out.append(("groq", call_groq))
    return out


def loads_loose(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", str(text).strip(), flags=re.M).strip()
    return json.loads(text)


async def ai_json(system: str, user_text: str, history: list | None = None):
    history = history or []
    for name, fn in ai_providers():
        for attempt in range(AI_ATTEMPTS):
            try:
                return loads_loose(await fn(system, history, user_text))
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                log.error("%s json call failed: %s %s", name, code, e.response.text[:300])
                if code not in RETRY_STATUSES or (code == 400 and name == "gemini"):
                    break
            except Exception:
                log.exception("%s json call failed", name)
            if attempt < AI_ATTEMPTS - 1:
                await asyncio.sleep(1.5)
    return None


async def all_users() -> list[dict]:
    if pool is None:
        return list(mem_users.values())
    try:
        rows = await pool.fetch("SELECT * FROM users")
        return [merge_row(r["user_id"], dict(r)) for r in rows]
    except Exception:
        log.exception("all_users failed")
        return []


# ---- statistics ----
def day_key(u: dict) -> str:
    return local_today(u).isoformat()


async def record_stats(u: dict, result: dict, voice: bool) -> None:
    stats = dict(u.get("stats") or {})
    d = dict(stats.get(day_key(u)) or {"m": 0, "v": 0, "c": 0, "e": 0, "t": 0, "k": {}})
    d["m"] += 1
    if voice:
        d["v"] += 1
    mistakes = result.get("mistakes") or []
    tips = result.get("tips") or []
    if not mistakes:
        d["c"] += 1
    d["e"] += len(mistakes)
    d["t"] += len(tips)
    k = dict(d.get("k") or {})
    for m in mistakes:
        c = categorize(m.get("explanation", ""))
        k[c] = k.get(c, 0) + 1
    for t in tips:
        c = categorize(t)
        k[c] = k.get(c, 0) + 1
    d["k"] = k
    stats[day_key(u)] = d
    if len(stats) > STATS_KEEP_DAYS:
        for old in sorted(stats)[:-STATS_KEEP_DAYS]:
            del stats[old]
    await upd(u, stats=stats)


def period_sum(stats: dict, end: date, n: int) -> dict:
    out = {"m": 0, "days": 0, "c": 0, "e": 0}
    for i in range(n):
        d = stats.get((end - timedelta(days=i)).isoformat())
        if d and d.get("m", 0) > 0:
            out["m"] += d["m"]
            out["c"] += d.get("c", 0)
            out["e"] += d.get("e", 0)
            out["days"] += 1
    return out


def top_categories(stats: dict, end: date, n: int) -> list[tuple[str, int]]:
    total: dict[str, int] = {}
    for i in range(n):
        d = stats.get((end - timedelta(days=i)).isoformat())
        for code, cnt in ((d or {}).get("k") or {}).items():
            total[code] = total.get(code, 0) + cnt
    return sorted(total.items(), key=lambda kv: -kv[1])


def weak_note(u: dict) -> str | None:
    if not u["weak_on"]:
        return None
    today = local_today(u)
    if u["last_weak"] is not None and (today - u["last_weak"]).days < 3:
        return None
    top = [(c, n) for c, n in top_categories(u["stats"], today, 14) if c != "o" and n >= 3]
    if not top:
        return None
    code, n = top[0]
    return f"Lately your most common slip: {CATEGORY_NAMES[code]} ({n} times). Keep an eye on it today."


# ---- goals ----
def item_attempts(it: dict) -> int:
    return it["c"] + it["w"]


def item_pct(it: dict):
    att = item_attempts(it)
    if att == 0:
        return None
    acc = it["c"] / att
    return round(100 * min(1, it["c"] / 3) * min(1, len(it["days"]) / 2) * acc)


def item_status(it: dict) -> str:
    att = item_attempts(it)
    if att == 0:
        return "unused"
    acc = it["c"] / att
    if it["c"] >= 3 and len(it["days"]) >= 2 and acc >= 0.8:
        return "mastered"
    if att >= 2 and acc < 0.5:
        return "hard"
    return "progress"


def goal_overall(g: dict):
    pcts = [p for p in (item_pct(i) for i in g["items"]) if p is not None]
    return round(sum(pcts) / len(pcts)) if pcts else None


def active_goals(u: dict) -> list[dict]:
    return [g for g in u.get("goals") or [] if not g.get("done")]


def goal_progress_line(u: dict, g: dict) -> str:
    today = local_today(u)
    start = date.fromisoformat(g["start"])
    end = date.fromisoformat(g["end"])
    total = (end - start).days + 1
    day_n = min(max((today - start).days + 1, 1), total)
    ov = goal_overall(g)
    used = sum(1 for i in g["items"] if item_attempts(i) > 0)
    return (f"🎯 {g['name']}: {'no data yet' if ov is None else str(ov) + '%'} "
            f"(day {day_n} of {total}, used {used} of {len(g['items'])} items)")


def goal_report(g: dict) -> list[str]:
    ov = goal_overall(g)
    used = [i for i in g["items"] if item_attempts(i) > 0]
    groups = {"mastered": [], "progress": [], "hard": [], "unused": []}
    for it in g["items"]:
        groups[item_status(it)].append(it)

    def line(it):
        p = item_pct(it)
        base = f"• {it['t']}"
        if p is None:
            return html.escape(base + (f" (only mentioned {it['m']}x)" if it["m"] else ""))
        txt = html.escape(f"{base}: {p}% ({it['c']} right, {it['w']} wrong)")
        if it["ex"]:
            txt += f"\n   <i>{html.escape(it['ex'][-1])}</i>"
        return txt

    head = (f"🎯 <b>Goal report: {html.escape(g['name'])}</b>\n{g['start']} to {g['end']}\n"
            f"Overall: {'not enough data' if ov is None else str(ov) + '%'} "
            f"(used {len(used)} of {len(g['items'])} items)\n"
            "Only your own uses count. Mentions and uses right after my hint do not.")
    parts = [head]
    titles = (("mastered", "✅ Mastered"), ("progress", "📈 In progress"),
              ("hard", "⚠️ Needs work"), ("unused", "⏳ Not used yet (this does not mean you do not know it)"))
    for key, title in titles:
        if groups[key]:
            body = "\n".join(line(i) for i in groups[key])
            parts.append(f"<b>{title} ({len(groups[key])})</b>\n{body}")
    prompted = sum(i["p"] for i in g["items"])
    if prompted:
        parts.append(f"Uses after my hints (not counted): {prompted}")
    # split into messages under the Telegram limit, only between lines (tags stay balanced)
    msgs, cur = [], ""
    for ln in "\n\n".join(parts).split("\n"):
        if len(cur) + len(ln) + 1 > 3800 and cur:
            msgs.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    msgs.append(cur)
    return msgs


STOP_WORDS = {"to", "a", "an", "the", "sb", "sth", "smb", "something", "somebody", "someone", "one's", "your"}


def stem(w: str) -> str:
    w = w.lower()
    for suf in ("ing", "ed", "es", "s", "ly"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def lex_in(item: str, text: str) -> bool:
    words = [stem(w) for w in re.findall(r"[A-Za-z']+", item) if w.lower() not in STOP_WORDS]
    toks = [stem(w) for w in re.findall(r"[A-Za-z']+", text)]
    if not words or not toks:
        return False
    return all(any(t.startswith(w) or w.startswith(t) for t in toks if len(t) >= 2) for w in words)


ASSESS_PROMPT = (
    "You are a strict language-learning assessor. A learner chats in English with a friendly partner. "
    "You get the learner's latest message, the partner's previous message and a list of study items. "
    "For each item that appears in the learner's message in any form (any tense or inflection, a phrasal "
    "verb with words in between, a grammar construction actually used), decide the outcome:\n"
    '- "correct": the learner used it on their own, naturally and correctly in context (form AND meaning).\n'
    '- "wrong": the learner clearly tried to use it but got that item wrong (form, meaning or construction).\n'
    '- "mentioned": the item is only quoted, asked about, defined, translated or listed, not used to say '
    "something of their own.\n"
    "Leave out items that are absent. Do not guess: when unsure, leave the item out.\n"
    '"prompted" is true if the partner\'s previous message or the shown hint contained the same item, or the '
    "learner echoes or closely follows the partner's wording or the hint, otherwise false.\n"
    '"example" is the learner\'s own phrase with the item (max 120 characters).\n'
    'Return ONLY JSON: {"results":[{"id":number,"outcome":"correct|wrong|mentioned","prompted":boolean,'
    '"example":string}]}'
)


def last_assistant(u: dict) -> str:
    for m in reversed(u.get("history") or []):
        if m.get("role") == "assistant":
            return m.get("content", "")
    return ""


async def track_goals(uid: int, text: str, prev_bot: str) -> None:
    try:
        u = await get_user(uid)
        today = local_today(u).isoformat()
        goals = [g for g in active_goals(u) if g["start"] <= today <= g["end"]]
        if not goals or len(text.split()) < 2:
            return
        flat = [(g["id"], it["id"], it["t"], it["k"]) for g in goals for it in g["items"]]
        hint = u.get("last_hint") or ""
        if time.time() - (u.get("last_hint_ts") or 0) > 900:
            hint = ""  # an old hint is no longer in the learner's head
        payload = {"learner_message": text[:600], "partner_previous_message": prev_bot[:600],
                   "hint_shown_to_learner": hint[:300],
                   "items": [{"id": i, "item": t, "kind": k} for i, (_, _, t, k) in enumerate(flat)]}
        data = await ai_json(ASSESS_PROMPT, json.dumps(payload, ensure_ascii=False))
        if not data or not isinstance(data.get("results"), list):
            return
        fresh = await get_user(uid)  # the goals may have changed while the AI was thinking
        h = hashlib.md5(norm_text(text).encode()).hexdigest()[:10]
        changed = False
        for r in data["results"]:
            try:
                gid, iid, item_text, _kind = flat[int(r["id"])]
            except Exception:
                continue
            outcome = r.get("outcome")
            if outcome not in ("correct", "wrong", "mentioned"):
                continue
            g = next((x for x in fresh["goals"] if x["id"] == gid and not x.get("done")), None)
            it = next((x for x in (g or {}).get("items", []) if x["id"] == iid), None)
            if it is None or h in it["seen"]:
                continue  # the same message is never counted twice
            prompted = bool(r.get("prompted")) or lex_in(item_text, prev_bot) or (bool(hint) and lex_in(item_text, hint))
            example = str(r.get("example") or "")[:120]
            if prompted:
                it["p"] += 1
            elif outcome == "correct":
                it["c"] += 1
                if today not in it["days"]:
                    it["days"].append(today)
                if example:
                    it["ex"] = (it["ex"] + [example])[-3:]
            elif outcome == "wrong":
                it["w"] += 1
                if example:
                    it["ex"] = (it["ex"] + ["(wrong) " + example])[-3:]
            else:
                it["m"] += 1
            it["seen"] = (it["seen"] + [h])[-200:]
            changed = True
        if changed:
            await upd(fresh, goals=fresh["goals"])
    except Exception:
        log.exception("track_goals failed")


GOAL_PARSE_PROMPT = (
    "Extract study items from the learner's text. Items can be words, expressions, phrasal verbs, grammar "
    "constructions or tenses, or anything else they want to practice. Return ONLY JSON: "
    '{"items":[{"t":string,"k":"word|phrase|phrasal|grammar|other"}]}. '
    "Keep each item short, in English (for grammar use a short name like 'Present Perfect'). "
    "Merge duplicates, at most 40 items."
)


def fallback_items(text: str) -> list[dict]:
    parts = [p.strip(" -•*\t") for p in re.split(r"[\n;,]+", text) if p.strip(" -•*\t")]
    return [{"t": p[:60], "k": "phrase"} for p in parts[:40]]


def new_goal(u: dict, items: list[dict], days: int) -> dict:
    today = local_today(u)
    gid = max([g["id"] for g in (u.get("goals") or [])] + [0]) + 1
    return {"id": gid, "name": f"Goal #{gid}", "start": today.isoformat(),
            "end": (today + timedelta(days=days - 1)).isoformat(), "done": False,
            "items": [{"id": i + 1, "t": it["t"], "k": it.get("k", "other"), "c": 0, "w": 0, "m": 0,
                       "p": 0, "days": [], "ex": [], "seen": []} for i, it in enumerate(items)]}


# ---- stats text and widget ----
def build_stats(u: dict) -> dict:
    today = local_today(u)
    stats = u["stats"]
    days = [stats.get((today - timedelta(days=i)).isoformat(), {}).get("m", 0) for i in range(89, -1, -1)]
    w1 = period_sum(stats, today, 7)
    w0 = period_sum(stats, today - timedelta(days=7), 7)
    clean_now = round(100 * w1["c"] / w1["m"]) if w1["m"] else None
    clean_prev = round(100 * w0["c"] / w0["m"]) if w0["m"] else None
    return {
        "today": today, "days": days, "w1": w1, "w0": w0, "clean_now": clean_now, "clean_prev": clean_prev,
        "total_days": sum(1 for v in stats.values() if v.get("m", 0) > 0),
        "total_msgs": sum(v.get("m", 0) for v in stats.values()),
        "total_voice": sum(v.get("v", 0) for v in stats.values()),
        "top": [(c, n) for c, n in top_categories(stats, today, 30) if c != "o"][:3],
    }


def stats_text(u: dict, s: dict) -> str:
    lines = ["📊 Your progress",
             f"🔥 Streak: {u['streak']} (best {u['best_streak']})",
             f"📅 Days practiced: {s['total_days']}, messages: {s['total_msgs']}, voice: {s['total_voice']}",
             f"This week: {s['w1']['m']} messages, {s['w1']['days']} active days "
             f"(last week: {s['w0']['m']} messages, {s['w0']['days']} days)"]
    if s["clean_now"] is not None:
        extra = ""
        if s["clean_prev"] is not None:
            diff = s["clean_now"] - s["clean_prev"]
            extra = f" (last week {s['clean_prev']}%, {'+' if diff >= 0 else ''}{diff})"
        lines.append(f"✨ Messages without mistakes: {s['clean_now']}%{extra}")
    if s["top"]:
        lines.append("Most common slips (30 days): " +
                     ", ".join(f"{CATEGORY_NAMES[c]} {n}" for c, n in s["top"]))
    for g in active_goals(u):
        lines.append(goal_progress_line(u, g))
    return "\n".join(lines)


def widget_payload(u: dict, s: dict) -> str:
    data = {"e": s["today"].isoformat(), "d": s["days"][-90:], "s": u["streak"], "b": u["best_streak"],
            "t": s["total_days"], "w": s["clean_now"] if s["clean_now"] is not None else -1,
            "p": s["clean_prev"] if s["clean_prev"] is not None else -1,
            "k": [[c, n] for c, n in s["top"]],
            "g": [[g["name"][:14], goal_overall(g) if goal_overall(g) is not None else -1]
                  for g in active_goals(u)][:2]}
    text = json.dumps(data, separators=(",", ":"))
    while len(text.encode()) > 1000 and len(data["d"]) > 14:
        data["d"] = data["d"][len(data["d"]) // 4:]
        if len(text.encode()) > 1000 and data["g"]:
            data["g"] = data["g"][:1]
        text = json.dumps(data, separators=(",", ":"))
    return text


STATS_WIDGET_HTML = """<style>
body{margin:0;padding:10px 12px 12px}
h4{margin:0}
.s{color:var(--tg-theme-hint-color);font-size:12px;min-height:15px;margin:2px 0 6px}
.b{display:flex;align-items:flex-end;height:96px;gap:1px;border-bottom:1px solid var(--tg-theme-section-separator-color)}
.b i{flex:1;background:var(--tg-theme-button-color);border-radius:2px 2px 0 0}
.t{display:flex;gap:6px;margin:8px 0}
.t button{flex:1;padding:5px 0;font-size:13px;border-radius:8px;border:0;background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-text-color)}
.t .on{background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color)}
.g{display:flex;text-align:center}
.g div{flex:1}
.g b{display:block;font-size:20px}
.g span,.k{color:var(--tg-theme-hint-color);font-size:12px}
.k{margin-top:6px}
</style>
<h4>Practice activity</h4><div class=s id=s></div><div class=b id=b></div>
<div class=t id=t></div><div class=g id=g></div><div class=k id=k></div>
<script>
var W=window.TelegramWidget,D={};
try{D=JSON.parse(W.publicData)}catch(e){}
var all=D.d||[],n=30,a=[];
var N={a:"articles",t:"tenses",p:"prepositions",w:"word order",v:"verb forms",s:"spelling",c:"word choice"};
function $(i){return document.getElementById(i)}
function dt(i){if(!D.e)return"?";var d=new Date(D.e+"T00:00:00Z");d.setUTCDate(d.getUTCDate()-(a.length-1-i));return d.toISOString().slice(0,10)}
function draw(){
a=all.slice(-n);var mx=Math.max.apply(null,a.concat([1])),h="",i;
for(i=0;i<a.length;i++)h+='<i data-i="'+i+'" style="height:'+Math.max(1,Math.round(a[i]/mx*92))+'px"></i>';
$("b").innerHTML=h;
var sum=0;for(i=0;i<a.length;i++)sum+=a[i];
$("s").textContent="Last "+a.length+" days: "+sum+" messages";
var t="";[7,30,90].forEach(function(v){t+='<button data-n="'+v+'"'+(v==n?' class=on':'')+'>'+v+'d</button>'});
$("t").innerHTML=t}
$("b").onclick=function(e){var i=e.target.getAttribute?e.target.getAttribute("data-i"):null;if(i!==null)$("s").textContent=dt(+i)+": "+a[+i]+" messages"};
$("t").onclick=function(e){var v=e.target.getAttribute?e.target.getAttribute("data-n"):null;if(v){n=+v;draw()}};
var c=D.w<0?"-":D.w+"%",d=D.w>=0&&D.p>=0?(D.w-D.p>=0?"+":"")+(D.w-D.p):"";
$("g").innerHTML="<div><b>"+(D.s||0)+"</b><span>streak</span></div><div><b>"+(D.t||0)+"</b><span>days</span></div><div><b>"+c+"</b><span>clean "+d+"</span></div>";
var k="",j;(D.k||[]).forEach(function(x){k+=(k?", ":"")+N[x[0]]+" "+x[1]});
(D.g||[]).forEach(function(x){k+=(k?"<br>":"")+x[0]+": "+(x[1]<0?"no data":x[1]+"%")});
$("k").innerHTML=(k?"Slips: ":"")+k;
draw();
</script>"""


async def send_widget(chat_id: int, html_text: str, caption: str, public_data: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendWidget",
                                  json={"chat_id": chat_id, "html": html_text,
                                        "caption": caption[:1000], "public_data": public_data})
        data = r.json()
        if not data.get("ok"):
            log.warning("sendWidget refused: %s", str(data)[:300])
            return False
        return True
    except Exception:
        log.exception("sendWidget failed")
        return False


# ---- hints and reminders ----
HINT_PROMPT = (
    "You help an English learner who went silent in a chat. Suggest 3 short, natural things they could say "
    "next to continue the conversation. They must fit the last messages, the topic and the level "
    "({level}). Return ONLY JSON: {{\"hints\":[{{\"en\":string,\"ru\":string}}]}} where ru is a short "
    "Russian translation."
)


async def gen_hint(u: dict, friend: dict | None = None) -> str | None:
    level = LEVELS.get(u["level"], "unknown")
    data = await ai_json(HINT_PROMPT.format(level=level), "Suggest what I could say next.",
                         ((friend or u).get("history") or [])[-6:])
    hints = (data or {}).get("hints") if isinstance(data, dict) else None
    if not hints:
        return None
    lines = []
    for h in hints[:3]:
        if isinstance(h, dict) and h.get("en"):
            ru = f" ({h['ru']})" if h.get("ru") else ""
            lines.append(f"• {html.escape(str(h['en']))}{html.escape(ru)}")
    if not lines:
        return None
    await upd(u, last_hint=" ".join(str(h.get("en", "")) for h in hints[:3] if isinstance(h, dict)),
              last_hint_ts=time.time())
    return "💡 You could say:\n" + "\n".join(lines)


STATIC_NUDGES = ["Hey, up for a quick chat in English?", "Got a minute? Let's practice a bit.",
                 "Hey, how's your day going? Tell me about it in English."]


async def gen_nudge(u: dict) -> str:
    system = (build_prompt(u) + "\nNow write ONE short message (1-2 sentences) that invites the learner to come "
              'back and chat, in your style, mentioning the last topic of the chat if there is one. '
              'Return ONLY JSON: {"reply":"...","mistakes":[],"tips":[]}.')
    data = await ai_json(system, "Write the nudge now.", (u.get("history") or [])[-6:])
    if isinstance(data, dict) and data.get("reply"):
        try:
            return clean_reply(data["reply"])
        except Exception:
            pass
    return random.choice(STATIC_NUDGES)


def in_quiet_hours(u: dict, now: datetime) -> bool:
    a, b = parse_hhmm(u.get("quiet_from") or ""), parse_hhmm(u.get("quiet_to") or "")
    if not a or not b or a == b:
        return False
    t = now.strftime("%H:%M")
    return (t >= a or t < b) if a > b else (a <= t < b)


async def nudge_one(bot: Bot, u: dict) -> None:
    now_ts = time.time()
    uid = u["user_id"]
    last_user, last_bot = u.get("last_user_ts"), u.get("last_bot_ts")
    waiting = bool(last_bot) and (last_user is None or last_user < last_bot)

    # 1) hint when he stops answering in an active conversation, then end the conversation
    if u["active"] and u["hints_on"] and waiting:
        wait = u["hint_min"] * 60
        if not u["hint_sent"] and now_ts - last_bot >= wait:
            text = await gen_hint(u)
            await upd(u, hint_sent=True, hint_ts=now_ts)
            if text:
                await bot.send_message(uid, text, **main_kw(u))
            return
        if u["hint_sent"] and now_ts - (u.get("hint_ts") or now_ts) >= wait:
            await upd(u, active=False, silence_end=True, hint_sent=False)
            return

    # 2) reminders after a longer silence
    if u["remind_on"] and last_user:
        limit = 1 if u["remind2_h"] == 0 else 2
        sent = u["remind_sent"]
        if sent < limit:
            threshold = u["remind1_h"] * 3600 if sent == 0 else u["remind2_h"] * 3600
            gap_ok = sent == 0 or now_ts - (u.get("remind_ts") or 0) >= 3600
            now_local = local_now(u)
            counted = u["streak_on"] and u["last_day"] == now_local.date()
            if (now_ts - last_user >= threshold and gap_ok and not counted
                    and not in_quiet_hours(u, now_local)):
                await upd(u, remind_sent=sent + 1, remind_ts=now_ts)
                await bot.send_message(uid, html.escape(await gen_nudge(u)), **main_kw(u))
                return

    # 3) goal reports on the last day of a goal (or later, if the bot was off)
    now_local = local_now(u)
    for g in active_goals(u):
        end = date.fromisoformat(g["end"])
        if now_local.date() > end or (now_local.date() == end and now_local.hour >= GOAL_REPORT_HOUR):
            for part in goal_report(g):
                await bot.send_message(uid, part, **main_kw(u))
            g["done"] = True
            for it in g["items"]:
                it["seen"] = []
            done = [x for x in u["goals"] if x.get("done")]
            keep = [x for x in u["goals"] if not x.get("done")] + done[-3:]
            await upd(u, goals=keep)
            return


async def friend_hints(bot: Bot, u: dict) -> None:
    """Silence hint inside a friend's topic: he stopped answering, so send ideas into that topic."""
    if not u["hints_on"]:
        return
    now_ts = time.time()
    for f in await friends_of(u["user_id"]):
        last_bot, last_user = f.get("last_bot_ts"), f.get("last_user_ts")
        waiting = bool(last_bot) and (last_user is None or last_user < last_bot)
        if waiting and not f.get("hint_sent") and now_ts - last_bot >= u["hint_min"] * 60:
            await upd_friend(f, hint_sent=True, hint_ts=now_ts)
            text = await gen_hint(u, f)
            if text:
                await bot.send_message(u["user_id"], text, message_thread_id=f["thread_id"])


async def nudge_loop(bot: Bot) -> None:
    while True:
        try:
            for u in await all_users():
                try:
                    await nudge_one(bot, u)
                    await friend_hints(bot, u)
                except Exception:
                    log.exception("nudge failed for %s", u.get("user_id"))
        except Exception:
            log.exception("nudge loop error")
        await asyncio.sleep(NUDGE_TICK_SECONDS)


async def mark_user_message(u: dict, friend: dict | None = None) -> None:
    await upd(u, last_user_ts=time.time(), hint_sent=False, remind_sent=0)
    if friend:
        await upd_friend(friend, last_user_ts=time.time(), hint_sent=False)


async def ensure_active(message: Message, u: dict) -> bool:
    if u["active"]:
        return True
    if u["silence_end"] or u["remind_sent"] > 0:  # he is back after a pause: just continue
        await upd(u, active=True, silence_end=False)
        return True
    await say(message, "Send /start to begin 🙂")
    return False


async def after_user_message(bot: Bot, u: dict, text: str, result: dict, voice: bool, prev_bot: str) -> None:
    await record_stats(u, result, voice)
    track(track_goals(u["user_id"], text, prev_bot))


# ---- voice replies (text to speech) ----
def to_ogg_opus(data: bytes, mime: str) -> bytes:
    import subprocess
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        import shutil
        exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found")
    in_args = ["-i", "pipe:0"]
    if "l16" in mime.lower() or "pcm" in mime.lower():
        m = re.search(r"rate=(\d+)", mime)
        in_args = ["-f", "s16le", "-ar", m.group(1) if m else "24000", "-ac", "1", "-i", "pipe:0"]
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", *in_args, "-c:a", "libopus", "-b:a", "32k",
           "-ar", "48000", "-ac", "1", "-f", "ogg", "pipe:1"]
    r = subprocess.run(cmd, input=data, capture_output=True, timeout=90)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("ffmpeg failed: " + r.stderr.decode(errors="ignore")[:200])
    return r.stdout


async def tts_gemini(text: str, voice: str, accent: str | None = None) -> tuple[bytes, str]:
    style = (f"relaxed, friendly, speaking English with a strong {accent} accent, conversational pace" if accent
             else "relaxed, friendly, natural American accent, conversational pace")
    variants = [
        (GEMINI_TTS_MODEL, {
            "contents": [{"role": "user", "parts": [{"text": text, "speech_metadata": {"style": style}}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"voice": voice}}}}),
        (GEMINI_TTS_LEGACY_MODEL, {
            "contents": [{"parts": [{"text": f"Say in a {style}: {text}"}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}),
    ]
    last_error: Exception = RuntimeError("no variants")
    for model, payload in variants:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(GEMINI_URL.format(model=model),
                                      headers={"x-goog-api-key": GEMINI_API_KEY}, json=payload)
            r.raise_for_status()
            part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
            return base64.b64decode(part["data"]), part.get("mimeType", "audio/L16;rate=24000")
        except Exception as e:
            last_error = e
            log.warning("Gemini TTS variant %s failed: %s", model, str(e)[:200])
    raise last_error


async def tts_edge(text: str, voice: str) -> tuple[bytes, str]:
    import edge_tts
    audio = b""
    async for chunk in edge_tts.Communicate(text, voice).stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    if not audio:
        raise RuntimeError("Edge TTS returned no audio")
    return audio, "audio/mpeg"


def clean_for_speech(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]", "", text)
    return " ".join(text.split())[:700]


async def synth(text: str, u: dict, friend: dict | None = None) -> bytes:
    text = clean_for_speech(text)
    if not text:
        raise RuntimeError("nothing to say")
    provider, _, voice = ((friend or {}).get("voice") or u.get("tts_voice") or "gemini:Kore").partition(":")
    accent = (friend or {}).get("accent")
    order = [(provider, voice)]
    other = "edge" if provider == "gemini" else "gemini"
    order.append((other, EDGE_VOICES[0] if other == "edge" else GEMINI_VOICES[0]))
    last_error: Exception = RuntimeError("no voice provider")
    for prov, vc in order:
        try:
            if prov == "gemini" and GEMINI_API_KEY:
                data, mime = await tts_gemini(text, vc, accent)
            elif prov == "edge":
                data, mime = await tts_edge(text, vc)
            else:
                continue
            return await asyncio.to_thread(to_ogg_opus, data, mime)
        except Exception as e:
            last_error = e
            log.warning("TTS provider %s failed: %s", prov, str(e)[:200])
    raise last_error


async def send_voice_reply(bot: Bot, u: dict, chat_id: int, text: str, friend: dict | None = None,
                           thread_id: int | None = None) -> bool:
    try:
        async with Typing(bot, chat_id, "record_voice", thread_id):
            ogg = await synth(text, u, friend)
            kw = {"message_thread_id": thread_id} if thread_id else {}
            await bot.send_voice(chat_id, BufferedInputFile(ogg, filename="reply.ogg"), **kw)
        return True
    except Exception:
        log.exception("voice reply failed")
        return False


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
    def tog(label: str, value: bool, data: str):  # green when on, red when off
        return (f"{'✅' if value else '❌'} {label}", data, "success" if value else "danger")

    style_label = STYLES.get(u["style"], STYLES[DEFAULT_STYLE])[0]
    voice_on = u["voice_mode"] != "off"
    return kb([
        [tog("Emoji", u["emoji"], "set:emoji"), tog("Tips 💡", u["tips_on"], "set:tips")],
        [tog("Streak", u["streak_on"], "set:streak"), tog("Pin streak", u["pin_on"], "set:pin")],
        [tog("Morning message", u["morning_on"], "set:morning")],
        [(f"🗣 Explanations: {'English' if u['explain_lang'] == 'en' else 'Русский'}", "set:lang")],
        [(f"🎭 Style: {style_label}", "set:style")],
        [(f"🕒 Time zone: {tz_label(u['tz'])}", "set:tz")],
        [(f"⏰ Morning time: {u['morning_time']}", "set:time")],
        [tog("Silence hints", u["hints_on"], "set:hints"), tog("Weak spots", u["weak_on"], "set:weak")],
        [(f"💡 Hint after: {u['hint_min']} min", "set:hmin")],
        [tog("Reminders", u["remind_on"], "set:remind"), ("🔔 Reminder timing", "set:rm")],
        [(f"🔊 Voice replies: {VOICE_MODE_LABELS.get(u['voice_mode'], 'off')}", "set:voice",
          "success" if voice_on else "danger"),
         ("🎙 Voice choice", "set:vchoice")],
    ])


# ---- handlers ----
class AccessMiddleware(BaseMiddleware):
    """Only the admin and the people the admin allowed can use the bot."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
        if chat is not None and getattr(chat, "type", "private") != "private":
            return None
        if getattr(user, "is_bot", False) or getattr(event, "pinned_message", None) is not None:
            return None  # service messages are not from a person
        if user is None or user.id == ADMIN_ID or user.id in allowed:
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer("This is a private bot.")
        elif isinstance(event, CallbackQuery):
            await event.answer("This is a private bot.", show_alert=True)
        return None


class TopicMiddleware(BaseMiddleware):
    """Keeps the topics apart: commands (except /start, /new, /stop, /hint) only work in Commands."""

    async def __call__(self, handler, event, data):
        text = getattr(event, "text", None) or ""
        user = getattr(event, "from_user", None)
        if text.startswith("/") and user and not getattr(user, "is_bot", False):
            u = await get_user(user.id)
            if topics_on(u):
                name = (text[1:].split() or [""])[0].split("@")[0].lower()
                kind, _ = await route(event, u)
                if not (name == "start" or kind == "cmd" or (kind in ("main", "friend") and name in CHAT_COMMANDS)):
                    await say(event, "Commands live in the Commands topic, open it in the sidebar 👉")
                    return None
        return await handler(event, data)


router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
router.message.outer_middleware(AccessMiddleware())
router.callback_query.outer_middleware(AccessMiddleware())
router.message.outer_middleware(TopicMiddleware())

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
    "/stats - your progress\n"
    "/setagoal - practice goals (words, grammar, phrasal verbs)\n"
    "/create - make a friend with their own topic\n"
    "/friends - your friends\n"
    "/hint - ideas for what to say next\n"
    "/help - this message"
)
ADMIN_HELP = (
    "\n\nAdmin:\n"
    "/allow ID - give someone access\n"
    "/deny ID - take access away\n"
    "/allowed - list of allowed IDs\n"
    "/setstreak N - set the streak to N days (today counts)\n"
    "/voicetest - send voice samples to choose a voice"
)


@router.message(Command("start", "new"))
async def cmd_start(message: Message):
    u = await get_user(message.from_user.id)
    kind, friend = await route(message, u)
    if friend:  # inside a friend's topic: just clear that conversation
        await upd_friend(friend, history=[])
        await say(message, "Fresh start 👋")
        return
    await ensure_topics(message.bot, u)
    await reset_chat(u)
    tid = u["main_thread"] if topics_on(u) else None
    await say(message,
              "Hey! 👋 I'm here to help you practice English. Just chat with me, "
              "and I'll gently point out mistakes.",
              tid, reply_markup=kb([[("Start a conversation", "go")]]))
    if tid and thread_of(message) != tid:
        await say(message, "Let's chat in Main, open it in the sidebar 👉")


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    u = await get_user(message.from_user.id)
    if await friend_here(message):  # keep the chats apart: /stop belongs to the main chat only
        await say(message, "Send /new here to clear this chat, or delete the friend in /friends.")
        return
    await reset_chat(u)
    await say(message, "Chat ended. Send /new whenever you want to talk again 👋")


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = HELP_TEXT + (ADMIN_HELP if message.from_user.id == ADMIN_ID else "")
    await say(message, text)


@router.message(Command("style"))
async def cmd_style(message: Message):
    await say(message, "Pick the vibe of our chat:", reply_markup=style_kb())


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    u = await get_user(message.from_user.id)
    await say(message, "⚙️ Settings", reply_markup=settings_kb(u))


@router.message(Command("streak"))
async def cmd_streak(message: Message):
    u = await get_user(message.from_user.id)
    if not u["streak_on"]:
        await say(message, "The streak is turned off. You can turn it on in /settings.")
        return
    counted = u["last_day"] == local_today(u)
    text = f"Streak: {u['streak']}{fire(u)} (best: {u['best_streak']})\n"
    text += "Today is counted." if counted else \
        f"Today is not counted yet: {STREAK_MESSAGES} messages in a conversation count the day."
    await say(message, text, reply_markup=streak_kb(u["streak"], counted))


# ---- admin commands ----
@router.message(Command("allow"))
async def cmd_allow(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await say(message, "Usage: /allow 123456789")
        return
    await set_allowed(int(arg), True)
    await say(message, f"Access granted to {arg}.")


@router.message(Command("deny"))
async def cmd_deny(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await say(message, "Usage: /deny 123456789")
        return
    await set_allowed(int(arg), False)
    await say(message, f"Access removed for {arg}.")


@router.message(Command("allowed"))
async def cmd_allowed(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await say(message, "Allowed IDs: " + (", ".join(str(i) for i in sorted(allowed)) or "nobody yet"))


@router.message(Command("setstreak"))
async def cmd_setstreak(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) > 9999:
        await say(message, "Usage: /setstreak 30")
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
        await say(message, "Streak set to 0.")


# ---- callbacks ----
@router.callback_query(F.data == "go")
async def cb_go(call: CallbackQuery):
    await say(call.message, "What's your level? You can skip this, I'll adapt as we chat.",
                              reply_markup=level_kb())
    await call.answer()


@router.callback_query(F.data.startswith("lvl:"))
async def cb_level(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    value = call.data.split(":", 1)[1]
    await upd(u, level=value if value in LEVELS else None)
    await say(call.message, "Pick a topic, write your own, or skip.", reply_markup=topic_kb())
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
        await say(call.message, "Type the topic you want to talk about 👇")
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
    await say(call.message, f"Vibe set: {STYLES[key][0]}. Just keep chatting 🙂")


@router.callback_query(F.data.startswith("set:"))
async def cb_set(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    key = call.data.split(":", 1)[1]
    toggles = {"emoji": "emoji", "tips": "tips_on", "streak": "streak_on",
               "morning": "morning_on", "pin": "pin_on", "hints": "hints_on", "weak": "weak_on",
               "remind": "remind_on"}
    await call.answer()
    if key in toggles:
        field = toggles[key]
        await upd(u, **{field: not u[field]})
    elif key == "lang":
        await upd(u, explain_lang="en" if u["explain_lang"] == "ru" else "ru")
    elif key == "hmin":
        await upd(u, hint_min={3: 5, 5: 10, 10: 3}.get(u["hint_min"], 5))
    elif key == "voice":
        await upd(u, voice_mode={"off": "button", "button": "always", "always": "off"}.get(u["voice_mode"], "off"))
    elif key == "vchoice":
        await say(call.message, "Pick a voice (or send /voicetest to hear them):", reply_markup=voice_kb())
        return
    elif key == "rm":
        await say(call.message, "🔔 " + reminder_summary(u), reply_markup=reminder_kb())
        return
    elif key == "style":
        await say(call.message, "Pick the vibe of our chat:", reply_markup=style_kb())
        return
    elif key == "tz":
        await say(call.message, "Pick your time zone or type your own:", reply_markup=tz_kb())
        return
    elif key == "time":
        await say(call.message, "When should the morning message come?", reply_markup=time_kb())
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
        await say(call.message, "Type a time zone like Europe/Berlin or an offset like +3 👇")
        return
    name = parse_tz(value)
    if name:
        await upd(u, tz=name)
        await say(call.message, f"Time zone set: {tz_label(name)}")


@router.callback_query(F.data.startswith("tm:"))
async def cb_time(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    value = call.data.split(":", 1)[1]
    await call.answer()
    if value == "other":
        await upd(u, awaiting="time")
        await say(call.message, "Type the time like 09:30 👇")
        return
    hhmm = parse_hhmm(value)
    if hhmm:
        await upd(u, morning_time=hhmm)
        await say(call.message, f"Morning message time set: {hhmm}")


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


@router.callback_query(F.data == "streak:file")
async def cb_streak_file(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    n = u["streak"]
    if n <= 0:
        await call.answer("No streak yet", show_alert=True)
        return
    await call.answer("Preparing the file...")
    try:
        await send_story_file(call.bot, call.from_user.id, n, thread_of(call.message))
    except Exception:
        log.exception("send story file failed")
        await say(call.message, "Could not prepare the file. Try again in a minute.")


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
        await say(message, "Please type that as text 👇")
        return
    kind, friend = await route(message, u)
    if kind in WRONG_TOPIC:
        await say(message, WRONG_TOPIC[kind])
        return
    if not friend and not await ensure_active(message, u):
        return
    if message.voice.duration > MAX_VOICE_SECONDS:
        await say(message, "Please keep voice messages under 60 seconds 🎙️")
        return
    await mark_user_message(u, friend)
    try:
        buf = BytesIO()
        await message.bot.download(message.voice, destination=buf)
        text = await transcribe(buf.getvalue())
    except AIError as e:
        await say(message, f"Something went wrong (error {e.code}). Try again in a minute.")
        return
    except Exception:
        log.exception("Voice download failed")
        await say(message, "Something went wrong (error 500). Try again in a minute.")
        return
    if not text:
        await say(message, "I couldn't hear anything. Try again? 🎙️")
        return
    await say(message, f"🎙 <i>{html.escape(text)}</i>")
    prev_bot = last_assistant(friend or u)
    result = await safe_ai(message, u, text, friend=friend)
    if result:
        await register_message(message.bot, u)
        await after_user_message(message.bot, u, text, result, True, prev_bot)


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
    if awaiting == "goal_text":
        await handle_goal_text(message, u)
        return
    if awaiting == "goal_days":
        digits = message.text.strip()
        if digits.isdigit() and 1 <= int(digits) <= 365:
            await create_goal(message, u, int(digits))
        else:
            await say(message, "Please type a number of days from 1 to 365.")
        return
    if awaiting in ("fr_name", "fr_about", "fr_accent"):
        await friend_dialog_text(message, u, awaiting)
        return
    if awaiting == "tz":
        name = parse_tz(message.text)
        if name:
            await upd(u, tz=name, awaiting=None)
            await say(message, f"Time zone set: {tz_label(name)}")
        else:
            await say(message, "I don't know that one. Try Europe/Berlin or an offset like +3.")
        return
    if awaiting == "time":
        hhmm = parse_hhmm(message.text)
        if hhmm:
            await upd(u, morning_time=hhmm, awaiting=None)
            await say(message, f"Morning message time set: {hhmm}")
        else:
            await say(message, "Please type the time like 09:30.")
        return
    kind, friend = await route(message, u)
    if kind in WRONG_TOPIC:
        await say(message, WRONG_TOPIC[kind])
        return
    if not friend and not await ensure_active(message, u):
        return
    await mark_user_message(u, friend)
    prev_bot = last_assistant(friend or u)
    result = await safe_ai(message, u, message.text, friend=friend)
    if result:
        await register_message(message.bot, u)
        await after_user_message(message.bot, u, message.text, result, False, prev_bot)


@router.message(F.sticker | F.photo | F.video | F.animation | F.document | F.video_note | F.audio)
async def on_media(message: Message):
    uid = message.from_user.id
    u = await get_user(uid)
    kind, friend = await route(message, u)
    if kind in WRONG_TOPIC:
        return
    if not (friend or u["active"]) or not cooldown_ok(uid):
        return
    kind = message.content_type.value
    note = (f"[The user sent a {kind}. React to it in English like it is a meme or a funny situation, "
            "keep it short, and at the end say you can't see or read it. Return an empty list for mistakes.]")
    await safe_ai(message, u, note, save_user_text=False, friend=friend)


# ---- friends: own characters in their own topics ----
VOICE_GENDER = {"Kore": "♀", "Puck": "♂", "Charon": "♂", "Aoede": "♀",
                "en-US-AvaNeural": "♀", "en-US-AndrewNeural": "♂", "en-US-AriaNeural": "♀", "en-US-GuyNeural": "♂"}
GREET_NOTE = ("[The learner just opened this chat with you. Greet them in character in one or two short "
              "sentences, like a friend who messages first. Return an empty list for mistakes.]")


def friend_voice_kb():
    items = ([(f"{v} {VOICE_GENDER.get(v, '')}".strip(), f"fv:gemini:{v}") for v in GEMINI_VOICES]
             if GEMINI_API_KEY else [])
    items += [(f"{v.replace('en-US-', '').replace('Neural', '')} {VOICE_GENDER.get(v, '')} (Edge)",
               f"fv:edge:{v}") for v in EDGE_VOICES]
    return kb([items[i:i + 2] for i in range(0, len(items), 2)])


def accent_kb():
    return kb([[("No accent", "fa:none")], [("Spanish", "fa:Spanish"), ("Indian", "fa:Indian")],
               [("French", "fa:French"), ("✍️ Other", "fa:other")]])


async def start_friend_create(message, u: dict) -> None:
    await upd(u, awaiting="fr_name", pending_friend={})
    await say(message, "Let's create a friend. What's their name?")


async def friend_dialog_text(message: Message, u: dict, awaiting: str) -> None:
    text = (message.text or "").strip()
    pend = dict(u.get("pending_friend") or {})
    if awaiting == "fr_name":
        if not 1 <= len(text) <= 30:
            await say(message, "Please send a name, up to 30 characters.")
            return
        pend["name"] = text
        await upd(u, pending_friend=pend, awaiting="fr_about")
        await say(message, f"Who is {html.escape(text)} and what are they like? Their character, how they talk, "
                           "what they care about. Any words, any language.")
    elif awaiting == "fr_about":
        if len(text) < 3:
            await say(message, "Tell me a bit more about them.")
            return
        pend["about"] = text[:600]
        await upd(u, pending_friend=pend, awaiting=None)
        await say(message, "Pick a voice. It is used when voice replies are on in /settings "
                           "(/voicetest sends samples).", reply_markup=friend_voice_kb())
    elif awaiting == "fr_accent":
        pend["accent"] = text[:30] or None
        await upd(u, pending_friend=pend, awaiting=None)
        await finish_friend(message, u)


async def finish_friend(message, u: dict) -> None:
    pend = u.get("pending_friend") or {}
    if not (pend.get("name") and pend.get("about") and pend.get("voice")):
        await say(message, "Something is missing. Start again with /create.")
        return
    try:
        topic = await message.bot.create_forum_topic(chat_id=message.chat.id, name=pend["name"][:30])
    except Exception:
        log.exception("create_forum_topic failed")
        await say(message, "I couldn't create the topic. Open @BotFather, go to this bot's settings and turn on "
                           "topics for private chats, then tap the button.",
                  reply_markup=kb([[("Try again", "fc:go")]]))
        return
    friend = await add_friend(u["user_id"], pend["name"], pend["about"], pend["voice"],
                              pend.get("accent"), topic.message_thread_id)
    await upd(u, pending_friend=None, awaiting=None)
    await say(message, f"✅ {html.escape(friend['name'])} is ready. Open the topic in the sidebar.")
    await safe_ai(message, u, GREET_NOTE, save_user_text=False, friend=friend, thread_id=friend["thread_id"])


@router.message(Command("create"))
async def cmd_create(message: Message):
    u = await get_user(message.from_user.id)
    await start_friend_create(message, u)


@router.message(Command("friends"))
async def cmd_friends(message: Message):
    fs = await friends_of(message.from_user.id)
    if fs:
        text = "Your friends:\n" + "\n".join(
            f"• {html.escape(f['name'])}: {html.escape(f['about'][:60])}" for f in fs) + "\n\nTap 🗑 to delete one."
    else:
        text = "You have no friends yet. Tap the button or send /create."
    rows = [[("➕ New friend", "fr:new")]] + [[(f"🗑 {f['name']}", f"fr:del:{f['id']}")] for f in fs]
    await say(message, text, reply_markup=kb(rows))


@router.callback_query(F.data.startswith("fr:"))
async def cb_friends(call: CallbackQuery):
    uid = call.from_user.id
    u = await get_user(uid)
    await call.answer()
    parts = call.data.split(":")
    if parts[1] == "new":
        await start_friend_create(call.message, u)
        return
    if parts[1] == "no":
        await say(call.message, "Cancelled.")
        return
    if len(parts) < 3 or not parts[2].isdigit():
        return
    friend = next((f for f in await friends_of(uid) if f["id"] == int(parts[2])), None)
    if not friend:
        await say(call.message, "I can't find that friend.")
    elif parts[1] == "del":
        await say(call.message, f"Delete {html.escape(friend['name'])} and the whole conversation?",
                  reply_markup=kb([[("Yes, delete", f"fr:yes:{friend['id']}"), ("Cancel", "fr:no")]]))
    elif parts[1] == "yes":
        try:
            await call.bot.delete_forum_topic(chat_id=call.message.chat.id, message_thread_id=friend["thread_id"])
        except Exception:
            log.warning("could not delete the topic of %s", friend["name"])
        await del_friend(friend)
        await say(call.message, "Deleted.")


@router.callback_query(F.data.startswith("fv:"))
async def cb_friend_voice(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    await call.answer()
    pend = dict(u.get("pending_friend") or {})
    _, provider, voice = call.data.split(":", 2)
    if not pend.get("about") or not ((provider == "gemini" and voice in GEMINI_VOICES)
                                     or (provider == "edge" and voice in EDGE_VOICES)):
        return
    pend["voice"] = f"{provider}:{voice}"
    await upd(u, pending_friend=pend)
    if provider == "gemini":  # only Gemini voices can have an accent
        await say(call.message, "Should their English have an accent? (voice only)", reply_markup=accent_kb())
    else:
        await finish_friend(call.message, u)


@router.callback_query(F.data.startswith("fa:"))
async def cb_friend_accent(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    await call.answer()
    pend = dict(u.get("pending_friend") or {})
    if not pend.get("voice"):
        return
    key = call.data.split(":", 1)[1]
    if key == "other":
        await upd(u, awaiting="fr_accent")
        await say(call.message, "Type the accent, for example Scottish or Brazilian.")
        return
    pend["accent"] = None if key == "none" else key
    await upd(u, pending_friend=pend)
    await finish_friend(call.message, u)


@router.callback_query(F.data == "fc:go")
async def cb_friend_retry(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    await call.answer()
    await finish_friend(call.message, u)


# ---- stage 4 handlers: hints, stats, goals, reminders menu, voice ----
QUIET_OPTIONS = [("22:00-08:00", "22:00", "08:00"), ("23:00-09:00", "23:00", "09:00"),
                 ("00:00-10:00", "00:00", "10:00"), ("Off", "00:00", "00:00")]
GOAL_ASK_TEXT = ("Send me what you want to practice: words, expressions, phrasal verbs, grammar, anything. "
                 "One message, any format. I will not push these words into our chat, I will just notice "
                 "when you use them on your own.")


@router.message(Command("hint"))
async def cmd_hint(message: Message):
    u = await get_user(message.from_user.id)
    friend = await friend_here(message)
    if not friend and not u["active"]:
        await say(message, "Start a conversation first: /start")
        return
    text = await gen_hint(u, friend)
    await say(message, text or "I could not think of a hint right now. Try again in a minute.")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    u = await get_user(message.from_user.id)
    s = build_stats(u)
    text = stats_text(u, s)
    if not await send_widget(message.chat.id, STATS_WIDGET_HTML, text, widget_payload(u, s)):
        await say(message, text)


@router.message(Command("setagoal", ignore_case=True))
async def cmd_setagoal(message: Message):
    u = await get_user(message.from_user.id)
    goals = active_goals(u)
    if not goals:
        await upd(u, awaiting="goal_text")
        await say(message, GOAL_ASK_TEXT)
        return
    rows = [[("➕ New goal", "ga:new")]] + [[(f"🗑 Delete {g['name']}", f"ga:del:{g['id']}")] for g in goals]
    await say(message, "Your goals:\n" + "\n".join(goal_progress_line(u, g) for g in goals),
                         reply_markup=kb(rows))


@router.callback_query(F.data.startswith("ga:"))
async def cb_goal_menu(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    parts = call.data.split(":")
    await call.answer()
    if parts[1] == "new":
        await upd(u, awaiting="goal_text")
        await say(call.message, GOAL_ASK_TEXT)
    elif parts[1] == "del" and len(parts) == 3 and parts[2].isdigit():
        gid = int(parts[2])
        await upd(u, goals=[g for g in u["goals"] if g["id"] != gid])
        await say(call.message, "Goal deleted.")


async def handle_goal_text(message: Message, u: dict) -> None:
    await say(message, "Reading your list...")
    data = await ai_json(GOAL_PARSE_PROMPT, message.text[:2000])
    items: list[dict] = []
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        for it in data["items"][:40]:
            if isinstance(it, dict) and str(it.get("t", "")).strip():
                items.append({"t": str(it["t"]).strip()[:60], "k": str(it.get("k", "other"))[:10]})
    if not items:
        items = fallback_items(message.text)
    if not items:
        await say(message, "I could not find anything there. Send the words separated by commas.")
        return
    await upd(u, pending_goal={"items": items}, awaiting=None)
    text = (f"I found {len(items)} items:\n" + "\n".join(f"• {html.escape(i['t'])}" for i in items) +
            "\n\nFor how long should I track them?")
    await say(message, text, reply_markup=kb([[("7 days", "gd:7"), ("14 days", "gd:14"), ("30 days", "gd:30")],
                                                [("✍️ Custom", "gd:custom"), ("✖ Cancel", "gd:cancel")]]))


async def create_goal(message: Message, u: dict, days: int) -> None:
    pending = u.get("pending_goal")
    if not pending or not pending.get("items"):
        await say(message, "There is nothing to create. Start again with /setagoal.")
        return
    goal = new_goal(u, pending["items"], days)
    await upd(u, goals=(u.get("goals") or []) + [goal], pending_goal=None, awaiting=None)
    await say(message, f"Done: {goal['name']} for {days} days ({len(goal['items'])} items), until {goal['end']}. "
                         f"I will just watch how you use them. The report comes on the last day at "
                         f"{GOAL_REPORT_HOUR}:00.")


@router.callback_query(F.data.startswith("gd:"))
async def cb_goal_days(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    value = call.data.split(":", 1)[1]
    await call.answer()
    if value == "cancel":
        await upd(u, pending_goal=None, awaiting=None)
        await say(call.message, "Cancelled.")
    elif value == "custom":
        await upd(u, awaiting="goal_days")
        await say(call.message, "How many days? Type a number from 1 to 365.")
    elif value.isdigit():
        await create_goal(call.message, u, int(value))


# ---- reminder timing menu ----
def reminder_summary(u: dict) -> str:
    second = "off" if u["remind2_h"] == 0 else f"{u['remind2_h']}h"
    quiet = "off" if u["quiet_from"] == u["quiet_to"] else f"{u['quiet_from']}-{u['quiet_to']}"
    return f"First reminder after {u['remind1_h']}h, second after {second}, quiet hours: {quiet}."


def reminder_kb():
    return kb([[("1h", "rm1:1"), ("2h", "rm1:2"), ("3h", "rm1:3"), ("5h", "rm1:5")],
               [("2nd: 12h", "rm2:12"), ("2nd: 24h", "rm2:24"), ("2nd: off", "rm2:0")],
               [(f"🌙 {label}", f"rmq:{i}") for i, (label, _a, _b) in enumerate(QUIET_OPTIONS)]])


@router.callback_query(F.data.startswith("rm1:") | F.data.startswith("rm2:") | F.data.startswith("rmq:"))
async def cb_reminder_timing(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    kind, value = call.data.split(":", 1)
    await call.answer()
    if kind == "rm1" and value.isdigit():
        await upd(u, remind1_h=int(value))
    elif kind == "rm2" and value.isdigit():
        await upd(u, remind2_h=int(value))
    elif kind == "rmq" and value.isdigit() and int(value) < len(QUIET_OPTIONS):
        _label, a, b = QUIET_OPTIONS[int(value)]
        await upd(u, quiet_from=a, quiet_to=b)
    try:
        await call.message.edit_text("🔔 " + reminder_summary(u), reply_markup=reminder_kb())
    except Exception:
        pass


# ---- voice replies ----
def voice_kb():
    rows = [[(f"Gemini {v}", f"vc:gemini:{v}") for v in GEMINI_VOICES[i:i + 2]] for i in range(0, len(GEMINI_VOICES), 2)]
    rows += [[(v.replace("en-US-", "").replace("Neural", " (Edge)"), f"vc:edge:{v}") for v in EDGE_VOICES[i:i + 2]]
             for i in range(0, len(EDGE_VOICES), 2)]
    return kb(rows)


@router.callback_query(F.data.startswith("vc:"))
async def cb_voice_choice(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    _, provider, voice = call.data.split(":", 2)
    await call.answer()
    if (provider == "gemini" and voice in GEMINI_VOICES) or (provider == "edge" and voice in EDGE_VOICES):
        await upd(u, tts_voice=f"{provider}:{voice}")
        await say(call.message, f"Voice set: {voice} ({provider}). Try /voicetest or turn on voice replies in /settings.")


@router.callback_query(F.data == "tts:go")
async def cb_tts(call: CallbackQuery):
    u = await get_user(call.from_user.id)
    await call.answer()
    tid = thread_of(call.message)
    friend = await friend_by_thread(call.from_user.id, tid)
    await send_voice_reply(call.bot, u, call.from_user.id, call.message.text or "", friend, tid)


@router.message(Command("voicetest"))
async def cmd_voicetest(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await say(message, "Sending voice samples one by one, it can take a minute...")
    tests = ([("gemini", v) for v in GEMINI_VOICES] if GEMINI_API_KEY else []) + [("edge", v) for v in EDGE_VOICES]
    for provider, voice in tests:
        try:
            data, mime = await (tts_gemini if provider == "gemini" else tts_edge)(VOICE_SAMPLE, voice)
            ogg = await asyncio.to_thread(to_ogg_opus, data, mime)
            await message.bot.send_voice(message.chat.id, BufferedInputFile(ogg, filename="sample.ogg"),
                                         caption=f"{provider}: {voice}",
                                         **({"message_thread_id": thread_of(message)} if thread_of(message) else {}))
        except Exception as e:
            log.warning("voicetest %s %s failed: %s", provider, voice, str(e)[:200])
            await say(message, f"{provider}: {voice} failed ({str(e)[:80]})")
    await say(message, "Pick one in /settings → Voice choice.")



# ---- web server (webhook, health check for the pinger, story page) ----
STORY_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{color-scheme:dark light}
body{margin:0;min-height:100vh;box-sizing:border-box;padding:20px 16px 28px;display:flex;
flex-direction:column;align-items:center;gap:14px;background:var(--tg-theme-bg-color,#1c1c1e);
color:var(--tg-theme-text-color,#fff);font-family:-apple-system,Segoe UI,Roboto,sans-serif}
.card{width:min(62vw,300px);aspect-ratio:9/16;border-radius:22px;overflow:hidden;background:#000;
box-shadow:0 8px 28px rgba(0,0,0,.35)}
.card img,.card video{width:100%;height:100%;object-fit:cover;display:block}
h1{font-size:20px;margin:4px 0 0;font-weight:700}
button{width:min(100%,340px);padding:14px 16px;border:0;border-radius:14px;font-size:16px;
font-weight:600;background:var(--tg-theme-button-color,#2f80ed);color:var(--tg-theme-button-text-color,#fff)}
button.secondary{background:rgba(127,127,127,.22);color:var(--tg-theme-text-color,#fff)}
#status{min-height:20px;font-size:14px;text-align:center;opacity:.9;max-width:340px}
#info{font-size:11px;opacity:.45}
</style></head>
<body>
<div class="card" id="media"></div>
<h1 id="title"></h1>
<button id="pub">📣 Publish story</button>
<button id="send" class="secondary">💾 Send file to chat</button>
<button id="back" class="secondary">← Back to chat</button>
<div id="status"></div>
<div id="info"></div>
<script>
var tg = window.Telegram.WebApp;
tg.ready();
tg.expand();
var MEDIA = __MEDIA__, TEXT = __TEXT__, KIND = __KIND__, N = __N__, SIG = __SIG__;
var status = document.getElementById("status");
document.getElementById("title").textContent = TEXT;
var holder = document.getElementById("media");
if (KIND === "mp4") {
  var v = document.createElement("video");
  v.src = MEDIA; v.muted = true; v.loop = true; v.autoplay = true; v.playsInline = true;
  holder.appendChild(v);
} else {
  var im = document.createElement("img");
  im.src = MEDIA;
  holder.appendChild(im);
}
document.getElementById("pub").onclick = function () {
  try {
    if (tg.isVersionAtLeast && !tg.isVersionAtLeast("7.8")) {
      status.textContent = "Your Telegram is too old for stories (version 7.8 or newer is needed).";
      return;
    }
    tg.shareToStory(MEDIA, {text: TEXT});
    status.textContent = "If the editor did not open, use 'Send file to chat' and post the file yourself.";
  } catch (e) {
    status.textContent = "Could not open the editor: " + e;
  }
};
document.getElementById("send").onclick = function () {
  status.textContent = "Sending...";
  fetch("/api/story_file", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({initData: tg.initData, n: N, sig: SIG})
  }).then(function (r) { return r.json(); }).then(function (d) {
    status.textContent = d.ok ? "Sent! Check the chat." : "Error: " + (d.error || "unknown");
  }).catch(function (e) { status.textContent = "Network error: " + e; });
};
document.getElementById("back").onclick = function () { tg.close(); };
document.getElementById("info").textContent = "Telegram " + tg.version + ", " + tg.platform;
</script></body></html>"""


def check_init_data(init_data: str) -> int | None:
    """Checks the signature of the data Telegram gives to a mini app. Returns the user id or None."""
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        return int(json.loads(pairs["user"])["id"])
    except Exception:
        return None


async def story_page(request):
    try:
        n = int(request.query.get("n", ""))
    except ValueError:
        return web.Response(status=400)
    sig = request.query.get("sig", "")
    if not 0 <= n <= 9999 or not hmac.compare_digest(sig, story_sig(n)):
        return web.Response(status=403)
    try:
        path, kind = await asyncio.wait_for(asyncio.to_thread(story.ensure_story, n), timeout=150)
    except Exception:
        log.exception("story page failed")
        return web.Response(text="Could not prepare the story, please try again.", status=500)
    media = f"{BASE_URL}/media/{os.path.basename(path)}?sig={sig}"
    text = f"{n} {'day' if n == 1 else 'days'} in a row 🔥"
    page = (STORY_HTML.replace("__MEDIA__", json.dumps(media)).replace("__TEXT__", json.dumps(text))
            .replace("__KIND__", json.dumps(kind)).replace("__N__", str(n))
            .replace("__SIG__", json.dumps(sig)))
    return web.Response(text=page, content_type="text/html")


async def story_send(request):
    """Called by the mini app: sends the story file into the user's chat."""
    try:
        body = await request.json()
        n = int(body.get("n", -1))
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"}, status=400)
    uid = check_init_data(str(body.get("initData", "")))
    if uid is None or not (uid == ADMIN_ID or uid in allowed):
        return web.json_response({"ok": False, "error": "not allowed"}, status=403)
    if not 0 <= n <= 9999 or not hmac.compare_digest(str(body.get("sig", "")), story_sig(n)):
        return web.json_response({"ok": False, "error": "bad signature"}, status=403)
    try:
        await send_story_file(request.app["bot"], uid, n, main_kw(await get_user(uid)).get("message_thread_id"))
    except Exception:
        log.exception("story_send failed")
        return web.json_response({"ok": False, "error": "could not send"}, status=500)
    return web.json_response({"ok": True})


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
    track(nudge_loop(bot))


async def on_shutdown(bot: Bot):
    if pool is not None:
        await pool.close()


async def health(request):
    return web.Response(text="OK")
    
    
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
    app.router.add_post("/api/story_file", story_send)
    app["bot"] = bot
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))


if __name__ == "__main__":
    main()
