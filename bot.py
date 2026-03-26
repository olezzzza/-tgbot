import os
import json
import asyncio
import httpx
import base64
import tempfile
import urllib.parse
import urllib.request
import re
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
from openai import OpenAI
from duckduckgo_search import DDGS

load_dotenv()

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
ALLOWED_USER_ID   = int(os.getenv("ALLOWED_USER_ID", "0"))
MONITOR_URL       = os.getenv("MONITOR_URL", "https://voicecrmapp.com")

TASKS_FILE     = "tasks.json"
MEMORY_FILE    = "memory.json"
REMINDERS_FILE = "reminders.json"
EXPENSES_FILE  = "expenses.json"
LONGMEM_FILE   = "longmem.json"
TENDERS_FILE   = "tenders_seen.json"
DUBLIN_TZ      = ZoneInfo("Europe/Dublin")

TENDER_KEYWORDS = [
    "construction", "building", "refurbishment", "renovation", "maintenance",
    "repair", "restoration", "fit-out", "fit out", "fitout",
    "civil works", "minor works", "small works", "general contractor",
    "carpentry", "joinery", "painting", "decorating", "plastering",
    "plumbing", "electrical", "roofing", "flooring", "tiling",
    "insulation", "landscaping", "groundworks", "drainage",
    "facilities", "facility management", "handyman", "contractor",
]

claude         = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client  = OpenAI(api_key=OPENAI_API_KEY)

conversation_history = {}
current_project      = {}
voice_mode           = {}   # user_id -> True/False


# ═══════════════════════════════════════════════════════════════════
# 1. ПАМЯТЬ МЕЖДУ ПЕРЕЗАПУСКАМИ
# ═══════════════════════════════════════════════════════════════════

def load_memory():
    """Загружает историю диалогов из файла при старте."""
    global conversation_history, current_project, voice_mode
    if os.path.exists(MEMORY_FILE):
        try:
            data = json.load(open(MEMORY_FILE, encoding="utf-8"))
            # Ключи в JSON — строки, конвертируем в int
            conversation_history = {int(k): v for k, v in data.get("history", {}).items()}
            current_project      = {int(k): v for k, v in data.get("project", {}).items()}
            voice_mode           = {int(k): v for k, v in data.get("voice",   {}).items()}
        except Exception:
            pass


def save_memory():
    """Сохраняет историю диалогов в файл."""
    data = {
        "history": {str(k): v for k, v in conversation_history.items()},
        "project": {str(k): v for k, v in current_project.items()},
        "voice":   {str(k): v for k, v in voice_mode.items()},
    }
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_history(user_id: int) -> list:
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    hist = conversation_history[user_id]
    if len(hist) > 20:
        conversation_history[user_id] = hist[-20:]
    return conversation_history[user_id]


# ═══════════════════════════════════════════════════════════════════
# 2. ЗАДАЧИ
# ═══════════════════════════════════════════════════════════════════

def load_tasks() -> list:
    if os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_tasks_to_file(tasks: list):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


def add_task(description: str, project: str) -> int:
    tasks = load_tasks()
    task_id = max((t["id"] for t in tasks), default=0) + 1
    tasks.append({
        "id": task_id,
        "description": description,
        "project": project,
        "done": False,
        "created": datetime.now(DUBLIN_TZ).strftime("%d.%m.%Y %H:%M")
    })
    save_tasks_to_file(tasks)
    return task_id


def complete_task(task_id: int) -> bool:
    tasks = load_tasks()
    for task in tasks:
        if task["id"] == task_id:
            task["done"] = True
            save_tasks_to_file(tasks)
            return True
    return False


def format_tasks(show_done=False) -> str:
    tasks = load_tasks()
    if not tasks:
        return "Список задач пуст."
    icons = {"voicecrm": "🤖", "handyman": "🔨", "general": "💬"}
    lines = []
    for t in tasks:
        if t["done"] and not show_done:
            continue
        status = "✅" if t["done"] else "⬜"
        icon   = icons.get(t["project"], "📌")
        lines.append(f"{status} [{t['id']}] {icon} {t['description']}  ({t['created']})")
    return "\n".join(lines) if lines else "Активных задач нет."


# ═══════════════════════════════════════════════════════════════════
# 3. ТРЕКЕР РАСХОДОВ
# ═══════════════════════════════════════════════════════════════════

