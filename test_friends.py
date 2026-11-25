import os, sys, types, json, asyncio
from unittest.mock import MagicMock
os.environ.update(BOT_TOKEN="1:x", ADMIN_ID="1", GEMINI_API_KEY="k", DATABASE_URL="postgres://x")

class _Base:
    def __init__(s, *a, **k): pass
class _Deco:
    def __call__(s, *a, **k): return lambda fn: fn
    def filter(s, *a, **k): pass
    def outer_middleware(s, *a, **k): pass
class _Router:
    def __init__(s): s.message = _Deco(); s.callback_query = _Deco()
for name in ["asyncpg","httpx","aiohttp","aiohttp.web","aiogram","aiogram.client","aiogram.client.default","aiogram.enums",
             "aiogram.filters","aiogram.types","aiogram.utils","aiogram.webhook","aiogram.webhook.aiohttp_server","edge_tts"]:
    sys.modules[name] = MagicMock()
sys.modules["aiogram"].BaseMiddleware = _Base
sys.modules["aiogram"].Router = _Router
import bot
real_kb = bot.kb
bot.kb = lambda rows: rows
bot.HTTPStatusError = Exception

class FakeBot:
    def __init__(s):
        s.sent, s.voices, s.actions, s.topics, s.deleted = [], [], [], [], []
        s.topic_fail = False; s.tid = 776; s.dead = set(); s.chat_fail = None
    async def send_message(s, chat_id, text, **kw):
        s.sent.append((text, kw)); return types.SimpleNamespace(message_id=5)
    async def send_voice(s, chat_id, f, **kw): s.voices.append(kw)
    async def send_chat_action(s, chat_id, action, **kw):
        if s.chat_fail: raise RuntimeError(s.chat_fail)
        if kw.get("message_thread_id") in s.dead: raise RuntimeError("Bad Request: message thread not found")
        s.actions.append((action, kw))
    async def pin_chat_message(s, *a, **k): pass
    async def unpin_chat_message(s, *a, **k): pass
    async def create_forum_topic(s, chat_id, name):
        if s.topic_fail: raise RuntimeError("topics are off")
        s.topics.append(name); s.tid += 1; return types.SimpleNamespace(message_thread_id=s.tid)
    async def delete_forum_topic(s, chat_id, message_thread_id): s.deleted.append(message_thread_id)
    async def download(s, *a, **k): pass
B = FakeBot()
class Msg:
    def __init__(s, text="", thread=None, uid=1):
        s.bot = B; s.chat = types.SimpleNamespace(id=uid, type="private"); s.from_user = types.SimpleNamespace(id=uid)
        s.kb_removed = False
        s.text = text; s.message_thread_id = thread; s.from_user.first_name = "Suren"; s.is_topic_message = bool(thread); s.content_type = types.SimpleNamespace(value="sticker")
    async def answer(s, text, **kw): await s.bot.send_message(s.chat.id, text, **kw)
    async def edit_reply_markup(s, **k): s.kb_removed = True
class Call:
    def __init__(s, data, thread=None, uid=1):
        s.data = data; s.bot = B; s.from_user = types.SimpleNamespace(id=uid); s.message = Msg("reply text", thread, uid)
        s.answered = None
    async def answer(s, text=None, **k): s.answered = (text, k)

prompts, replies = [], {"mistake": False}
async def fake_gemini(system, history, user_text):
    await asyncio.sleep(0.02)
    prompts.append((system, list(history), user_text))
    m = [{"quote": "I goed", "explanation": "went", "examples": ["I went"]}] if "goed" in user_text else []
    if "STALE" in user_text:
        m = [{"quote": "I not hungry", "explanation": "need am", "examples": ["I am not hungry"]}]
    tips = []
    if "TIPSTALE" in user_text: tips = ["Не хватает предлога: cheat on my gf"]
    if "newgame" in user_text: tips = ["Не хватает a: a new game"]
    return json.dumps({"reply": "Hey there", "mistakes": m, "tips": tips})
