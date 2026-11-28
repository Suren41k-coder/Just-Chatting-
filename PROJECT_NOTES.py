# =============================================================================
#  PROJECT_NOTES.py  ·  Just Chatting (ENG)  ·  @TheBotToTalk_bot
# =============================================================================
#  Этот файл НЕ запускается и ничего не импортирует. Тут живёт всё, что раньше
#  было комментариями и докстрингами в bot.py, story.py и test_friends.py,
#  плюс карта проекта и готовые промпты для других нейросетей.
#  Код в основных файлах чистый, без комментариев.
#
#  КАК ПОЛЬЗОВАТЬСЯ ПРИ ЛИМИТЕ:
#    1. Открой новый чат в любой нейросети.
#    2. Вставь PROMPT_CONTEXT (ниже), потом нужный промпт: PROMPT_BUGFIX,
#       PROMPT_FEATURE, PROMPT_REVIEW или PROMPT_TESTS.
#    3. Прикрепи bot.py, story.py, test_friends.py, requirements.txt и этот файл.
#
#  КАК ДЕРЖАТЬ ФАЙЛ АКТУАЛЬНЫМ:
#    Новые комментарии в код не пишем. Любая правка кода = пара строк сюда
#    (раздел CHANGELOG в конце). Так основные файлы остаются чистыми.
# =============================================================================


# =============================================================================
#  1. ЧТО ЭТО
# =============================================================================
#  Telegram-бот «Just Chatting (ENG)» (@TheBotToTalk_bot) для практики
#  английского. Только для владельца. Чужие получают «private bot»,
#  доступ даётся командой /allow ID.
#
#  Стек: Python, aiogram 3 (>=3.13,<4), webhook на aiohttp.
#  Файлы: bot.py (всё остальное), story.py (картинка/видео «N days in a row»),
#         test_friends.py (тесты на заглушках), requirements.txt.
#  Хостинг: Render, новый аккаунт, бесплатный план, автодеплоя нет,
#           только Manual Deploy.
#  База: Supabase Postgres через Session pooler (переменная DATABASE_URL).
#        Без базы бот работает на памяти (in-memory fallback).
#  ИИ: Gemini 3.5 Flash Lite, запасной Groq.
#  Голос: расшифровка Gemini -> Groq Whisper; озвучка Gemini TTS -> Edge TTS.
#  Все секреты берутся из переменных окружения (BOT_TOKEN, ADMIN_ID,
#  GEMINI_API_KEY, DATABASE_URL и т.д.).


# =============================================================================
#  2. КАРТА bot.py (искать по названиям разделов)
# =============================================================================
#    settings ........ переменные окружения и константы
#    styles/prompt ... 4 стиля разговора и правила, которые уходят в ИИ
#    storage ......... Postgres (Supabase) + запасной вариант в памяти
#    topics/friends .. темы Main/Commands, друзья, say(), Typing, route()
#    streak .......... стрик, закреп, утреннее сообщение
#    ai .............. Gemini (основной), Groq (запасной), разбор ответа, фильтры подсказок
#    voice ........... расшифровка и озвучка
#    stage 4 ......... статистика, цели, подсказки, напоминания, голосовые ответы
#    handlers ........ команды, /settings, сообщения, колбэки, друзья
#    web server ...... webhook, health check для пингера, страница истории (mini app)