def load_expenses() -> list:
    if os.path.exists(EXPENSES_FILE):
        with open(EXPENSES_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_expenses_to_file(expenses: list):
    with open(EXPENSES_FILE, "w", encoding="utf-8") as f:
        json.dump(expenses, f, ensure_ascii=False, indent=2)


def add_expense(amount: float, category: str, description: str) -> int:
    expenses = load_expenses()
    exp_id = max((e["id"] for e in expenses), default=0) + 1
    expenses.append({
        "id": exp_id,
        "amount": amount,
        "category": category,
        "description": description,
        "date": datetime.now(DUBLIN_TZ).strftime("%d.%m.%Y")
    })
    save_expenses_to_file(expenses)
    return exp_id


def format_expenses(month: str = None) -> str:
    expenses = load_expenses()
    if not expenses:
        return "Расходов нет."
    if month is None:
        month = datetime.now(DUBLIN_TZ).strftime("%m.%Y")
    filtered = [e for e in expenses if e["date"].endswith(month)]
    if not filtered:
        return f"Расходов за {month} нет."
    total = sum(e["amount"] for e in filtered)
    lines = [f"💰 Расходы за {month}:"]
    for e in filtered:
        lines.append(f"  [{e['id']}] {e['date']} — €{e['amount']:.2f} | {e['category']} | {e['description']}")
    lines.append(f"\n💳 Итого: €{total:.2f}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 4. ДОЛГОСРОЧНАЯ ПАМЯТЬ
# ═══════════════════════════════════════════════════════════════════

def load_longmem() -> list:
    if os.path.exists(LONGMEM_FILE):
        with open(LONGMEM_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_longmem(facts: list):
    with open(LONGMEM_FILE, "w", encoding="utf-8") as f:
        json.dump(facts, f, ensure_ascii=False, indent=2)


def add_fact(fact: str) -> int:
    facts = load_longmem()
    fact_id = max((f["id"] for f in facts), default=0) + 1
    facts.append({
        "id": fact_id,
        "fact": fact,
        "date": datetime.now(DUBLIN_TZ).strftime("%d.%m.%Y")
    })
    save_longmem(facts)
    return fact_id


def format_longmem() -> str:
    facts = load_longmem()
    if not facts:
        return "Долгосрочная память пуста."
    return "\n".join(f"• [{f['id']}] {f['fact']}  ({f['date']})" for f in facts)


# ═══════════════════════════════════════════════════════════════════
# 5. МОНИТОРИНГ ТЕНДЕРОВ (etenders.gov.ie)
# ═══════════════════════════════════════════════════════════════════

def load_seen_tenders() -> set:
    if os.path.exists(TENDERS_FILE):
        with open(TENDERS_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_tenders(seen: set):
    with open(TENDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f)


def is_relevant_tender(title: str, description: str = "") -> bool:
    text = (title + " " + description).lower()
    return any(kw in text for kw in TENDER_KEYWORDS)


def fetch_tenders_rss() -> list:
    """Получает тендеры через RSS publicprocurement.ie."""
    items = []
    try:
        urls = [
            "https://publicprocurement.ie/etenders-feed/",
            "https://www.etenders.gov.ie/epps/cft/listContractDocuments.do?currentType=cft",
        ]
        for url in urls:
            try:
                content = fetch_url(url)
                # Парсим RSS вручную (без lxml)
                titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", content)
                links  = re.findall(r"<link>(https?://[^<]+)</link>|<link href=[\"'](https?://[^\"']+)[\"']", content)
                descs  = re.findall(r"<description><!\[CDATA\[(.*?)\]\]></description>|<description>(.*?)</description>", content)

                # Берём только item-записи (пропускаем первый заголовок канала)
                for i, (t1, t2) in enumerate(titles[1:], 0):
                    title = (t1 or t2).strip()
                    link  = links[i][0] or links[i][1] if i < len(links) else ""
                    desc  = (descs[i][0] or descs[i][1]).strip() if i < len(descs) else ""
                    if title and title not in ("", "eTenders"):
                        items.append({"title": title, "link": link, "desc": desc})
                if items:
                    break
            except Exception:
                continue
    except Exception as e:
        print(f"Ошибка RSS тендеров: {e}")

    # Если RSS не дал результатов — ищем через DuckDuckGo
    if not items:
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(
                    "site:etenders.gov.ie software AI digital IT services 2025",
                    max_results=10
                ):
                    items.append({
                        "title": r["title"],
                        "link":  r["href"],
                        "desc":  r["body"]
                    })
        except Exception as e:
            print(f"Ошибка поиска тендеров: {e}")

    return items


def check_new_tenders() -> list:
    """Возвращает список новых релевантных тендеров (ещё не виденных)."""
    seen    = load_seen_tenders()
    all_t   = fetch_tenders_rss()
    new_t   = []

    for t in all_t:
        uid = t["link"] or t["title"]
        if uid in seen:
            continue
        if is_relevant_tender(t["title"], t["desc"]):
            new_t.append(t)
            seen.add(uid)

    if new_t:
        save_seen_tenders(seen)
    return new_t


def format_tender(t: dict) -> str:
    text = f"📋 *{t['title']}*"
    if t.get("desc"):
        text += f"\n{t['desc'][:200]}..."
    if t.get("link"):
        text += f"\n🔗 {t['link']}"
    return text


# ═══════════════════════════════════════════════════════════════════
# 6. НАПОМИНАНИЯ
# ═══════════════════════════════════════════════════════════════════

def load_reminders() -> list:
    if os.path.exists(REMINDERS_FILE):
        with open(REMINDERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_reminders(reminders: list):
    with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(reminders, f, ensure_ascii=False, indent=2)


def add_reminder(user_id: int, text: str, remind_at: datetime) -> int:
    reminders = load_reminders()
    rem_id = max((r["id"] for r in reminders), default=0) + 1
    reminders.append({
        "id": rem_id,
        "user_id": user_id,
        "text": text,
        "at": remind_at.isoformat(),
        "sent": False
    })
    save_reminders(reminders)
    return rem_id


# ═══════════════════════════════════════════════════════════════════
# 4. ИНТЕРНЕТ: ПОИСК И ЧТЕНИЕ СТРАНИЦ
# ═══════════════════════════════════════════════════════════════════

def web_search(query: str) -> str:
    try:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=5):
                results.append(f"• {r['title']}\n  {r['body']}\n  Источник: {r['href']}")
        return "\n\n".join(results) if results else "Результаты не найдены."
    except Exception as e:
        return f"Ошибка поиска: {e}"


def fetch_url(url: str) -> str:
    """Загружает страницу и возвращает текст без HTML тегов."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
        content = re.sub(r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL)
        content = re.sub(r"<script[^>]*>.*?</script>", "", content, flags=re.DOTALL)
        content = re.sub(r"<[^>]+>", " ", content)
        content = re.sub(r"\s+", " ", content).strip()
        return content[:3000]
    except Exception as e:
        return f"Не удалось загрузить страницу: {e}"


# ═══════════════════════════════════════════════════════════════════
# 5. ГЕНЕРАЦИЯ КАРТИНОК (DALL-E 3)
# ═══════════════════════════════════════════════════════════════════

def generate_image(prompt: str) -> bytes | None:
    """Генерирует картинку через DALL-E 3, возвращает байты PNG."""
    try:
        response = openai_client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1,
            response_format="b64_json"
        )
        b64 = response.data[0].b64_json
        return base64.b64decode(b64)
    except Exception as e:
        print(f"Ошибка генерации картинки: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# ОПИСАНИЯ АГЕНТОВ
# ═══════════════════════════════════════════════════════════════════

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
ВАЖНО: если в сообщении есть блок [РЕЗУЛЬТАТЫ ПОИСКА], это реальные данные из интернета — используй их.
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
ВАЖНО: если в сообщении есть блок [РЕЗУЛЬТАТЫ ПОИСКА], это реальные данные из интернета — используй их.
Общайся на русском языке."""
    },
    "general": {
        "name": "Общий агент",
        "system": """Ты Claude — личный AI ассистент Олега Боженко.
Олег — предприниматель из Дублина, Ирландия. Строит стартапы.
Помогай с любыми задачами: программирование, бизнес, анализ, переводы, идеи.
Анализируй скриншоты и картинки когда их присылают.
ВАЖНО: если в сообщении есть блок [РЕЗУЛЬТАТЫ ПОИСКА], это реальные актуальные данные из интернета — используй их для ответа, ссылайся на источники.
Общайся на русском языке."""
    }
}


# ═══════════════════════════════════════════════════════════════════
# ОРКЕСТРАТОР
# ═══════════════════════════════════════════════════════════════════

ORCHESTRATOR_TOOLS = [
    {
        "name": "detect_project",
        "description": "Определить к какому проекту относится сообщение. Вызывай ВСЕГДА первым.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "enum": ["voicecrm", "handyman", "general"]}
            },
            "required": ["project"]
        }
    },
    {
        "name": "web_search",
        "description": "Поиск актуальной информации: новости, цены, документация, события. Используй когда нужны свежие данные.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "fetch_url",
        "description": (
            "Открыть любую страницу в интернете. "
            "Для погоды используй: https://wttr.in/НазваниеГорода?format=4 "
            "Для курсов валют, любых сайтов — прямой URL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Полный URL страницы"}
            },
            "required": ["url"]
        }
    },
    {
        "name": "generate_image",
        "description": "Сгенерировать картинку через DALL-E 3. Используй когда пользователь просит нарисовать, сделать картинку, изображение.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Описание картинки на английском языке для лучшего результата"}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "save_task",
        "description": "Сохранить задачу. Используй когда пользователь говорит 'нужно сделать', 'запомни', 'добавь задачу'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "project": {"type": "string", "enum": ["voicecrm", "handyman", "general"]}
            },
            "required": ["description", "project"]
        }
    },
    {
        "name": "get_tasks",
        "description": "Получить список задач. Используй когда пользователь спрашивает о задачах.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "save_expense",
        "description": "Сохранить расход. Используй когда пользователь говорит 'потратил', 'заплатил', 'купил', 'расход'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount":      {"type": "number", "description": "Сумма в евро"},
                "category":    {"type": "string", "description": "Категория: hosting, marketing, tools, food, transport, other"},
                "description": {"type": "string", "description": "Описание расхода"}
            },
            "required": ["amount", "category", "description"]
        }
    },
    {
        "name": "save_fact",
        "description": "Сохранить важный факт в долгосрочную память. Используй когда пользователь сообщает важное о себе, бизнесе, клиентах, планах, решениях.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "Факт для запоминания"}
            },
            "required": ["fact"]
        }
    }
]

