import html
import json
import logging
import os
import random
import time
from io import BytesIO

import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ---- settings (all secrets come from environment variables) ----
BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me-please-123")
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-large-v3-turbo")
MAX_VOICE_SECONDS = 60

COOLDOWN_SECONDS = 5
HISTORY_LIMIT = 12
MAX_TOKENS = 1500  # gpt-oss "thinks" first, and those tokens count too

LEVELS = {"a": "A1-A2 (beginner)", "b": "B1-B2 (intermediate)", "c": "C1-C2 (advanced)"}
TOPICS = ["Games", "Sport", "Music", "Movies", "Travel", "Food"]
PRAISE = ["🔥", "❤️", "✨"]

# ---- in-memory state (stage 1: lost on restart, database comes in stage 3) ----
users: dict[int, dict] = {}


def get_user(uid: int) -> dict:
    if uid not in users:
        users[uid] = {"level": None, "topic": None, "history": [],
                      "last": 0.0, "awaiting_topic": False, "active": False}
    return users[uid]


def reset_chat(u: dict) -> None:
    u.update(level=None, topic=None, history=[], awaiting_topic=False, active=False)


def build_prompt(u: dict) -> str:
    level = LEVELS.get(u["level"]) if u["level"] else None
    explain_lang = "English" if u["level"] == "c" else "Russian"
    return (
        'You are "Just Chatting", a friendly English speaking-practice partner in a Telegram chat. '
        "Use American English (spelling and vocabulary), never British.\n"
        f"Learner level: {level or 'unknown, adapt to the person as the chat goes'}. "
        f"Topic: {u['topic'] or 'free talk'}. The tone comes from the topic; you have no fixed persona.\n"
        "Match your vocabulary to the level. Keep replies short (2-4 sentences) and always end with a question.\n"
        "If the user's message is not in English, reply in English, ask them to switch to English, "
        "and return an empty list for mistakes. Do not analyze non-English text as a mistake.\n"
        "Check the user's last message carefully and list EVERY significant English mistake: grammar, "
        "missing or wrong articles, verb forms and tenses, prepositions, word choice, word order, spelling. "
        "Never skip a real mistake, even when there are several. Ignore only insignificant slips: "
        "capitalization, punctuation (commas, apostrophes, the final period) and extra spaces.\n"
        'Return ONLY a JSON object: {"reply": string, "mistakes": [{"quote": string, '
        '"explanation": string, "examples": [string, string]}]}.\n'
        "Each mistake is a separate item. quote = the exact wrong phrase taken from the user's message, "
        "as short as possible. "
        f"explanation = why it is wrong, written in {explain_lang}, short and clear. "
        "examples = two short correct example sentences in English.\n"
        "If there are no significant mistakes, return an empty list for mistakes."
    )


class AIError(Exception):
    def __init__(self, code: str):
        self.code = code


async def ask_ai(u: dict, user_text: str, save_user_text: bool = True) -> dict:
    messages = [{"role": "system", "content": build_prompt(u)}]
    messages += u["history"][-HISTORY_LIMIT:]
    messages.append({"role": "user", "content": user_text})
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
    }
    if "gpt-oss" in GROQ_MODEL:
        payload["reasoning_effort"] = "low"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json=payload,
            )
        r.raise_for_status()
        data = json.loads(r.json()["choices"][0]["message"]["content"])
        reply = str(data["reply"]).strip()
        if not reply:
            raise ValueError("empty reply")
    except httpx.HTTPStatusError as e:
        log.error("Groq HTTP error: %s %s", e.response.status_code, e.response.text[:300])
        raise AIError(str(e.response.status_code))
    except (httpx.HTTPError, TimeoutError):
        log.exception("Groq network error")
        raise AIError("503")
    except Exception:
        log.exception("Bad AI answer")
        raise AIError("422")

    if save_user_text:
        u["history"].append({"role": "user", "content": user_text})
    u["history"].append({"role": "assistant", "content": reply})
    u["history"] = u["history"][-HISTORY_LIMIT:]
    mistakes = data.get("mistakes") or []
    if not isinstance(mistakes, list):
        mistakes = []
    mistakes = [m for m in mistakes if isinstance(m, dict) and m.get("quote")]
    return {"reply": reply, "mistakes": mistakes}


async def transcribe(audio: bytes) -> str:
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                WHISPER_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": ("voice.ogg", audio, "audio/ogg")},
                data={"model": WHISPER_MODEL, "response_format": "json", "temperature": "0"},
            )
        r.raise_for_status()
        return (r.json().get("text") or "").strip()
    except httpx.HTTPStatusError as e:
        log.error("Whisper HTTP error: %s %s", e.response.status_code, e.response.text[:300])
        raise AIError(str(e.response.status_code))
    except (httpx.HTTPError, TimeoutError):
        log.exception("Whisper network error")
        raise AIError("503")
    except Exception:
        log.exception("Bad Whisper answer")
        raise AIError("422")