# =============================================================================
#  3. КАК УСТРОЕН БОТ (важные правила поведения)
# =============================================================================
#  Темы (Bot API 10.5)
#    - При запуске бот вызывает setMySettings с has_topic_sidebar=false,
#      темы показываются вкладками.
#    - /start создаёт темы Main и Commands (колонки main_thread, cmd_thread).
#    - Main: единственное место обычного разговора, знакомства и всего, что
#      бот пишет сам (стрик, утреннее, напоминания, подсказки).
#    - Commands: все команды.
#    - В Main и в темах друзей работают только /start, /new, /stop, /hint,
#      /cancel (TopicMiddleware).
#    - Текст в Commands получает ответ-указатель на Main.
#    - Лишние темы. Когда пишешь во вкладку All, Telegram сам создаёт новую
#      тему и присылает служебное сообщение forum_topic_created (на телефоне
#      сам текст приходит без message_thread_id, поэтому ответ идёт в общий
#      чат). Бот ловит это служебное сообщение в AccessMiddleware (пропускает
#      его, даже если оно от аккаунта бота) и в TopicMiddleware, откуда
#      вызывается drop_stray(): через STRAY_GRACE секунд проверяет, что тема не
#      Main, не Commands, не тема друга и не создана самим ботом (bot_topics),
#      удаляет её через delete_forum_topic и пишет в Main подсказку
#      STRAY_NOTICE (не чаще раза в 20 секунд). Сообщения в неизвестной теме
#      тоже запускают удаление. /start и /cancel в такой теме сначала
#      выполняются, потом тема удаляется. Если удалить не вышло, указатель
#      пишется внутри темы, а причина попадает в лог.
#    - Закрепить темы Main и Commands бот не может: в Bot API нет метода для
#      закрепления тем (в MTProto он только для пользователей). Закреплять надо
#      вручную: долгое нажатие на тему и Pin.
#    - Проверка жива ли тема (topic_alive): 3 попытки с паузой, новая тема
#      создаётся только если все 3 раза Telegram ответил "thread not found".
#      В логах видно: "probe N/3 failed", "topic ... is gone", "created the
#      ... topic", "extra topic ... checking", "deleted the extra topic".
#
#  Друзья
#    - /create: имя, характер, голос, акцент (акцент только для Gemini-голосов).
#    - /friends: список, удаление с подтверждением.
#    - Таблица friends, у каждого друга своя тема, история, промпт и подсказки
#      молчания. История каждого чата изолирована.
#
#  Старт и разговор
#    - /start: приветствие (по времени суток, стрик, кнопки), историю не стирает.
#    - /new при идущем разговоре спрашивает подтверждение.
#    - Старые кнопки знакомства не запускают второй разговор.
#    - Разговор в 4 стилях, разбор ошибок и 💡 (цитата ошибки должна быть в
#      текущем сообщении, старые подсказки отфильтрованы), перевод.
#
#  Стикер для истории (story.py)
#    - Файл стикера кладётся рядом с story.py или в assets/. Форматы (в
#      порядке приоритета): flame.apng, flame.webp (анимированный), flame.webm,
#      flame.mov, flame.gif, flame.png. WebM и MOV читаются через ffmpeg,
#      нужна прозрачность (WebM VP9 alpha, MOV ProRes 4444), иначе будет
#      прямоугольник. Максимум MAX_CLIP_SECONDS секунд из ролика.
#    - Вёрстка 720x1280 по образцу скриншота Duolingo: стикер шириной
#      FLAME_WIDTH=300 (видимая часть около 230 пикселей), под ним число
#      (ширина около 128 px на цифру, не больше NUMBER_MAX_W=400), под числом
#      "days in a row" (ширина 85% от числа, от 260 до 380). Отступы
#      GAP_STICKER_NUMBER=60 и GAP_NUMBER_LABEL=40. Вся группа (стикер, число,
#      подпись) стоит по вертикали вокруг GROUP_CENTER_Y (середина холста),
#      по горизонтали всё по центру.
#    - Размер текста не зависит от шрифта: текст рисуется крупно (TEXT_RENDER_SIZE),
#      обрезается по буквам и сжимается до нужной ширины (_sprite). Поэтому
#      встроенные размеры шрифта не важны. Хочешь крупнее или мельче: меняй
#      NUMBER_PER_DIGIT, NUMBER_MAX_W, LABEL_RATIO.
#    - Центр стикера считается по телу монеты (медиана), а не по кольцу.
#    - Память: кадры стикера читаются по одному (не лежат в памяти), один круг
#      кодируется ffmpeg в временный файл и повторяется через -stream_loop
#      без перекодирования, ffmpeg работает в 2 потока. Пик Python на тесте
#      был 49 МБ против 125 МБ раньше. После смены вёрстки кэш обновляется
#      сам (LAYOUT_VERSION в подписи кэша).
#
#  Остальное
#    - Стрик с закрепом, /settings с цветными кнопками (зелёное включено,
#      красное выключено), /setagoal, /hint, /stats (виджет), напоминания.
#    - Голосовые ответы: off / button / always. В always только голосовое,
#      без дубля текста.
#    - /voicetest, /cancel.
#    - Чужие получают «private bot» не чаще раза в 10 минут.
#    - Файлы историй рендерятся только при открытии, в кэше 3 последних.
#    - Все ответы идут через say(message, text, thread_id), статус
#      typing/recording через класс Typing.
#    - RLS включается в init_db для users, allowed, friends.
#
#  Отложено
#    - Истории (кнопка Publish story).
#    - Раздельная статистика по друзьям.
#    - Виджеты для старта и настроек (ждём, пока Telegram включит).
#    - Сворачиваемый разбор ошибок (blockquote expandable).