ORCHESTRATOR_SYSTEM = """Ты главный оркестратор Олега Боженко (предприниматель, Дублин, Ирландия).

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:
1. ВСЕГДА вызывай detect_project первым.
2. Нужны актуальные данные? → используй web_search или fetch_url:
   - Погода → fetch_url: https://wttr.in/НазваниеГорода?format=4
   - Новости, цены, курсы → web_search
   - Конкретный сайт → fetch_url
3. Пользователь просит картинку/изображение → generate_image (промпт на английском)
4. Пользователь ставит задачу → save_task
5. Спрашивает о задачах → get_tasks
6. Пользователь говорит о расходах ('потратил', 'заплатил', 'купил') → save_expense
7. Пользователь сообщает важную информацию о себе, бизнесе, клиентах, планах → save_fact

Не отвечай текстом. Только вызывай инструменты."""


def orchestrate(text: str, user_id: int) -> dict:
    """
    Оркестратор: определяет проект, при необходимости ищет в интернете,
    генерирует картинки, сохраняет задачи.
    """
    result = {
        "project":        current_project.get(user_id, "general"),
        "search_results": None,
        "image_bytes":    None,
        "task_saved":     False,
        "task_id":        None,
        "tasks_list":     None,
        "expense_saved":  False,
        "expense_id":     None,
        "fact_saved":     False,
    }

    messages = [{"role": "user", "content": text}]

    for _ in range(6):
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=ORCHESTRATOR_SYSTEM,
            tools=ORCHESTRATOR_TOOLS,
            messages=messages
        )

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            name  = block.name
            inp   = block.input

            if name == "detect_project":
                project = inp.get("project", "general")
                result["project"] = project
                current_project[user_id] = project
                tool_result = f"Проект: {project}"

            elif name == "web_search":
                data = web_search(inp.get("query", ""))
                result["search_results"] = (result["search_results"] or "") + data
                tool_result = data

            elif name == "fetch_url":
                data = fetch_url(inp.get("url", ""))
                result["search_results"] = (result["search_results"] or "") + data
                tool_result = data

            elif name == "generate_image":
                image_bytes = generate_image(inp.get("prompt", ""))
                result["image_bytes"] = image_bytes
                tool_result = "Картинка сгенерирована." if image_bytes else "Ошибка генерации."

            elif name == "save_task":
                task_id = add_task(inp.get("description", ""), inp.get("project", "general"))
                result["task_saved"] = True
                result["task_id"] = task_id
                tool_result = f"Задача #{task_id} сохранена."

            elif name == "get_tasks":
                tasks_text = format_tasks()
                result["tasks_list"] = tasks_text
                tool_result = tasks_text

            elif name == "save_expense":
                exp_id = add_expense(inp.get("amount", 0), inp.get("category", "other"), inp.get("description", ""))
                result["expense_saved"] = True
                result["expense_id"] = exp_id
                tool_result = f"Расход #{exp_id} сохранён."

            elif name == "save_fact":
                fact_id = add_fact(inp.get("fact", ""))
                result["fact_saved"] = True
                tool_result = f"Факт #{fact_id} сохранён в памяти."

            else:
                tool_result = "Неизвестный инструмент."

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": tool_result
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user",      "content": tool_results})

    return result