async def send_ai_result(message: Message, result: dict) -> None:
    mistakes = result["mistakes"][:5]
    reply = html.escape(result["reply"])
    if not mistakes:
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


async def safe_ai(message: Message, u: dict, text: str, save_user_text: bool = True) -> None:
    try:
        result = await ask_ai(u, text, save_user_text)
    except AIError as e:
        await message.answer(f"Something went wrong (error {e.code}). Try again in a minute.")
        return
    await send_ai_result(message, result)


# ---- keyboards ----
def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def level_kb():
    return kb([
        [("A1-A2", "lvl:a"), ("B1-B2", "lvl:b"), ("C1-C2", "lvl:c")],
        [("Skip", "lvl:skip")],
    ])


def topic_kb():
    rows = [[(t, f"top:{t}") for t in TOPICS[i:i + 3]] for i in range(0, len(TOPICS), 3)]
    rows.append([("✍️ Write your own", "top:own"), ("Skip", "top:skip")])
    return kb(rows)


# ---- handlers ----
router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


@router.message(Command("start", "new"))
async def cmd_start(message: Message):
    u = get_user(message.from_user.id)
    reset_chat(u)
    await message.answer(
        "Hey! 👋 I'm here to help you practice English. Just chat with me, "
        "and I'll gently point out mistakes.",
        reply_markup=kb([[("Start a conversation", "go")]]),
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    reset_chat(get_user(message.from_user.id))
    await message.answer("Chat ended. Send /new whenever you want to talk again 👋")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "I'm an English speaking-practice buddy.\n\n"
        "/new - start a new conversation\n"
        "/stop - end the conversation\n"
        "/help - this message\n\n"
        "Write to me in English. If you make a mistake, I'll explain it below my reply."
    )


@router.callback_query(F.data == "go")
async def cb_go(call: CallbackQuery):
    await call.message.answer("What's your level? You can skip this, I'll adapt as we chat.",
                              reply_markup=level_kb())
    await call.answer()


@router.callback_query(F.data.startswith("lvl:"))
async def cb_level(call: CallbackQuery):
    u = get_user(call.from_user.id)
    value = call.data.split(":", 1)[1]
    u["level"] = value if value in LEVELS else None
    await call.message.answer("Pick a topic, write your own, or skip.", reply_markup=topic_kb())
    await call.answer()


async def begin_chat(message: Message, u: dict):
    u["active"] = True
    u["awaiting_topic"] = False
    await safe_ai(message, u, "Start the conversation with a short friendly opener and a question.",
                  save_user_text=False)


@router.callback_query(F.data.startswith("top:"))
async def cb_topic(call: CallbackQuery):
    u = get_user(call.from_user.id)
    value = call.data.split(":", 1)[1]
    await call.answer()
    if value == "own":
        u["awaiting_topic"] = True
        await call.message.answer("Type the topic you want to talk about 👇")
        return
    u["topic"] = None if value == "skip" else value
    await begin_chat(call.message, u)


def cooldown_ok(uid: int, u: dict) -> bool:
    if uid == ADMIN_ID:
        return True
    now = time.time()
    if now - u["last"] < COOLDOWN_SECONDS:
        return False
    u["last"] = now
    return True


@router.message(F.voice)
async def on_voice(message: Message):
    uid = message.from_user.id
    u = get_user(uid)
    if not cooldown_ok(uid, u):
        return
    if u["awaiting_topic"]:
        await message.answer("Please type the topic as text 👇")
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
    await safe_ai(message, u, text)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    uid = message.from_user.id
    u = get_user(uid)
    if not cooldown_ok(uid, u):
        return
    if u["awaiting_topic"]:
        u["topic"] = message.text.strip()[:100]
        await begin_chat(message, u)
        return
    if not u["active"]:
        await message.answer("Send /start to begin 🙂")
        return
    await safe_ai(message, u, message.text)


@router.message(F.sticker | F.photo | F.video | F.animation | F.document | F.video_note | F.audio)
async def on_media(message: Message):
    uid = message.from_user.id
    u = get_user(uid)
    if not u["active"] or not cooldown_ok(uid, u):
        return
    kind = message.content_type.value
    note = (f"[The user sent a {kind}. React to it in English like it is a meme or a funny situation, "
            "keep it short, and at the end say you can't see or read it. Return an empty list for mistakes.]")
    await safe_ai(message, u, note, save_user_text=False)


# ---- web server (webhook + health check for the pinger) ----
async def on_startup(bot: Bot):
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", secret_token=WEBHOOK_SECRET,
                              drop_pending_updates=True)
        log.info("Webhook set")


async def health(_request):
    return web.Response(text="ok")


def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))


if __name__ == "__main__":
    main()