# =============================================================================
#  4. ПРАВИЛА РАБОТЫ С ИИ (для любой нейросети)
# =============================================================================
#    - Код пишем только после слова «пиши» (или явного «делай»).
#      Иначе только разбор и план.
#    - Отвечать по-русски, на «ты», коротко (читаю с телефона).
#    - Не писать лишнего.
#    - Проверять тестами на заглушках: test_friends.py (aiogram в песочнице
#      не ставится, библиотеки заглушены). Запускать после каждой правки и
#      добавлять новые проверки.
#    - После правки отдавать файлы и писать точные шаги проверки после
#      деплоя. Шаги должны совпадать с реальным поведением бота.
#    - Когда присылаю лог или скриншот, сначала разобрать, что в нём видно.
#    - В код комментарии не добавлять, пояснения писать в PROJECT_NOTES.py.
#
#  Как запускать тесты:   python test_friends.py   (в конце должно быть ALL OK)
#  Деплой:                Render -> Manual Deploy (автодеплоя нет).


# =============================================================================
#  5. ПРОМПТЫ ДЛЯ ДРУГИХ НЕЙРОСЕТЕЙ
# =============================================================================

PROMPT_CONTEXT = """
Ты помогаешь мне с Telegram-ботом «Just Chatting (ENG)» (@TheBotToTalk_bot)
для практики английского. Бот только для меня, чужим он отвечает «private bot».
К сообщению приложены bot.py, story.py, test_friends.py, requirements.txt и
PROJECT_NOTES.py. Сначала прочитай PROJECT_NOTES.py: там карта проекта,
правила поведения бота и оригинальные комментарии из кода. В самом коде
комментариев нет специально.

Стек: Python, aiogram 3 (>=3.13,<4), webhook на aiohttp. Render (бесплатный,
автодеплоя нет, Manual Deploy), Supabase (Session pooler, DATABASE_URL),
Gemini 3.5 Flash Lite и запасной Groq. Голос: расшифровка Gemini -> Groq
Whisper, озвучка Gemini TTS -> Edge TTS.

Мои правила:
1. Пиши код только после слова «пиши» (или явного «делай»), иначе только
   разбор и план.
2. Отвечай по-русски на «ты», коротко: я с телефона.
3. Не пиши лишнего.
4. Не добавляй комментарии и докстринги в код. Пояснения для будущих правок
   давай отдельным блоком для PROJECT_NOTES.py (раздел CHANGELOG).
5. Проверяй тестами на заглушках: test_friends.py (aiogram не ставится,
   библиотеки заглушены). Запускай после каждой правки и добавляй новые
   проверки. Если у тебя нет среды для запуска, прямо скажи об этом и
   опиши, что именно нужно прогнать мне.
6. После правки отдавай изменённые файлы целиком (или чёткий diff, если
   файлы не отдать) и пиши точные шаги проверки после деплоя. Шаги должны
   совпадать с реальным поведением бота.
7. Когда я пришлю лог или скриншот, сначала разбери, что в нём видно, и
   только потом предлагай правку.

Подтверди, что прочитал, одной строкой, и жди задачу.
"""

PROMPT_BUGFIX = """
Баг. Лог или скриншот ниже. Сначала разбери, что в них видно: какая строка
лога, какой хендлер, какое условие сработало. Назови вероятную причину и
укажи функцию в bot.py. Код пока не пиши, дай план правки в 2-4 пунктах и
жди моё «пиши».

Когда я скажу «пиши»: поправь минимально, ничего лишнего не трогай, без
комментариев в коде, добавь проверку в test_friends.py, прогони тесты
(в конце должно быть ALL OK), отдай файлы и шаги проверки после деплоя.

[вставь лог или скриншот и что ожидал увидеть]
"""

PROMPT_FEATURE = """
Новая задача для бота. Сначала только план: какие функции в bot.py затронем,
какие таблицы или колонки нужны, как это поведёт себя в темах Main, Commands
и в темах друзей, что может сломаться. Если есть развилка, предложи 2
варианта и порекомендуй один. Кода не пиши, пока я не скажу «пиши».

После «пиши»: минимальная правка, без комментариев в коде, новые проверки в
test_friends.py, прогон тестов, файлы целиком и шаги проверки после деплоя.

[опиши задачу]
"""