# ═══════════════════════════════════════════════════════════════════
# ОТВЕТ ЧЕРЕЗ CLAUDE
# ═══════════════════════════════════════════════════════════════════

def ask_claude(text: str, user_id: int, image_base64: str = None, orch: dict = None) -> str:
    if orch is None:
        orch = orchestrate(text, user_id)

    # Если просили список задач — возвращаем сразу
    if orch["tasks_list"]:
        return f"📋 Список задач:\n\n{orch['tasks_list']}"

    project = orch["project"]
    agent   = AGENTS[project]
    history = get_history(user_id)

    # Долгосрочная память в системный промпт
    facts = format_longmem()
    if facts != "Долгосрочная память пуста.":
        system = agent["system"] + f"\n\nДОЛГОСРОЧНАЯ ПАМЯТЬ (важные факты об Олеге и его бизнесе):\n{facts}"
    else:
        system = agent["system"]

    # Формируем текст с результатами поиска
    user_text = text
    if orch["search_results"]:
        user_text = (
            f"{text}\n\n"
            f"[РЕЗУЛЬТАТЫ ПОИСКА — используй эти данные для ответа:]\n"
            f"{orch['search_results']}\n"
            f"[КОНЕЦ РЕЗУЛЬТАТОВ]"
        )

    if image_base64:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_base64}},
            {"type": "text",  "text": user_text}
        ]
    else:
        content = user_text

    history.append({"role": "user", "content": content})

    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=system,
        messages=history
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    save_memory()

    # Добавляем пометки
    labels          = {"voicecrm": "🤖 VoiceCRM", "handyman": "🔨 Handyman", "general": "💬 Общий"}
    search_notice   = "\n\n🌐 _Использован поиск в интернете._" if orch["search_results"] else ""
    task_notice     = f"\n\n📌 _Задача #{orch['task_id']} добавлена._" if orch["task_saved"] else ""
    expense_notice  = f"\n\n💰 _Расход #{orch['expense_id']} записан._" if orch["expense_saved"] else ""
    fact_notice     = "\n\n🧠 _Запомнил._" if orch["fact_saved"] else ""
    return f"{labels.get(project, '')} агент:\n\n{reply}{search_notice}{task_notice}{expense_notice}{fact_notice}"