bot.call_gemini = fake_gemini
async def noop(*a, **k): pass
bot.register_message = noop; bot.after_user_message = noop
synth_calls = []
async def fake_tts(text, voice, accent=None): synth_calls.append((voice, accent)); return b"x", "audio/mpeg"
bot.tts_gemini = fake_tts
bot.to_ogg_opus = lambda d, m: b"ogg"

def clear():
    B.sent.clear(); B.voices.clear(); B.actions.clear(); prompts.clear(); synth_calls.clear()
def texts(): return [t for t, _ in B.sent]

async def main():
    u = await bot.get_user(1)
    await bot.upd(u, active=True)

    await bot.cmd_create(Msg("/create"))
    assert u["awaiting"] == "fr_name" and "name" in texts()[-1].lower()
    await bot.on_text(Msg("Sofia")); assert u["awaiting"] == "fr_about"
    await bot.on_text(Msg("my Colombian friend, sarcastic, loves films"))
    kbd = B.sent[-1][1]["reply_markup"]; datas = [d for row in kbd for _, d in row]
    assert "fv:gemini:Puck" in datas and "fv:edge:en-US-GuyNeural" in datas, datas
    clear()
    await bot.cb_friend_voice(Call("fv:gemini:Puck"))
    assert "fa:Spanish" in [d for row in B.sent[-1][1]["reply_markup"] for _, d in row]
    clear()
    await bot.cb_friend_accent(Call("fa:Spanish"))
    assert B.topics == ["Sofia"], B.topics
    fr = (await bot.friends_of(1))[0]
    assert fr["thread_id"] == 777 and fr["voice"] == "gemini:Puck" and fr["accent"] == "Spanish"
    assert u["pending_friend"] is None
    greet = B.sent[-1]
    assert greet[1].get("message_thread_id") == 777 and greet[0].startswith("Hey there"), greet
    sysprompt = prompts[0][0]
    assert "You are Sofia" in sysprompt and "Colombian friend" in sysprompt and "Just Chatting" not in sysprompt
    assert len(fr["history"]) == 1 and u["history"] == []
    print("create dialog + greeting in topic OK")

    clear()
    await bot.on_text(Msg("I goed to cinema", thread=777))
    assert all(kw.get("message_thread_id") == 777 for _, kw in B.sent), B.sent
    assert len(B.sent) == 2
    assert ("typing", {"message_thread_id": 777}) in B.actions
    assert len(fr["history"]) == 3 and u["history"] == []
    clear()
    await bot.on_text(Msg("hello"))
    assert all("message_thread_id" not in kw for _, kw in B.sent) and "Just Chatting" in prompts[0][0]
    assert ("typing", {}) in B.actions and len(u["history"]) == 2 and len(fr["history"]) == 3
    print("routing OK (friend history separate, replies stay in topic)")

    await bot.upd(u, voice_mode="always"); clear()
    await bot.on_text(Msg("I goed home", thread=777))
    assert B.voices == [{"message_thread_id": 777}], B.voices
    assert synth_calls == [("Puck", "Spanish")], synth_calls
    assert [t for t in texts() if t.startswith("Hey")] == []
    assert any("<blockquote>" in t for t in texts())
    assert ("record_voice", {"message_thread_id": 777}) in B.actions
    await bot.upd(u, voice_mode="button"); clear()
    await bot.on_text(Msg("hi again", thread=777))
    assert B.sent[0][1]["reply_markup"] == [[("🔊 Listen", "tts:go")]]
    clear(); await bot.cb_tts(Call("tts:go", thread=777))
    assert B.voices == [{"message_thread_id": 777}] and synth_calls == [("Puck", "Spanish")]
    await bot.upd(u, voice_mode="off")
    print("voice in topic (voice, accent, no text dup) OK")

    n_default = len(u["history"]); clear()
    await bot.cmd_new(Msg("/new", thread=777))
    assert fr["history"] == [] and len(u["history"]) == n_default and "Fresh start" in texts()[-1]
    print("/new in topic OK")

    clear(); await bot.on_media(Msg("", thread=777))
    assert B.sent and all(kw.get("message_thread_id") == 777 for _, kw in B.sent)

    clear(); await bot.cmd_friends(Msg("/friends"))
    assert "Sofia" in texts()[-1]
    rows = B.sent[-1][1]["reply_markup"]; assert ("🗑 Sofia", f"fr:del:{fr['id']}") in [b for r in rows for b in r]
    clear(); await bot.cb_friends(Call(f"fr:del:{fr['id']}")); assert "Delete Sofia" in texts()[-1]
    await bot.cb_friends(Call(f"fr:yes:{fr['id']}"))
    assert B.deleted == [777] and await bot.friends_of(1) == []
    print("/friends + delete OK")

    B.topic_fail = True; clear()
    await bot.cmd_create(Msg("/create")); await bot.on_text(Msg("Diego"))
    await bot.on_text(Msg("a guy with a thick Spanish accent")); await bot.cb_friend_voice(Call("fv:edge:en-US-GuyNeural"))
    assert "BotFather" in texts()[-1] and B.sent[-1][1]["reply_markup"] == [[("Try again", "fc:go")]]
    assert u["pending_friend"]["name"] == "Diego" and await bot.friends_of(1) == []
    B.topic_fail = False; clear()
    await bot.cb_friend_retry(Call("fc:go"))
    fr2 = (await bot.friends_of(1))[0]
    assert fr2["name"] == "Diego" and fr2["voice"] == "edge:en-US-GuyNeural" and fr2["accent"] is None
    print("topic failure + retry + edge skips accent OK")

    await bot.cmd_create(Msg("/create")); await bot.on_text(Msg("Anna")); await bot.on_text(Msg("a chatty neighbor"))
    await bot.cb_friend_voice(Call("fv:gemini:Kore")); await bot.cb_friend_accent(Call("fa:other"))
    assert u["awaiting"] == "fr_accent"
    await bot.on_text(Msg("Scottish"))
    assert (await bot.friends_of(1))[-1]["accent"] == "Scottish"
    print("custom accent OK")

    await bot.upd(u, voice_mode="always"); clear()
    await bot.on_text(Msg("I goed home"))
    assert B.voices == [{}] and not [t for t in texts() if t.startswith("Hey")]
    async def boom(*a, **k): raise RuntimeError("tts down")
    bot.tts_gemini = boom
    async def edge_boom(*a, **k): raise RuntimeError("edge down")
    bot.tts_edge = edge_boom; clear()
    await bot.on_text(Msg("hello"))
    assert [t for t in texts() if t.startswith("Hey")]
    print("default chat voice-only + fallback OK")


    await bot.upd(u, voice_mode="off"); clear()
    await bot.on_text(Msg("Do you want to talk about our update? STALE"))
    assert not any("<blockquote>" in t for t in texts()), texts()
    clear(); await bot.on_text(Msg("I goed home"))
    assert any("<blockquote>" in t for t in texts())
    assert "Check ONLY the user's last message" in prompts[-1][0]
    print("stale mistake filter OK")

    import time as _t
    hint_calls = []
    async def fake_ai_json(system, user_text, history=None):
        hint_calls.append(list(history or [])); return {"hints": [{"en": "What movie?", "ru": "Какой фильм?"}]}
    bot.ai_json = fake_ai_json
    mx = await bot.add_friend(1, "Max", "grumpy neighbor", "gemini:Puck", None, 888)
    main_bot_ts = u.get("last_bot_ts"); clear()
    await bot.on_text(Msg("I goed out", thread=888))
    assert mx["last_bot_ts"] and u.get("last_bot_ts") == main_bot_ts
    clear(); await bot.friend_hints(B, u)
    assert B.sent == []
    mx["last_user_ts"] = _t.time() - 500; mx["last_bot_ts"] = _t.time() - 400; clear()
    await bot.friend_hints(B, u)
    assert len(B.sent) == 1 and "You could say" in B.sent[0][0] and B.sent[0][1] == {"message_thread_id": 888}
    assert hint_calls[-1] == mx["history"][-6:] and hint_calls[-1] != []
    clear(); await bot.friend_hints(B, u); assert B.sent == []
    await bot.on_text(Msg("ok I am here", thread=888))
    assert mx["hint_sent"] is False and mx["last_user_ts"]
    mx["last_bot_ts"] = _t.time() - 400; mx["last_user_ts"] = _t.time() - 500; mx["hint_sent"] = False
    await bot.upd(u, hints_on=False); clear(); await bot.friend_hints(B, u)
    assert B.sent == []
    await bot.upd(u, hints_on=True)
    clear(); await bot.cmd_hint(Msg("/hint", thread=888))
    assert B.sent[0][1] == {"message_thread_id": 888} and "You could say" in B.sent[0][0]
    print("friend hints OK")

    bot.InlineKeyboardButton = lambda **kw: kw
    bot.InlineKeyboardMarkup = lambda inline_keyboard: inline_keyboard
    bot.kb = real_kb
    await bot.upd(u, emoji=True, tips_on=False, voice_mode="off")
    rows = bot.settings_kb(u)
    flat = {b["callback_data"]: b for r in rows for b in r}
    assert flat["set:emoji"]["style"] == "success" and flat["set:emoji"]["text"].startswith("✅")
    assert flat["set:tips"]["style"] == "danger" and flat["set:tips"]["text"].startswith("❌")
    assert flat["set:voice"]["style"] == "danger"
    await bot.upd(u, voice_mode="always"); flat = {b["callback_data"]: b for r in bot.settings_kb(u) for b in r}
    assert flat["set:voice"]["style"] == "success"
    assert "style" not in flat["set:lang"] and "style" not in flat["set:tz"]
    plain = real_kb([[("A", "a")]]); assert plain == [[{"text": "A", "callback_data": "a"}]]
    print("coloured settings OK")


    await bot.upd(u, history=[], voice_mode="off", active=True)
    mx["history"] = []
    lia = await bot.add_friend(1, "Lia", "cheerful student from Lisbon", "gemini:Kore", None, 999)
    clear()
    for text, th in (("ALPHA main one", None), ("BRAVO max one", 888), ("CHARLIE lia one", 999),
                     ("ALPHA main two", None), ("BRAVO max two", 888), ("CHARLIE lia two", 999)):
        await bot.on_text(Msg(text, thread=th))
    seen = [(sysm, [m["content"] for m in hist], txt) for sysm, hist, txt in prompts]
    for sysm, hist, txt in seen:
        mark = txt.split()[0]
        others = {"ALPHA", "BRAVO", "CHARLIE"} - {mark}
        assert not any(o in h for h in hist for o in others), (txt, hist)
        persona = {"ALPHA": "Just Chatting", "BRAVO": "grumpy neighbor", "CHARLIE": "student from Lisbon"}[mark]
        assert persona in sysm and not any(p in sysm for k, p in
               {"ALPHA": "Just Chatting", "BRAVO": "grumpy neighbor", "CHARLIE": "student from Lisbon"}.items() if k != mark)
    assert [m["content"] for m in u["history"] if m["role"] == "user"] == ["ALPHA main one", "ALPHA main two"]
    assert [m["content"] for m in mx["history"] if m["role"] == "user"] == ["BRAVO max one", "BRAVO max two"]
    assert [m["content"] for m in lia["history"] if m["role"] == "user"] == ["CHARLIE lia one", "CHARLIE lia two"]
    print("history isolation OK")

    clear(); await bot.cmd_stop(Msg("/stop", thread=888))
    assert u["active"] and u["history"] and "/new" in texts()[-1]
    print("/stop in a friend topic OK")

    await bot.upd(u, history=[], tips_on=True); clear()
    await bot.on_text(Msg("i cheat my gf"))
    clear(); await bot.on_text(Msg("Do you want talk? TIPSTALE"))
    assert not any("💡" in t for t in texts()), texts()
    clear(); await bot.on_text(Msg("I want new game newgame"))
    assert any("💡" in t and "a new game" in t for t in texts()), texts()
    assert bot.tip_repeats_old("Не хватает предлога: cheat on my gf", "do you want talk",
                               [{"role": "user", "content": "i cheat my gf"}])
    assert not bot.tip_repeats_old("Опечатка: lile → like", "i lile it", [{"role": "user", "content": "i cheat my gf"}])
    print("stale tip filter OK")


    await bot.upd(u, history=[], active=False, main_thread=None, cmd_thread=None, voice_mode="off", hints_on=True)
    B.topics.clear(); clear()
    await bot.cmd_start(Msg("/start"))
    assert B.topics == ["Main", "Commands"], B.topics
    main, cmd = u["main_thread"], u["cmd_thread"]
    assert main and cmd and main != cmd and bot.topics_on(u)
    assert B.sent[0][1].get("message_thread_id") == main and "Commands topic" in B.sent[0][0]
    assert "message_thread_id" not in B.sent[1][1] and "Main" in B.sent[1][0]
    await bot.upd(u, active=True)
    print("/start creates Main + Commands, onboarding in Main OK")

    clear(); await bot.on_text(Msg("hello main", thread=main))
    assert prompts and all(kw.get("message_thread_id") == main for _, kw in B.sent) and len(u["history"]) == 2
    n_hist = len(u["history"])
    clear(); await bot.on_text(Msg("hi", thread=cmd))
    assert prompts == [] and "for commands" in B.sent[0][0] and B.sent[0][1] == {"message_thread_id": cmd}
    clear(); await bot.on_text(Msg("hi", thread=4242)); await bot.on_voice(Msg("", thread=4242)) if False else None
    assert prompts == [] and "chat in Main" in B.sent[0][0] and B.sent[0][1] == {"message_thread_id": 4242}
    clear(); await bot.on_media(Msg("", thread=4242)); assert prompts == [] and B.sent == []
    assert len(u["history"]) == n_hist
    clear(); await bot.on_text(Msg("hello max", thread=888))
    assert prompts and "grumpy neighbor" in prompts[0][0] and all("hello main" not in m["content"] for m in prompts[0][1])
    assert len(u["history"]) == n_hist and B.sent[0][1].get("message_thread_id") == 888
    print("chat only in Main / friends, Commands + stray topics answer with a pointer OK")

    mw = bot.TopicMiddleware(); ran = []
    async def h(event, data): ran.append(event.text); return "ran"
    async def tryit(text, th):
        ran.clear(); clear(); await mw(h, Msg(text, thread=th), {}); return bool(ran)
    assert not await tryit("/settings", main) and "Commands live in the Commands topic" in texts()[-1]
    assert not await tryit("/stats", 888)
    assert await tryit("/settings", cmd) and await tryit("/friends", cmd)
    assert await tryit("/hint", main) and await tryit("/new", main) and await tryit("/stop", main)
    assert await tryit("/new", 888) and await tryit("/hint", 888)
    assert await tryit("/start", 4242) and await tryit("/start", main) and await tryit("/start", None)
    assert not await tryit("/settings", 4242) and not await tryit("/create", main)
    assert await tryit("/settings@Just_Bot", cmd) and not await tryit("/settings@Just_Bot", main)
    await bot.upd(u, main_thread=None, cmd_thread=None)
    assert await tryit("/settings", 4242) and await tryit("/settings", None)
    await bot.upd(u, main_thread=main, cmd_thread=cmd)
    print("command guard OK")

    clear(); await bot.announce_day(B, u, 3)
    assert B.sent[0][1].get("message_thread_id") == main
    import time as _t2
    await bot.upd(u, active=True, hints_on=True, hint_sent=False, last_user_ts=_t2.time() - 900,
                  last_main_ts=_t2.time() - 900, last_bot_ts=_t2.time() - 800)
    clear(); await bot.nudge_one(B, u)
    assert B.sent and B.sent[0][1].get("message_thread_id") == main and "You could say" in B.sent[0][0]
    print("bot-initiated messages go to Main OK")

    B.dead.add(main); B.topics.clear(); clear()
    await bot.cmd_start(Msg("/start", thread=cmd))
    assert B.topics == ["Main"] and u["main_thread"] != main and u["cmd_thread"] == cmd
    assert B.sent[0][1].get("message_thread_id") == u["main_thread"]
    main = u["main_thread"]; B.topics.clear(); B.chat_fail = "Timed out"; clear()
    await bot.cmd_start(Msg("/start", thread=cmd))
    assert B.topics == [] and u["main_thread"] == main
    B.chat_fail = None
    print("topic self-heal OK")

    await bot.upd(u, main_thread=None, cmd_thread=None); B.topic_fail = True; clear()
    await bot.cmd_start(Msg("/start"))
    assert not bot.topics_on(u) and len(B.sent) == 1 and "message_thread_id" not in B.sent[0][1]
    B.topic_fail = False
    print("topics off fallback OK")


    import time as _t3
    await bot.upd(u, main_thread=7001, cmd_thread=7002, active=True, hints_on=True, voice_mode="off", tips_on=False)
    clear(); await bot.on_text(Msg("hello main again", thread=7001))
    assert u["last_main_ts"] and u["last_bot_ts"]
    now = _t3.time()
    await bot.upd(u, hint_sent=True)
    clear(); await bot.on_text(Msg("hello max", thread=888))
    assert u["hint_sent"] is True and u["last_main_ts"] < u["last_user_ts"]
    await bot.upd(u, hint_sent=False, last_main_ts=now - 900, last_bot_ts=now - 800)
    clear(); await bot.nudge_one(B, u)
    assert B.sent and B.sent[0][1].get("message_thread_id") == 7001 and "You could say" in B.sent[0][0], B.sent
    await bot.upd(u, hint_sent=False, last_main_ts=None, last_user_ts=now - 900, last_bot_ts=now - 800)
    clear(); await bot.nudge_one(B, u)
    assert B.sent and "You could say" in B.sent[0][0]
    print("Main silence hint independent of friends OK")

    clear(); await bot.cmd_create(Msg("/create"))
    assert u["awaiting"] == "fr_name" and "/cancel" in texts()[-1]
    clear(); await bot.cmd_cancel(Msg("/cancel"))
    assert u["awaiting"] is None and u["pending_friend"] is None and texts() == ["Cancelled 👌"]
    clear(); await bot.cmd_cancel(Msg("/cancel")); assert texts() == ["Nothing to cancel."]
    await bot.upd(u, awaiting="goal_days", pending_goal={"items": ["x"]}); clear()
    await bot.cmd_cancel(Msg("/cancel", thread=888))
    assert u["awaiting"] is None and u["pending_goal"] is None and texts() == ["Cancelled 👌"]
    assert "/cancel" in bot.GOAL_ASK_TEXT
    await bot.upd(u, awaiting="tz"); await bot.cmd_cancel(Msg("/cancel", thread=7001)); clear()
    await bot.on_text(Msg("Europe/Berlin please", thread=7001))
    assert prompts, "the text must go to the chat now"
    mw = bot.TopicMiddleware(); ran = []
    async def h2(event, data): ran.append(1)
    for th in (7001, 7002, 888, 4242, None):
        ran.clear(); await mw(h2, Msg("/cancel", thread=th), {}); assert ran, th
    print("/cancel OK")


    import inspect
    calls = []
    class Resp:
        def __init__(s, d): s.d = d
        def json(s): return s.d
    class Http:
        script = {}
        def __init__(s, *a, **k): pass
        async def __aenter__(s): return s
        async def __aexit__(s, *a): return False
        async def post(s, url, json=None):
            method = url.rsplit("/", 1)[1]; calls.append((method, json))
            out = Http.script[method]
            if isinstance(out, Exception): raise out
            return Resp(out)
    bot.httpx = types.SimpleNamespace(AsyncClient=Http, HTTPError=Exception, Response=object)
    Http.script = {"getMe": {"ok": True, "result": {"id": 1, "has_topic_sidebar": True}}, "setMySettings": {"ok": True, "result": True}}
    calls.clear(); await bot.use_topic_tabs()
    assert calls == [("getMe", {}), ("setMySettings", {"has_topic_sidebar": False})], calls
    Http.script["getMe"] = {"ok": True, "result": {"id": 1, "has_topic_sidebar": False}}
    calls.clear(); await bot.use_topic_tabs(); assert calls == [("getMe", {})]
    Http.script["getMe"] = {"ok": True, "result": {"id": 1}}
    calls.clear(); await bot.use_topic_tabs(); assert calls[-1][0] == "setMySettings"
    Http.script["setMySettings"] = {"ok": False, "description": "Bad Request: unknown parameter"}
    await bot.use_topic_tabs()
    Http.script["getMe"] = RuntimeError("network down")
    await bot.use_topic_tabs()
    assert "use_topic_tabs" in inspect.getsource(bot.on_startup)
    print("topic tabs at startup OK")


    import datetime as _dt
    def datas(kbd): return [b["callback_data"] if isinstance(b, dict) else b[1] for row in kbd for b in row]
    await bot.upd(u, main_thread=7001, cmd_thread=7002, history=[{"role": "user", "content": "hi"}], active=True,
                  streak=5, last_day=_dt.date(2026, 11, 22))
    bot.local_now = lambda uu: _dt.datetime(2026, 11, 23, 8, 0)
    clear(); await bot.cmd_start(Msg("/start", thread=7002))
    text, kw = B.sent[0]
    assert kw["message_thread_id"] == 7001 and "Suren" in text and "🔥 Day 5" in text and "Commands topic" in text
    assert any(g.split("{")[0] in text for g in bot.GREETINGS["morning"]), text
    assert "already chatting" in text and "/stop" in text and "/new" in text
    d = datas(kw["reply_markup"]); assert d == ["st:cont", "st:new", "st:friend", "st:how"], d
    assert len(u["history"]) == 1 and "Main" in B.sent[1][0]
    bot.local_now = lambda uu: _dt.datetime(2026, 11, 23, 23, 30)
    await bot.upd(u, history=[], streak=0, active=False); clear(); await bot.cmd_start(Msg("/start", thread=7001))
    assert datas(B.sent[0][1]["reply_markup"])[0] == "go" and "🔥" not in B.sent[0][0] and len(B.sent) == 1
    assert any(g.split("{")[0] in B.sent[0][0] for g in bot.GREETINGS["night"]) and "already chatting" not in B.sent[0][0]
    await bot.upd(u, history=[{"role": "user", "content": "x"}]); clear(); await bot.cmd_start(Msg("/start", thread=888))
    assert "chatting with" in texts()[0] and u["history"]
    await bot.upd(u, active=True)
    clear(); await bot.cmd_new(Msg("/new", thread=7002))
    assert "middle of a conversation" in texts()[0] and "/stop" in texts()[0] and u["history"]
    assert datas(B.sent[0][1]["reply_markup"]) == ["nw:yes", "nw:no"]
    clear(); await bot.cb_new_confirm(Call("nw:no", thread=7002)); assert "keep chatting" in texts()[0] and u["history"]
    clear(); await bot.cb_new_confirm(Call("nw:yes", thread=7002))
    assert u["history"] == [] and B.sent[0][1]["message_thread_id"] == 7001 and "Fresh start" in B.sent[0][0]
    assert any(x.startswith("lvl:") for x in datas(B.sent[0][1]["reply_markup"])) and "Main" in B.sent[1][0]
    await bot.upd(u, active=False); clear(); await bot.cmd_new(Msg("/new", thread=7002))
    assert "Fresh start" in texts()[0]
    await bot.upd(u, active=False); clear(); await bot.cb_start_menu(Call("st:cont", thread=7001))
    assert u["active"] and "listening" in texts()[0]
    await bot.upd(u, history=[{"role": "user", "content": "x"}])
    clear(); await bot.cb_start_menu(Call("st:new", thread=7001)); assert "middle of a conversation" in texts()[0]
    clear(); await bot.cb_new_confirm(Call("nw:yes", thread=7001))
    assert u["history"] == [] and len(B.sent) == 1
    clear(); await bot.cb_start_menu(Call("st:friend", thread=7001))
    assert u["awaiting"] == "fr_name" and B.sent[0][1].get("message_thread_id") == 7002 and "Commands" in B.sent[1][0]
    await bot.upd(u, awaiting=None, pending_friend=None)
    clear(); await bot.cb_start_menu(Call("st:how", thread=7001)); assert "How it works" in texts()[0]
    print("/start welcome + /new + start menu OK")


    bot.Message, bot.CallbackQuery = Msg, Call
    await bot.upd(u, active=False, history=[], awaiting=None, level=None, topic=None, voice_mode="off")
    lvl = next(iter(bot.LEVELS)); clear()
    c = Call("go"); await bot.cb_go(c); assert c.message.kb_removed and "level" in texts()[-1].lower()
    c = Call(f"lvl:{lvl}"); await bot.cb_level(c); assert c.message.kb_removed and "topic" in texts()[-1].lower()
    clear()
    ca, cb2 = Call("top:skip"), Call("top:skip")
    await asyncio.gather(bot.cb_topic(ca), bot.cb_topic(cb2))
    assert len(prompts) == 1 and u["active"], len(prompts)
    assert ca.message.kb_removed and cb2.message.kb_removed
    n_prompts = len(prompts)
    for cbq in (Call("top:skip"), Call("go"), Call(f"lvl:{lvl}")):
        await getattr(bot, {"top": "cb_topic", "go": "cb_go", "lvl": "cb_level"}[cbq.data.split(":")[0]])(cbq)
        assert cbq.answered == (bot.ALREADY_CHATTING, {"show_alert": True}), cbq.answered
    assert len(prompts) == n_prompts
    await bot.cmd_new(Msg("/new")); await bot.cb_new_confirm(Call("nw:yes")); assert not u["active"]
    clear(); c = Call("top:skip"); await bot.cb_topic(c); assert len(prompts) == 1 and u["active"]
    print("onboarding guard OK")

    mwa = bot.AccessMiddleware(); hits = []; users_asked = []
    async def hh(event, data): hits.append(1)
    real_get_user = bot.get_user
    async def spy(uid): users_asked.append(uid); return await real_get_user(uid)
    bot.get_user = spy; bot.allowed.discard(99); clear()
    await mwa(hh, Msg("hi", uid=99), {}); assert texts() == ["This is a private bot."]
    clear(); await mwa(hh, Msg("hi again", uid=99), {}); await mwa(hh, Msg("/start", uid=99), {}); assert texts() == []
    cq = Call("go", uid=99); await mwa(hh, cq, {}); assert cq.answered == (None, {"show_alert": False})
    bot.stranger_notice[99] -= 700; clear(); await mwa(hh, Msg("hi", uid=99), {}); assert texts() == ["This is a private bot."]
    assert hits == [] and users_asked == [] and "prepare_story" not in dir(bot)
    bot.allowed.add(99); await mwa(hh, Msg("hi", uid=99), {}); assert hits == [1]
    bot.allowed.discard(99); bot.get_user = real_get_user
    print("stranger rate limit OK")

    import tempfile, os as _os, time as _tm, story as _story
    tmp = tempfile.mkdtemp(); _story.OUT_DIR = tmp
    for i in range(1, 7):
        for suffix in ("", ".meta"):
            path = _os.path.join(tmp, f"story_{i}.mp4{suffix}"); open(path, "w").write("x")
            _os.utime(path, (_tm.time() - 100 + i, _tm.time() - 100 + i))
    open(_os.path.join(tmp, "keep.txt"), "w").write("x")
    _story._prune()
    left = sorted(_os.listdir(tmp))
    assert left == ["keep.txt", "story_4.mp4", "story_4.mp4.meta", "story_5.mp4", "story_5.mp4.meta",
                    "story_6.mp4", "story_6.mp4.meta"], left
    print("story cache prune OK")

    ex = []
    class Conn:
        async def execute(s, q, *a): ex.append(q)
        async def fetch(s, q, *a): return []
    class Acq:
        async def __aenter__(s): return Conn()
        async def __aexit__(s, *a): return False
    class Pool:
        def acquire(s): return Acq()
    async def cp(*a, **k): return Pool()
    bot.asyncpg.create_pool = cp
    await bot.init_db()
    assert any("CREATE TABLE IF NOT EXISTS friends" in q for q in ex)
    assert any("ALTER TABLE friends ADD COLUMN IF NOT EXISTS last_user_ts" in q for q in ex)
    for t in ("users", "allowed", "friends"):
        assert f"ALTER TABLE public.{t} ENABLE ROW LEVEL SECURITY" in ex
    print("init_db friends table + RLS OK")
    print("ALL OK")

asyncio.run(main())