PROMPT_REVIEW = """
Сделай ревью приложенного кода бота без правок. Ищи: места, где ответ
уходит мимо say() и мимо нужной темы, гонки при двойных нажатиях кнопок,
состояния, которые не сбрасываются в /cancel, запросы к базе без учёта
изоляции чатов, места, где при падении Gemini или Groq бот молчит.
Ответ: список находок по важности, у каждой функция и одно предложение
сути. Не пиши код и не переписывай файлы.
"""

PROMPT_TESTS = """
Добавь проверки в test_friends.py для описанного ниже поведения. Стиль
файла сохрани: заглушки вместо aiogram, FakeBot, простые assert и строка
«... OK» после каждого блока, в конце «ALL OK». Не меняй bot.py. Если для
теста нужна правка в bot.py, сначала напиши, какая именно, и жди «пиши».

[что проверить]
"""

PROMPT_SYNC_NOTES = """
Вот новая версия bot.py (и других файлов, если менялись). Сравни с
PROJECT_NOTES.py и выдай только то, что нужно добавить или поправить в
PROJECT_NOTES.py: новые разделы карты, новые правила поведения, строки для
CHANGELOG. Код не переписывай.
"""


# =============================================================================
#  6. ОРИГИНАЛЬНЫЕ КОММЕНТАРИИ ИЗ КОДА (по файлам, в порядке следования)
#     Формат: где находился комментарий и к какой строке кода относился.
#     Номера строк не указаны специально: они меняются, ищи по тексту строки.
# =============================================================================