# ═══════════════════════════════════════════════════════════════════
# ГОЛОСОВОЙ ОТВЕТ (TTS)
# ═══════════════════════════════════════════════════════════════════

async def send_voice_reply(update: Update, text: str):
    """Озвучивает текст через OpenAI TTS и отправляет голосовым сообщением."""
    try:
        # Убираем markdown разметку перед озвучкой
        clean = re.sub(r"[*_`#\[\]()]", "", text)
        clean = re.sub(r"🤖.*?агент:\n\n", "", clean)
        clean = clean[:3000]  # TTS лимит

        response = openai_client.audio.speech.create(
            model="tts-1",
            voice="nova",
            input=clean
        )
        audio_io = BytesIO(response.content)
        await update.message.reply_voice(voice=audio_io)
    except Exception as e:
        print(f"Ошибка TTS: {e}")


# ═══════════════════════════════════════════════════════════════════
# КОМАНДЫ
# ═══════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я твой AI ассистент.\n\n"
        "🤖 VoiceCRM — вопросы по voicecrmapp.com\n"
        "🔨 Handyman — Facebook постинг\n"
        "💬 Общий — всё остальное\n\n"
        "Что умею:\n"
        "🌐 Искать в интернете и читать сайты\n"
        "🎨 Генерировать картинки (попроси нарисовать)\n"
        "📋 Вести список задач\n"
        "⏰ Напоминания\n"
        "🎤 Голосовые ответы\n\n"
        "Команды:\n"
        "/tasks — список задач\n"
        "/done 1 — задача #1 выполнена\n"
        "/expense 50 hosting Оплата — записать расход\n"
        "/expenses — расходы за месяц\n"
        "/facts — долгосрочная память\n"
        "/facebook [тема] — пост для Handyman\n"
        "/remind 09:30 Текст — напоминание\n"
        "/reminders — список напоминаний\n"
        "/briefing — утренний брифинг сейчас\n"
        "/voice — вкл/выкл голосовые ответы\n"
        "/clear — очистить историю\n"
        "/project — текущий проект\n"
        "/tenders — проверить тендеры сейчас\n"
        "/check — статус voicecrmapp.com"
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_history[user_id] = []
    current_project.pop(user_id, None)
    save_memory()
    await update.message.reply_text("✅ История очищена.")


async def project_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    project = current_project.get(user_id, "не определён")
    labels  = {"voicecrm": "🤖 VoiceCRM AI", "handyman": "🔨 Handyman", "general": "💬 Общий"}
    await update.message.reply_text(f"Текущий проект: {labels.get(project, project)}")


async def tasks_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📋 Задачи:\n\n{format_tasks()}")


async def done_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Укажи номер: /done 1")
        return
    if complete_task(int(args[0])):
        await update.message.reply_text(f"✅ Задача #{args[0]} выполнена!")
    else:
        await update.message.reply_text(f"❌ Задача #{args[0]} не найдена.")


