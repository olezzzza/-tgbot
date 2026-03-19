import os
import asyncio
import httpx
import base64
import tempfile
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
from openai import OpenAI

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
MONITOR_URL = os.getenv("MONITOR_URL", "https://voicecrmapp.com")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# История диалога и контекст проекта для каждого пользователя
conversation_history = {}
current_project = {}

# ─── Описания агентов ───────────────────────────────────────────────

AGENTS = {
    "voicecrm": {
        "name": "VoiceCRM AI агент",
        "system": """Ты агент проекта VoiceCRM AI.
Контекст проекта:
- Продукт: AI обработка голосовых сообщений + CRM интеграция
- Стек: Python, FastAPI, OpenAI Whisper, GPT-4o-mini, HubSpot API, Pipedrive API
- Хостинг: Railway.app
- Домен: voicecrmapp.com
- GitHub: olezzzza/voicecrm
- Цель: продать компанию за 10M EUR за 1 год
- Основатель: Олег Боженко, Дублин, Ирландия
Помогай с кодом, стратегией, партнёрствами и развитием этого продукта.
Общайся на русском языке."""
    },
    "handyman": {
        "name": "Handyman агент",
        "system": """Ты агент проекта Handyman (услуги мастера на все руки).
Контекст проекта:
- Facebook страница: facebook.com/handyman.oleh
- Задача: автоматический постинг в Facebook каждый день
- Генерация текста и картинок через AI
- Основатель: Олег Боженко, Дублин, Ирландия
Помогай с контентом, постингом, маркетингом для этого проекта.
Общайся на русском языке."""
    },
    "general": {
        "name": "Общий агент",
        "system": """Ты Claude — личный AI ассистент Олега Боженко.
Олег — предприниматель из Дублина, Ирландия. Строит стартапы.
Помогай с любыми задачами: программирование, бизнес, анализ, переводы, идеи.
Анализируй скриншоты и картинки когда их присылают.
Общайся на русском языке."""
    }
}

ADMIN_SYSTEM = """Ты главный администратор. Твоя задача — определить о каком проекте идёт речь в сообщении пользователя и ответить ТОЛЬКО одним словом:
- voicecrm — если речь о VoiceCRM AI, голосовой почте, CRM интеграции, Railway, voicecrmapp.com
- handyman — если речь о Handyman, мастере, услугах, Facebook постинге
- general — если это общий вопрос не про конкретный проект

Отвечай ТОЛЬКО одним словом без пояснений."""


# ─── Определение проекта ────────────────────────────────────────────

def detect_project(text: str, user_id: int) -> str:
    """Определяет проект через Claude"""
    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=ADMIN_SYSTEM,
            messages=[{"role": "user", "content": text}]
        )
        project = response.content[0].text.strip().lower()
        if project in AGENTS:
            current_project[user_id] = project
            return project
    except Exception:
        pass
    return current_project.get(user_id, "general")


# ─── Ответ через Claude ─────────────────────────────────────────────

def ask_claude(text: str, user_id: int, image_base64: str = None) -> str:
    project = detect_project(text, user_id)
    agent = AGENTS[project]
    history = get_history(user_id)

    if image_base64:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_base64}},
            {"type": "text", "text": text}
        ]
    else:
        content = text

    history.append({"role": "user", "content": content})

    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=agent["system"],
        messages=history
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})

    # Добавляем подпись какой агент ответил
    project_label = {"voicecrm": "🤖 VoiceCRM", "handyman": "🔨 Handyman", "general": "💬 Общий"}
    return f"{project_label.get(project, '')} агент:\n\n{reply}"


def get_history(user_id: int):
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    # Храним только последние 20 сообщений
    if len(conversation_history[user_id]) > 20:
        conversation_history[user_id] = conversation_history[user_id][-20:]
    return conversation_history[user_id]


# ─── Команды ────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я твой AI ассистент с тремя агентами:\n\n"
        "🤖 VoiceCRM — вопросы по voicecrmapp.com\n"
        "🔨 Handyman — Facebook постинг\n"
        "💬 Общий — всё остальное\n\n"
        "Я сам определяю о каком проекте речь.\n\n"
        "Команды:\n"
        "/clear — очистить историю\n"
        "/project — показать текущий проект\n"
        "/check — статус voicecrmapp.com\n\n"
        "Можешь писать текст, присылать фото или голосовые сообщения 🎤"
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_history[user_id] = []
    current_project.pop(user_id, None)
    await update.message.reply_text("✅ История и контекст проекта очищены.")


async def project_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    project = current_project.get(user_id, "не определён")
    labels = {"voicecrm": "🤖 VoiceCRM AI", "handyman": "🔨 Handyman", "general": "💬 Общий"}
    await update.message.reply_text(f"Текущий проект: {labels.get(project, project)}")


async def check_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Проверяю {MONITOR_URL}...")
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(MONITOR_URL)
            if r.status_code == 200:
                await update.message.reply_text(f"✅ {MONITOR_URL} работает!")
            else:
                await update.message.reply_text(f"⚠️ Статус: {r.status_code}")
    except Exception as e:
        await update.message.reply_text(f"❌ Недоступен: {str(e)[:100]}")


# ─── Обработчики сообщений ──────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    await update.message.chat.send_action("typing")
    try:
        reply = ask_claude(update.message.text, user_id)
        for chunk in [reply[i:i+4096] for i in range(0, len(reply), 4096)]:
            await update.message.reply_text(chunk)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {str(e)[:200]}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    await update.message.chat.send_action("typing")
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_base64 = base64.standard_b64encode(file_bytes).decode("utf-8")
    caption = update.message.caption or "Опиши что на этом изображении и помоги разобраться."

    try:
        reply = ask_claude(caption, user_id, image_base64)
        for chunk in [reply[i:i+4096] for i in range(0, len(reply), 4096)]:
            await update.message.reply_text(chunk)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {str(e)[:200]}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    await update.message.reply_text("🎤 Расшифровываю голосовое...")
    await update.message.chat.send_action("typing")

    try:
        # Скачиваем голосовое
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        file_bytes = await file.download_as_bytearray()

        # Сохраняем во временный файл
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        # Расшифровываем через Whisper
        with open(tmp_path, "rb") as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        os.unlink(tmp_path)

        text = transcript.text
        await update.message.reply_text(f"📝 Расшифровка: {text}")

        # Отправляем в Claude
        reply = ask_claude(text, user_id)
        for chunk in [reply[i:i+4096] for i in range(0, len(reply), 4096)]:
            await update.message.reply_text(chunk)

    except Exception as e:
        await update.message.reply_text(f"Ошибка расшифровки: {str(e)[:200]}")


# ─── Мониторинг домена ──────────────────────────────────────────────

async def monitor_domain(app: Application):
    domain_was_down = True
    while True:
        await asyncio.sleep(300)
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(MONITOR_URL)
                if r.status_code == 200 and domain_was_down:
                    domain_was_down = False
                    if ALLOWED_USER_ID:
                        await app.bot.send_message(
                            chat_id=ALLOWED_USER_ID,
                            text=f"✅ {MONITOR_URL} заработал!"
                        )
                elif r.status_code != 200:
                    domain_was_down = True
        except Exception:
            domain_was_down = True


# ─── Запуск ─────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("project", project_status))
    app.add_handler(CommandHandler("check", check_domain))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    async def post_init(app: Application):
        asyncio.create_task(monitor_domain(app))

    app.post_init = post_init

    print("Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