#
# ------------------------------ bot.py ------------------------------
#
#   [описание модуля, было в начале файла]
#     Just Chatting (ENG): a private English speaking-practice bot for Telegram.
#
#     CODE MAP (search for these section titles):
#       settings ........ environment variables and constants
#       styles/prompt ... chat styles and the rules sent to the AI
#       storage ......... Postgres (Supabase) with an in-memory fallback
#       streak .......... streak counting, pinning, morning message
#       ai .............. Gemini (main) and Groq (fallback), answer parsing and filters
#       voice ........... Gemini (main) and Groq Whisper (fallback)
#       handlers ........ commands, settings menu, messages
#       web server ...... webhook, health check, story page
#
#  >> settings (all secrets come from environment variables)
#   верхний уровень, у строки `COOLDOWN_SECONDS = 5`: only for people the admin allowed, not for the admin
#   верхний уровень, у строки `HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "30"))`: messages (user + bot)
#   верхний уровень, у строки `AI_ATTEMPTS = 2`: attempts per AI provider
#   верхний уровень, у строки `STREAK_MESSAGES = 3`: messages needed to count a day
#   верхний уровень, у строки `MORNING_WINDOW_HOURS = 6`: a late morning message is skipped after this long
#   верхний уровень, у строки `GOAL_REPORT_HOUR = 21`: local hour of the final goal report on the last day
#
#  >> chat styles: key -> (button label, instruction for the AI)
#
#  >> storage: Postgres (Supabase) with an in-memory fallback
#   верхний уровень, у строки `pool = None`: asyncpg pool, stays None when there is no database
#   верхний уровень, у строки `allowed: set[int] = set()`: people the admin gave access to
#   верхний уровень, у строки `"hints_on": True, "hint_min": 5, "hint_sent": False, "hint_ts": None, "s`: stage 4
#   def track: Runs a coroutine in the background and keeps a reference so it is not garbage collected.
#
#  >> topics and friends
#   def Typing: Keeps a chat action (typing / record_voice) alive in a chat or topic while the block runs.
#   в _loop(), у строки `await asyncio.sleep(4.5)`: Telegram shows an action for about 5 seconds
#   def say: Sends text to the chat of `message`, into the same topic (or the given one).
#   верхний уровень, у строки `mem_friends: dict[int, dict] = {}`: used when there is no database
#   def topics_on: The Main and Commands topics exist (made by /start).
#   def main_kw: Where the bot writes by itself (streak, morning message, reminders, hints): the Main topic.
#   def route: Which chat a message belongs to: 'friend', 'main', 'cmd' (Commands topic) or 'other' (any stray topic).
#   верхний уровень, у строки `CHAT_COMMANDS = {"new", "stop", "hint"}`: allowed inside Main and friend topics; the rest live in Commands
#   верхний уровень, у строки `ANYWHERE_COMMANDS = {"start", "cancel"}`: work in every topic
#   в topic_alive(), у строки `return "thread" not in str(e).lower()`: only a missing topic counts, not a network hiccup
#   def ensure_topics: Creates the Main and Commands topics (or makes them again if one was deleted).
#   в init_db(), у строки `for table in ("users", "allowed", "friends"):`: keep the Supabase API closed for every table
#
#  >> time helpers
#   def parse_tz: Accepts a name like Europe/Berlin or an offset like +3, UTC+3, -5.
#   в parse_tz(), у строки `name = f"Etc/GMT{'-' if sign == '+' else '+'}{hours}"`: Etc zones have an inverted sign
#   в reset_chat(), у строки `await upd(u, level=None, topic=None, history=[], awaiting=None, active=F`: style and settings stay between conversations
#   def clean_reply: Remove JSON debris that sometimes leaks into the reply text.
#   def tip_in_text: A tip like 'Не хватает a: a new game' is wrong if the user already wrote that phrase.
#   def tip_repeats_old: A tip that has nothing to do with the current message but matches an earlier message of this
#       chat is an old tip coming back.
#   def tip_same_sides: Drop pointless tips like 'to the shop → to the shop'.
#   в is_trivial(), у строки `if ("наречи" in expl or "adverb" in expl) and CASUAL_ADVERB_VERBS.search`: "it's going amazing", "I feel great": an adjective after these verbs is normal speech
#   def salvage: Groq answers 400 when the model's JSON is broken, but includes what it wrote. Try to use it.
#
#  >> ai: Gemini first, Groq as a fallback
#   в call_gemini(), у строки `"generationConfig": {"responseMimeType": "application/json", "maxOutputT`: no temperature: Gemini 3.x models do not accept sampling parameters
#   в ask_ai(), у строки `final_user = f"{user_text}\n\n[Reply in this style (do not mention this `: the reminder keeps the style strong even when earlier replies were written in another style
#   в ask_ai(), у строки `break`: retrying will not help, try the next provider
#   в ask_ai(), у строки `result["mistakes"] = [m for m in result.get("mistakes", [])`: a mistake must quote words from this very message, otherwise the model is repeating an old one
#
#  >> voice: Gemini first, Groq Whisper as a fallback
#
#  >> sending answers
#   в send_ai_result(), у строки `if not voice_sent:`: text goes out unless the voice reply was sent (or voice failed)
#   в send_ai_result(), у строки `per_mistake = 2 if len(mistakes) == 1 else 1`: keep it short when there are several
#   в send_ai_result(), у строки `if friend:`: a friend's silence is tracked separately from the main chat
#
#  >> streak
#   def kb: Rows of (text, callback_data) or (text, callback_data, style); style: success=green, danger=red, primary=blue.
#   def send_story_file: Sends the rendered story to the chat, so it can be posted as a story by hand.
#   в morning_one(), у строки `await upd(u, last_morning=today)`: mark first, so a failure does not repeat every minute
#   в morning_one(), у строки `return`: already counted today
#   верхний уровень, у строки `CATEGORY_NAMES = {"a": "articles", "t": "tenses", "p": "prepositions", "`: stage 4: stats, weak spots, goals, hints, reminders, voice replies
#
#  >> generic "ask the AI for JSON" helper (used by goals, hints, nudges)
#
#  >> statistics
#
#  >> goals
#   в goal_report(), у строки `msgs, cur = [], ""`: split into messages under the Telegram limit, only between lines (tags stay balanced)
#   в track_goals(), у строки `hint = ""`: an old hint is no longer in the learner's head
#   в track_goals(), у строки `fresh = await get_user(uid)`: the goals may have changed while the AI was thinking
#   в track_goals(), у строки `continue`: the same message is never counted twice
#
#  >> stats text and widget
#   def tg_call: Raw Bot API call for methods that aiogram may not know yet. Returns the result or None.
#   def use_topic_tabs: Shows the topics as tabs above the chat instead of a sidebar (Bot API 10.5, setMySettings).
#
#  >> hints and reminders
#   в nudge_one(), у строки `last_main = u.get("last_main_ts") or last_user`: only messages in Main count for Main's hints
#   в nudge_one(), у строки `if u["active"] and u["hints_on"] and waiting:`: 1) hint when he stops answering in an active conversation, then end the conversation
#   в nudge_one(), у строки `if u["remind_on"] and last_user:`: 2) reminders after a longer silence
#   в nudge_one(), у строки `now_local = local_now(u)`: 3) goal reports on the last day of a goal (or later, if the bot was off)
#   def friend_hints: Silence hint inside a friend's topic: he stopped answering, so send ideas into that topic.
#   в mark_user_message(), у строки `if friend:`: talking to a friend counts as activity, but must not touch Main's silence hints
#   в ensure_active(), у строки `if u["silence_end"] or u["remind_sent"] > 0:`: he is back after a pause: just continue
#
#  >> voice replies (text to speech)
#
#  >> keyboards
#   в tog(), у строки `def tog(label: str, value: bool, data: str):`: green when on, red when off
#
#  >> handlers
#   верхний уровень, у строки `stranger_notice: dict[int, float] = {}`: when a person without access was last told "private bot"
#   def AccessMiddleware: Only the admin and the people the admin allowed can use the bot.
#   в __call__(), у строки `return None`: service messages are not from a person
#   в __call__(), у строки `if not told_recently:`: no reply to every message, a stranger could make the bot spam
#   def TopicMiddleware: Keeps the topics apart: commands (except /start, /new, /stop, /hint) only work in Commands.
#   в cmd_start(), у строки `await ensure_topics(message.bot, u)`: makes the topics, keeps the conversation
#   в cmd_new(), у строки `if friend:`: inside a friend's topic: just clear that conversation
#   в cmd_new(), у строки `if u["active"]:`: a running conversation is never wiped without asking
#   def cmd_cancel: Drops whatever the bot is waiting for: typed answers and half-made goals or friends.
#   в cmd_stop(), у строки `if await friend_here(message):`: keep the chats apart: /stop belongs to the main chat only
#
#  >> admin commands
#
#  >> callbacks
#   верхний уровень, у строки `starting: set[int] = set()`: people whose conversation is being started right now
#   в drop_keyboard(), у строки `pass`: the message is old or already changed, no problem
#   def begin_chat: Starts the conversation once: a second tap on an old button must not start another one.
#
#  >> messages
#
#  >> friends: own characters in their own topics
#   в cb_friend_voice(), у строки `if provider == "gemini":`: only Gemini voices can have an accent
#
#  >> stage 4 handlers: hints, stats, goals, reminders menu, voice
#
#  >> reminder timing menu
#
#  >> voice replies
#
#  >> web server (webhook, health check for the pinger, story page)
#   def check_init_data: Checks the signature of the data Telegram gives to a mini app. Returns the user id or None.
#   def story_send: Called by the mini app: sends the story file into the user's chat.

