import asyncio
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
AI_ATTEMPTS = 3
RETRY_STATUSES = {400, 408, 422, 429, 500, 502, 503, 504}

LEVELS = {"a": "A1-A2 (beginner)", "b": "B1-B2 (intermediate)", "c": "C1-C2 (advanced)"}
TOPICS = ["Games", "Sport", "Music", "Movies", "Travel", "Food"]
PRAISE = ["🔥", "❤️", "✨"]

# ---- chat styles: key -> (button label, instruction for the AI) ----
STYLES = {
    "friend": (
        "😊 Friend",
        "Talk like a close friend texting: relaxed, warm, natural contractions, light humor, "
        "short reactions (like 'oh nice', 'haha', 'wait, really?', 'that sounds rough', 'honestly'). "
        "Share a quick opinion or reaction of your own.",
    ),
    "slang": (
        "😎 Slang",
        "Talk like a young American friend texting, using casual slang naturally and SPARINGLY: "
        "at most one or two slang words per message, and only where a real person would say it "
        "(kinda, lowkey, no way, that's wild, dude, pretty cool, my bad). Never force it, never use "
        "outdated or meme-style slang, never sound like you are trying too hard. "
        "Keep the slang simple enough for the learner's level.",
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
    "- NEVER use filler openers or phrases like 'Sure', 'Certainly', 'Absolutely', 'Of course', "
    "'Great question', \"That's a great ...\", 'Got it', 'I understand'. Vary how you start; "
    "often just react directly.\n"
    "- Do not repeat or summarize the user's words back to them. No lists, no headings, no emojis, "
    "no exclamation marks in every message.\n"
    "- Have opinions and small preferences, and keep a consistent personality. Do not claim human "
    "experiences like having a body or a job. If the user sincerely asks whether you are an AI, "
    "answer honestly that you are.\n"
    "- If the user asks what a word or phrase means, explain it simply in English with a short example. "
    "But if they ask you to translate something into Russian (or ask in Russian), answer in Russian: "
    "first the translation, then the pronunciation of the English word in /slashes/, and mention any "
    "preposition that changes the meaning (for example 'look for' vs 'look after'); if there is none, "
    "say nothing about prepositions. For a single word "
    "keep it short and direct, do not list every possible meaning. Afterwards you may continue the chat "
    "in English.\n"
)

MISTAKE_RULES = (
    "Check the user's last message and list every REAL English mistake: wrong or missing articles, "
    "verb forms and tenses (for example, 'for/since + a period of time' with an ongoing action needs "
    "present perfect), prepositions, word choice, word order, subject-verb agreement, spelling.\n"
    "Do NOT report: informal spoken English and slang, including modern slang and memes (wanna, gonna, "
    "gotta, ain't, kinda, lemme, dunno, y'all, 'cause, \"I'm cooked\", \"he's cooking\", 'no cap', 'bet', "
    "'lowkey'), repetition for emphasis ('big big', 'very very', 'so so good'), contractions, sentence "
    "fragments, capitalization, punctuation, commas, run-on "
    "sentences, comma splices, extra spaces, or anything that is acceptable in natural spoken English. "
    "The text may come from a voice message. Report something only when you are sure it is wrong.\n"
    "Before answering, double-check that each correction and both example sentences are fully correct English.\n"
)

# ---- in-memory state (lost on restart; a database comes in stage 3) ----
users: dict[int, dict] = {}


def get_user(uid: int) -> dict:
    if uid not in users:
        users[uid] = {"level": None, "topic": None, "history": [], "style": DEFAULT_STYLE,
                      "last": 0.0, "awaiting_topic": False, "active": False}
    return users[uid]


def reset_chat(u: dict) -> None:
    # the chosen style is kept between conversations
    u.update(level=None, topic=None, history=[], awaiting_topic=False, active=False)


def build_prompt(u: dict) -> str:
    level = LEVELS.get(u["level"]) if u["level"] else None
    explain_lang = "English" if u["level"] == "c" else "Russian"
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
        '"explanation": string, "examples": [string, string]}]}.\n'
        "Each mistake is a separate item. quote = the exact wrong phrase taken from the user's message, "
        "as short as possible. "
        f"explanation = why it is wrong, written in {explain_lang}, short and clear. "
        "examples = two short correct example sentences in English.\n"
        "If there are no real mistakes, return an empty list for mistakes."
    )