async def voice_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    voice_mode[user_id] = not voice_mode.get(user_id, False)
    save_memory()
    status = "включены 🔊" if voice_mode[user_id] else "выключены 🔇"
    await update.message.reply_text(f"Голосовые ответы {status}")


async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Использование: /remind 09:30 Текст напоминания
    """
    user_id = update.effective_user.id
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Формат: /remind 09:30 Позвонить клиенту")
        return

    time_str = args[0]
    text     = " ".join(args[1:])

    try:
        now    = datetime.now(DUBLIN_TZ)
        hour, minute = map(int, time_str.split(":"))
        remind_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Если время уже прошло — ставим на завтра
        if remind_at <= now:
            from datetime import timedelta
            remind_at = remind_at + timedelta(days=1)

        rem_id = add_reminder(user_id, text, remind_at)
        at_str = remind_at.strftime("%d.%m в %H:%M")
        await update.message.reply_text(f"⏰ Напоминание #{rem_id} установлено на {at_str}:\n{text}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}\nФормат: /remind 09:30 Текст")


async def reminders_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reminders = [r for r in load_reminders() if not r["sent"]]
    if not reminders:
        await update.message.reply_text("Активных напоминаний нет.")
        return
    lines = []
    for r in reminders:
        at = datetime.fromisoformat(r["at"]).strftime("%d.%m %H:%M")
        lines.append(f"⏰ [{r['id']}] {at} — {r['text']}")
    await update.message.reply_text("\n".join(lines))


async def expense_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/expense 50 hosting Оплата Railway"""
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Формат: /expense 50 hosting Оплата Railway")
        return
    try:
        amount      = float(args[0])
        category    = args[1]
        description = " ".join(args[2:])
        exp_id      = add_expense(amount, category, description)
        await update.message.reply_text(f"💰 Расход #{exp_id} записан: €{amount:.2f} — {description}")
    except ValueError:
        await update.message.reply_text("Сумма должна быть числом. Пример: /expense 50 hosting Оплата Railway")


async def expenses_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/expenses — расходы за текущий месяц"""
    await update.message.reply_text(format_expenses())


async def facts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/facts — долгосрочная память"""
    await update.message.reply_text(f"🧠 Долгосрочная память:\n\n{format_longmem()}")