#
# ------------------------------ story.py ------------------------------
#
#   [описание модуля, было в начале файла]
#     Renders the "N days in a row" story picture (PNG) or video (MP4).
#
#     Put your flame file next to this file (or into an assets/ folder):
#       - animated: flame.apng, flame.webp or flame.gif -> MP4 video
#       - static:   flame.png                            -> PNG picture
#       - nothing:  a simple flame is drawn automatically
#     Optional: font.ttf (a bold font) for nicer text.
#
#     The cache is automatically refreshed when the flame or font file changes.
#   def _source_signature: Signature of the flame and font assets used for rendering.
#   def _procedural_flame: A simple flame, used when no flame file is provided.
#   def _load_flame: Returns (frames, durations_ms). One frame means a static picture.
#   def story_path: Returns the cached file for this number if it exists.
#   верхний уровень, у строки `KEEP_FILES = 3`: only the newest story files stay on the disk
#   def _prune: Deletes the older story files so the cache cannot grow without limit.
#   def ensure_story: Renders (or finds in the cache) the story for streak n.
#       Returns (path, 'mp4' | 'png').
#   в ensure_story(), у строки `for ext in ("mp4", "png"):`: Remove stale formats too, so an old MP4 cannot be selected
#   в ensure_story(), у строки `for ext in ("mp4", "png"):`: if rendering now falls back to PNG.