class AIError(Exception):
    def __init__(self, code: str):
        self.code = code


TRIVIAL_WORDS = ("запят", "comma", "пунктуац", "punctuat", "заглавн", "прописн",
                 "capitaliz", "апостроф", "apostroph")


def is_trivial(m: dict) -> bool:
    text = str(m.get("explanation", "")).lower()
    return any(w in text for w in TRIVIAL_WORDS)


def parse_ai(content: str) -> dict:
    data = json.loads(content)
    reply = str(data["reply"]).strip()
    if not reply:
        raise ValueError("empty reply")
    mistakes = data.get("mistakes") or []
    if not isinstance(mistakes, list):
        mistakes = []
    mistakes = [m for m in mistakes
                if isinstance(m, dict) and m.get("quote") and not is_trivial(m)]
    return {"reply": reply, "mistakes": mistakes}


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
    text = text.strip()
    if len(text) < 1500 and not text.startswith("{"):
        return {"reply": text, "mistakes": []}
    return None


async def ask_ai(u: dict, user_text: str, save_user_text: bool = True) -> dict:
    messages = [{"role": "system", "content": build_prompt(u)}]
    messages += u["history"][-HISTORY_LIMIT:]
    messages.append({"role": "user", "content": user_text})
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.8,
        "response_format": {"type": "json_object"},
    }
    if "gpt-oss" in GROQ_MODEL:
        payload["reasoning_effort"] = "low"

    result = None
    last_code = "500"
    for attempt in range(AI_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json=payload,
                )
            r.raise_for_status()
            result = parse_ai(r.json()["choices"][0]["message"]["content"])
            break
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            last_code = str(code)
            log.error("Groq HTTP error (attempt %s): %s %s", attempt + 1, code, e.response.text[:500])
            if code == 400:
                result = salvage(e.response)
                if result:
                    break
            if code not in RETRY_STATUSES:
                break
        except (httpx.HTTPError, TimeoutError):
            last_code = "503"
            log.exception("Groq network error")
        except Exception:
            last_code = "422"
            log.exception("Bad AI answer")
        if attempt < AI_ATTEMPTS - 1:
            await asyncio.sleep(1.5)

    if result is None:
        raise AIError(last_code)

    if save_user_text:
        u["history"].append({"role": "user", "content": user_text})
    u["history"].append({"role": "assistant", "content": result["reply"]})
    u["history"] = u["history"][-HISTORY_LIMIT:]
    return result


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


def style_kb():
    items = [(label, f"sty:{key}") for key, (label, _) in STYLES.items()]
    return kb([items[i:i + 2] for i in range(0, len(items), 2)])


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


@router.message(Command("style"))
async def cmd_style(message: Message):
    await message.answer("Pick the vibe of our chat:", reply_markup=style_kb())


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "I'm an English speaking-practice buddy.\n\n"
        "/new - start a new conversation\n"
        "/style - change the vibe (friend, slang, classic, curious)\n"
        "/stop - end the conversation\n"
        "/help - this message\n\n"
        "Write or send voice messages in English. If you make a mistake, "
        "I'll explain it below my reply."
    )


@router.callback_query(F.data.startswith("sty:"))
async def cb_style(call: CallbackQuery):
    key = call.data.split(":", 1)[1]
    await call.answer()
    if key not in STYLES:
        return
    get_user(call.from_user.id)["style"] = key
    await call.message.answer(f"Vibe set: {STYLES[key][0]}. Just keep chatting 🙂")


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
    await safe_ai(message, u, "Start the conversation with a short, natural opener.",
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