async def tenders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tenders — проверить тендеры прямо сейчас"""
    await update.message.reply_text("🔍 Проверяю etenders.gov.ie...")
    try:
        new_tenders = check_new_tenders()
        if new_tenders:
            await update.message.reply_text(f"🏛 Найдено новых тендеров: {len(new_tenders)}")
            for t in new_tenders:
                await update.message.reply_text(format_tender(t), parse_mode="Markdown")
        else:
            await update.message.reply_text(
                "🏛 Новых подходящих тендеров нет.\n\n"
                f"Слежу за ключевыми словами:\n{', '.join(TENDER_KEYWORDS)}"
            )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def facebook_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/facebook [тема] — сгенерировать пост для Handyman Facebook"""
    topic = " ".join(context.args) if context.args else "handyman services in Dublin"
    await update.message.reply_text("✍️ Генерирую пост для Facebook...")
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": (
                f"Write a short Facebook post for a handyman service in Dublin, Ireland. "
                f"Topic: {topic}. 3-4 sentences + call to action + 3-5 hashtags. English only."
            )}]
        )
        post_text   = resp.content[0].text
        image_bytes = generate_image(f"Professional handyman at work in Dublin, Ireland, {topic}, high quality photo")

        await update.message.reply_text(f"📱 Текст поста:\n\n{post_text}")
        if image_bytes:
            await update.message.reply_photo(photo=BytesIO(image_bytes), caption="🖼 Картинка для поста")
        await update.message.reply_text("💡 Опубликуй на facebook.com/handyman.oleh")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def briefing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет брифинг прямо сейчас по команде /briefing."""
    await update.message.reply_text("⏳ Собираю брифинг...")
    try:
        text = await send_briefing(None)
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


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


# ═══════════════════════════════════════════════════════════════════
# ОБРАБОТЧИКИ СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    await update.message.chat.send_action("typing")
    try:
        orch  = orchestrate(update.message.text, user_id)
        reply = ask_claude(update.message.text, user_id, orch=orch)

        # Картинка если была сгенерирована
        if orch["image_bytes"]:
            await update.message.reply_photo(photo=BytesIO(orch["image_bytes"]))

        # Текстовый ответ
        for chunk in [reply[i:i+4096] for i in range(0, len(reply), 4096)]:
            await update.message.reply_text(chunk)

        # Голосовой ответ если включён
        if voice_mode.get(user_id) and not orch["tasks_list"]:
            await send_voice_reply(update, reply)

    except Exception as e:
        await update.message.reply_text(f"Ошибка: {str(e)[:200]}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    await update.message.chat.send_action("typing")
    photo      = update.message.photo[-1]
    file       = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_b64  = base64.standard_b64encode(file_bytes).decode("utf-8")
    caption    = update.message.caption or "Опиши что на этом изображении и помоги разобраться."

    try:
        orch  = orchestrate(caption, user_id)
        reply = ask_claude(caption, user_id, image_base64=image_b64, orch=orch)
        for chunk in [reply[i:i+4096] for i in range(0, len(reply), 4096)]:
            await update.message.reply_text(chunk)
        if voice_mode.get(user_id):
            await send_voice_reply(update, reply)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {str(e)[:200]}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    await update.message.reply_text("🎤 Расшифровываю...")
    await update.message.chat.send_action("typing")

    try:
        voice      = update.message.voice
        file       = await context.bot.get_file(voice.file_id)
        file_bytes = await file.download_as_bytearray()

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        with open(tmp_path, "rb") as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1", file=audio_file
            )
        os.unlink(tmp_path)

        text = transcript.text
        await update.message.reply_text(f"📝 {text}")

        orch  = orchestrate(text, user_id)
        reply = ask_claude(text, user_id, orch=orch)
        if orch["image_bytes"]:
            await update.message.reply_photo(photo=BytesIO(orch["image_bytes"]))
        for chunk in [reply[i:i+4096] for i in range(0, len(reply), 4096)]:
            await update.message.reply_text(chunk)
        if voice_mode.get(user_id):
            await send_voice_reply(update, reply)

    except Exception as e:
        await update.message.reply_text(f"Ошибка: {str(e)[:200]}")


# ═══════════════════════════════════════════════════════════════════
# ФОНОВЫЕ ЗАДАЧИ
# ═══════════════════════════════════════════════════════════════════

async def send_briefing(bot) -> str:
    """Формирует и отправляет утренний брифинг. Возвращает текст брифинга."""
    weather = fetch_url("https://wttr.in/Dublin?format=4")
    tasks   = format_tasks()
    text = (
        f"☀️ Доброе утро, Олег!\n\n"
        f"🌤 Погода в Дублине:\n{weather}\n\n"
        f"📋 Активные задачи:\n{tasks}"
    )
    if bot and ALLOWED_USER_ID:
        await bot.send_message(chat_id=ALLOWED_USER_ID, text=text)
    return text


async def morning_briefing(app: Application):
    """Каждое утро в 8:00–8:04 по Дублину отправляет брифинг."""
    briefing_sent_date = None
    while True:
        await asyncio.sleep(60)
        now = datetime.now(DUBLIN_TZ)
        if now.hour == 8 and now.minute < 5 and now.date() != briefing_sent_date:
            briefing_sent_date = now.date()
            if not ALLOWED_USER_ID:
                continue
            try:
                await send_briefing(app.bot)
            except Exception as e:
                print(f"Ошибка брифинга: {e}")


async def check_reminders_task(app: Application):
    """Каждую минуту проверяет напоминания."""
    while True:
        await asyncio.sleep(60)
        now       = datetime.now(DUBLIN_TZ)
        reminders = load_reminders()
        changed   = False
        for r in reminders:
            if r["sent"]:
                continue
            remind_at = datetime.fromisoformat(r["at"])
            if now >= remind_at:
                try:
                    await app.bot.send_message(
                        chat_id=r["user_id"],
                        text=f"⏰ Напоминание:\n\n{r['text']}"
                    )
                    r["sent"] = True
                    changed   = True
                except Exception as e:
                    print(f"Ошибка напоминания: {e}")
        if changed:
            save_reminders(reminders)


async def tenders_monitor(app: Application):
    """Каждый день в 9:30 проверяет новые тендеры на etenders.gov.ie."""
    checked_date = None
    while True:
        await asyncio.sleep(60)
        now = datetime.now(DUBLIN_TZ)
        if now.hour == 9 and now.minute == 30 and now.date() != checked_date:
            checked_date = now.date()
            if not ALLOWED_USER_ID:
                continue
            try:
                new_tenders = check_new_tenders()
                if new_tenders:
                    header = f"🏛 Новые тендеры на etenders.gov.ie ({len(new_tenders)}):\n\n"
                    await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=header)
                    for t in new_tenders:
                        await app.bot.send_message(
                            chat_id=ALLOWED_USER_ID,
                            text=format_tender(t),
                            parse_mode="Markdown"
                        )
                else:
                    await app.bot.send_message(
                        chat_id=ALLOWED_USER_ID,
                        text="🏛 Новых подходящих тендеров сегодня нет."
                    )
            except Exception as e:
                print(f"Ошибка мониторинга тендеров: {e}")


async def evening_report(app: Application):
    """Каждый вечер в 21:00 отправляет итоги дня."""
    report_sent_date = None
    while True:
        await asyncio.sleep(60)
        now = datetime.now(DUBLIN_TZ)
        if now.hour == 21 and now.minute < 5 and now.date() != report_sent_date:
            report_sent_date = now.date()
            if not ALLOWED_USER_ID:
                continue
            try:
                from datetime import timedelta
                tasks    = load_tasks()
                expenses = load_expenses()
                pending  = [t for t in tasks if not t["done"]]
                today    = now.strftime("%d.%m.%Y")
                exp_today = [e for e in expenses if e["date"] == today]

                text = f"🌙 Итоги дня {today}:\n\n"
                text += f"⬜ Активных задач: {len(pending)}\n"
                if exp_today:
                    total = sum(e["amount"] for e in exp_today)
                    text += f"💰 Расходов за день: €{total:.2f}\n"
                if pending:
                    text += "\n📋 На завтра:\n"
                    for t in pending[:5]:
                        text += f"  ⬜ {t['description']}\n"

                await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=text)
            except Exception as e:
                print(f"Ошибка вечернего отчёта: {e}")


async def weekly_analysis(app: Application):
    """Каждое воскресенье в 20:00 отправляет AI-анализ недели."""
    analysis_sent_week = None
    while True:
        await asyncio.sleep(60)
        now  = datetime.now(DUBLIN_TZ)
        week = now.isocalendar()[1]
        if now.weekday() == 6 and now.hour == 20 and now.minute < 5 and week != analysis_sent_week:
            analysis_sent_week = week
            if not ALLOWED_USER_ID:
                continue
            try:
                from datetime import timedelta
                tasks    = load_tasks()
                expenses = load_expenses()
                pending  = [t for t in tasks if not t["done"]]
                week_exp = []
                for e in expenses:
                    try:
                        d = datetime.strptime(e["date"], "%d.%m.%Y").replace(tzinfo=DUBLIN_TZ)
                        if (now - d).days <= 7:
                            week_exp.append(e)
                    except Exception:
                        pass

                prompt = (
                    f"Проанализируй неделю Олега Боженко (стартап VoiceCRM AI, Дублин).\n\n"
                    f"Активных задач: {len(pending)}\n"
                    f"Расходов за неделю: €{sum(e['amount'] for e in week_exp):.2f}\n"
                    f"Задачи: {[t['description'] for t in pending[:10]]}\n\n"
                    f"Дай краткий анализ (3-4 предложения) и 3 конкретных совета на следующую неделю для развития стартапа."
                )
                resp    = claude.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=800,
                    messages=[{"role": "user", "content": prompt}]
                )
                text = f"📈 Анализ недели {now.strftime('%d.%m.%Y')}:\n\n{resp.content[0].text}"
                await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=text)
            except Exception as e:
                print(f"Ошибка еженедельного анализа: {e}")


async def monitor_domain(app: Application):
    """Каждые 5 минут проверяет доступность voicecrmapp.com."""
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


# ═══════════════════════════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════════════════════════

def main():
    load_memory()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("clear",      clear))
    app.add_handler(CommandHandler("project",    project_status))
    app.add_handler(CommandHandler("check",      check_domain))
    app.add_handler(CommandHandler("tasks",      tasks_list))
    app.add_handler(CommandHandler("done",       done_task))
    app.add_handler(CommandHandler("voice",      voice_toggle))
    app.add_handler(CommandHandler("briefing",   briefing_cmd))
    app.add_handler(CommandHandler("remind",     remind_cmd))
    app.add_handler(CommandHandler("reminders",  reminders_list))
    app.add_handler(CommandHandler("expense",    expense_cmd))
    app.add_handler(CommandHandler("expenses",   expenses_cmd))
    app.add_handler(CommandHandler("facts",      facts_cmd))
    app.add_handler(CommandHandler("facebook",   facebook_cmd))
    app.add_handler(CommandHandler("tenders",    tenders_cmd))
    app.add_handler(MessageHandler(filters.TEXT  & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO,  handle_photo))
    app.add_handler(MessageHandler(filters.VOICE,  handle_voice))

    async def post_init(app: Application):
        asyncio.create_task(morning_briefing(app))
        asyncio.create_task(check_reminders_task(app))
        asyncio.create_task(evening_report(app))
        asyncio.create_task(weekly_analysis(app))
        asyncio.create_task(tenders_monitor(app))

    app.post_init = post_init

    print("Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