#
# ------------------------------ test_friends.py ------------------------------
#   верхний уровень, у строки `bot.kb = lambda rows: rows`: keyboards become plain lists of (label, data)
#
#  >> fakes
#   в fake_gemini(), у строки `if "STALE" in user_text:`: the model repeats an old mistake that is not in this message
#
#  >> /create dialog
#
#  >> routing: friend topic vs default
#   в main(), у строки `assert len(B.sent) == 2`: reply + mistake block
#
#  >> voice modes in a friend topic
#   в main(), у строки `assert [t for t in texts() if t.startswith("Hey")] == []`: no text duplicate
#   в main(), у строки `assert any("<blockquote>" in t for t in texts())`: mistakes stay
#
#  >> /new inside topic clears only the friend
#
#  >> stickers in a topic
#
#  >> /friends, delete
#
#  >> topics disabled -> retry
#   в main(), у строки `await bot.cb_friend_retry(Call("fc:go"))`: edge voice: accent step is skipped
#
#  >> custom accent
#
#  >> old behaviour still fine: voice-only in default chat, fallback on tts failure
#   в main(), у строки `assert [t for t in texts() if t.startswith("Hey")]`: text fallback
#
#  >> stale mistake from history is dropped, a real one stays
#
#  >> friend silence hints
#   в main(), у строки `await bot.on_text(Msg("I goed out", thread=888))`: Max answers, now he is waiting
#   в main(), у строки `assert mx["last_bot_ts"] and u.get("last_bot_ts") == main_bot_ts`: main chat state untouched
#   в main(), у строки `clear(); await bot.friend_hints(B, u)`: too early
#   в main(), у строки `mx["last_user_ts"] = _t.time() - 500; mx["last_bot_ts"] = _t.time() - 40`: he wrote, Max answered 6+ min ago
#   в main(), у строки `assert hint_calls[-1] == mx["history"][-6:] and hint_calls[-1] != []`: built from Max's history
#   в main(), у строки `clear(); await bot.friend_hints(B, u); assert B.sent == []`: only one hint
#   в main(), у строки `await bot.on_text(Msg("ok I am here", thread=888))`: he answers: state resets
#   в main(), у строки `assert B.sent == []`: switched off in /settings
#
#  >> coloured settings buttons
#
#  >> every chat keeps its own history, nothing leaks between chats
#   в main(), у строки `print("history isolation OK")`: replies stayed where the question was asked
#   в main(), у строки `clear(); await bot.cmd_stop(Msg("/stop", thread=888))`: /stop in a friend's topic does not end the main chat
#
#  >> stale tip dropped, real tip kept
#   в main(), у строки `await bot.upd(u, history=[], tips_on=True); clear()`: tips were switched off by the colour test
#
#  >> Main + Commands topics
#   в main(), у строки `await bot.cmd_start(Msg("/start"))`: typed in the no-topic area
#   в main(), у строки `assert B.sent[0][1].get("message_thread_id") == main and "Commands topic`: onboarding in Main
#   в main(), у строки `assert "message_thread_id" not in B.sent[1][1] and "Main" in B.sent[1][0`: pointer where typed
#   в main(), у строки `assert len(u["history"]) == n_hist`: Commands / stray topics never touch the chat history
#   в main(), у строки `clear(); await bot.on_text(Msg("hello max", thread=888))`: a friend still works while topics are on, with its own history
#
#  >> commands live in Commands
#   в main(), у строки `await bot.upd(u, main_thread=None, cmd_thread=None)`: topics not set up yet: old behaviour
#
#  >> things the bot writes by itself go to Main
#
#  >> deleted topic is made again by /start, a network hiccup is not mistaken for it
#
#  >> topics are off in BotFather: the bot falls back to a single chat
#
#  >> Main's silence hints ignore talking to a friend
#   в main(), у строки `assert u["hint_sent"] is True and u["last_main_ts"] < u["last_user_ts"]`: Main's hint state left alone
#   в main(), у строки `await bot.upd(u, hint_sent=False, last_main_ts=now - 900, last_bot_ts=no`: last_user_ts is fresh: friend
#   в main(), у строки `await bot.upd(u, hint_sent=False, last_main_ts=None, last_user_ts=now - `: old data
#
#  >> /cancel
#   в main(), у строки `await bot.on_text(Msg("Europe/Berlin please", thread=7001))`: not taken as a time zone any more
#
#  >> topics as tabs: setMySettings(has_topic_sidebar=False) at startup
#   в main(), у строки `calls.clear(); await bot.use_topic_tabs(); assert calls == [("getMe", {}`: already tabs: nothing to do
#   в main(), у строки `calls.clear(); await bot.use_topic_tabs(); assert calls[-1][0] == "setMy`: field unknown: try anyway
#   в main(), у строки `await bot.use_topic_tabs()`: refused: logged, no crash
#   в main(), у строки `await bot.use_topic_tabs()`: network error: no crash
#
#  >> /start (welcome, keeps the chat) and /new (fresh start)
#   в main(), у строки `assert "already chatting" in text and "/stop" in text and "/new" in text`: a chat is running
#   в main(), у строки `assert len(u["history"]) == 1 and "Main" in B.sent[1][0]`: /start never wipes the chat
#   в main(), у строки `assert "chatting with" in texts()[0] and u["history"]`: friend topic: nothing is wiped
#   в main(), у строки `clear(); await bot.cmd_new(Msg("/new", thread=7002))`: a chat is running: asks first
#   в main(), у строки `assert "Fresh start" in texts()[0]`: no chat running: no question
#   в main(), у строки `await bot.upd(u, active=False); clear(); await bot.cb_start_menu(Call("s`: start menu buttons
#
#  >> onboarding: old buttons never start a second conversation
#   в main(), у строки `ca, cb2 = Call("top:skip"), Call("top:skip")`: two taps at the same moment
#   в main(), у строки `assert len(prompts) == 1 and u["active"], len(prompts)`: one opener only
#   в main(), у строки `for cbq in (Call("top:skip"), Call("go"), Call(f"lvl:{lvl}")):`: old buttons later
#   в main(), у строки `await bot.cmd_new(Msg("/new")); await bot.cb_new_confirm(Call("nw:yes"))`: buttons usable again
#
#  >> people without access: one notice per 10 minutes, nothing else runs
#   в main(), у строки `bot.allowed.add(99); await mwa(hh, Msg("hi", uid=99), {}); assert hits =`: allowed: passes
#
#  >> story cache keeps only the newest files
#
#  >> init_db: friends table + RLS on all three


# =============================================================================
#  7. CHANGELOG (добавляй сюда строки после каждой правки)
# =============================================================================
#  2026-11-25  Все комментарии и докстринги вынесены из bot.py, story.py и
#              test_friends.py в этот файл. Код не менялся (AST сверен),
#              test_friends.py проходит (ALL OK).
#  2026-11-26  story.py: стикер по центру холста и меньше на 5%, число меньше на
#              20% и ниже стикера с отступом, "days in a row" больше на 10% и
#              ровно под числом; центровка по реальным границам. Рендер
#              переписан потоково (экономия памяти для Render), добавлены
#              форматы WebM и MOV. bot.py: drop_stray() удаляет случайные
#              темы и пишет подсказку в Main, bot_topics защищает темы,
#              созданные ботом. test_friends.py: новые проверки (темы,
#              вёрстка, кадры), ALL OK.
#  2026-11-28  Правка по скриншоту. story.py: размеры как на образце Duolingo
#              (стикер 300, число около 394 px, подпись около 335 px, отступы
#              60 и 40), группа по центру холста, текст масштабируется до
#              нужной ширины независимо от шрифта, LAYOUT_VERSION=4. bot.py:
#              удаление лишних тем теперь по служебному сообщению
#              forum_topic_created (раньше его глотал AccessMiddleware, если оно
#              от аккаунта бота), topic_alive делает 3 попытки перед
#              пересозданием Main/Commands, добавлены записи в лог.
#              test_friends.py: новые проверки, ALL OK.


# =============================================================================
#  8. BUILD
# =============================================================================
BUILD = {
    "project": "Just Chatting (ENG)",
    "bot": "@TheBotToTalk_bot",
    "build": "2026-11-28",
    "files": ["bot.py", "story.py", "test_friends.py", "requirements.txt", "PROJECT_NOTES.py"],
    "requirements": ['aiogram>=3.13,<4', 'httpx>=0.27', 'asyncpg>=0.29', 'Pillow>=10.1', 'imageio-ffmpeg>=0.5', 'tzdata', 'edge-tts>=6.1'],
    "created_by": "Suren41k, Claude",
}

# ГЛАВНОЕ: Любые изменения должны быть внесены в данный раздел с подробным описанием и указанием даты. Данная секция служит журналом истории. Допускается хранение не более десяти последних изменений; превышение этого лимита запрещено.
# by t.me/suren41k, Claude