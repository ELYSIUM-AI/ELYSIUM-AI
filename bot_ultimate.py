import os
import json
import logging
import io
import uuid
import urllib.parse
import asyncio
import aiohttp
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from typing import Optional, List, Dict

import chromadb
from chromadb.utils import embedding_functions
from groq import AsyncGroq
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from bs4 import BeautifulSoup

# Поиск
try:
    from duckduckgo_search import DDGS
    DDGS_AVAILABLE = True
except ImportError:
    DDGS_AVAILABLE = False

# Документы
try:
    from pypdf import PdfReader
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
try:
    from docx import Document
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False
try:
    from openpyxl import load_workbook
    XLSX_AVAILABLE = True
except ImportError:
    XLSX_AVAILABLE = False

# Голос
try:
    import whisper
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
FAL_KEY = os.getenv("FAL_KEY")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

if not TELEGRAM_TOKEN or not GROQ_KEY:
    raise ValueError("❌ Ошибка: TELEGRAM_TOKEN или GROQ_KEY не найдены в .env файле!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = AsyncGroq(api_key=GROQ_KEY)

# ====================== БАЗА ДАННЫХ ======================
DB_PATH = "elysium_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_facts (
            user_id INTEGER NOT NULL,
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            PRIMARY KEY (user_id, fact_key)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON chat_history(user_id)")
    conn.commit()
    conn.close()

init_db()

# Кэш истории в памяти (последние 20 сообщений)
conversation_cache: Dict[int, List[Dict]] = {}

# ChromaDB для долговременной памяти
chroma_client = chromadb.PersistentClient(path="./elysium_memory")
embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")

def get_user_collection(user_id: int):
    return chroma_client.get_or_create_collection(
        name=f"memory_{user_id}",
        embedding_function=embedding_fn
    )

# ====================== ОБРАБОТКА ГОЛОСА ======================
async def transcribe_voice(file_path: str) -> Optional[str]:
    if not WHISPER_AVAILABLE:
        return None
    try:
        model = whisper.load_model("base")
        result = model.transcribe(file_path, language="ru")
        return result["text"]
    except Exception as e:
        logger.error(f"Whisper error: {e}")
        return None

# ====================== ОБРАБОТКА ДОКУМЕНТОВ ======================
async def extract_text_from_file(file_path: str, ext: str) -> str:
    try:
        if ext == "txt":
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read(3000)
        elif ext == "pdf" and PDF_AVAILABLE:
            reader = PdfReader(file_path)
            text = ""
            for page in reader.pages[:5]:
                text += page.extract_text()
            return text[:3000]
        elif ext == "docx" and DOCX_AVAILABLE:
            doc = Document(file_path)
            text = "\n".join([para.text for para in doc.paragraphs])
            return text[:3000]
        elif ext in ("xlsx", "xls") and XLSX_AVAILABLE:
            wb = load_workbook(file_path, read_only=True)
            sheet = wb.active
            text = ""
            for i, row in enumerate(sheet.iter_rows(values_only=True)):
                if i > 50:
                    break
                text += " ".join(str(cell) for cell in row if cell) + "\n"
            return text[:3000]
        else:
            return "Формат файла не поддерживается."
    except Exception as e:
        logger.error(f"File extraction error: {e}")
        return "Не удалось прочитать файл."

# ====================== ПОИСК (УЛУЧШЕННЫЙ) ======================
async def search_web(query: str) -> str:
    """Возвращает ссылки + сниппеты. Если DDGS нет – HTML‑парсер."""
    if DDGS_AVAILABLE:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))
                if results:
                    answer = "🔍 **Результаты поиска:**\n\n"
                    for r in results:
                        title = r.get('title', '').strip()
                        body = r.get('body', '').strip()
                        link = r.get('href', '')
                        if link:
                            answer += f"• **{title}**\n  {body[:200]}\n  [→ подробнее]({link})\n\n"
                    return answer
        except Exception as e:
            logger.error(f"DDGS error: {e}")
    # Fallback на HTML DuckDuckGo (только заголовки)
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10) as resp:
                if resp.status == 200:
                    soup = BeautifulSoup(await resp.text(), 'html.parser')
                    links = soup.find_all('a', class_='result__a', limit=5)
                    answer = "🔍 **Результаты (альтернативный режим):**\n\n"
                    for link in links:
                        title = link.get_text()[:80]
                        href = link.get('href')
                        if href and href.startswith('//'):
                            href = "https:" + href
                        if href:
                            answer += f"• [{title}]({href})\n"
                    return answer
    except Exception as e:
        logger.error(f"Search fallback error: {e}")
    return "❌ Не удалось выполнить поиск."

# ====================== ПОГОДА ======================
async def get_weather(city: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://wttr.in/{urllib.parse.quote(city)}?format=%C+%t"
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    return f"🌤️ Погода в {city.title()}: {await resp.text()}"
    except:
        pass
    return "Не удалось получить погоду."

# ====================== ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ (С ДВУМЯ ПОПЫТКАМИ) ======================
async def generate_image(prompt: str) -> Optional[bytes]:
    # 1) Пробуем Pollinations.ai
    try:
        encoded = urllib.parse.quote(prompt[:700])
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&enhance=true&nologo=true"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=50) as resp:
                if resp.status == 200 and resp.content_type.startswith('image/'):
                    logger.info("✅ Изображение получено от Pollinations")
                    return await resp.read()
    except Exception as e:
        logger.error(f"Pollinations error: {e}")

    # 2) Пробуем Fal.ai (если есть ключ)
    if FAL_KEY:
        try:
            fal_url = "https://fal.run/fal-ai/flux/schnell"
            headers = {"Authorization": f"Key {FAL_KEY}", "Content-Type": "application/json"}
            payload = {"prompt": prompt, "image_size": "square", "num_inference_steps": 4}
            async with aiohttp.ClientSession() as session:
                async with session.post(fal_url, headers=headers, json=payload, timeout=60) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("images"):
                            img_url = data["images"][0]["url"]
                            async with session.get(img_url, timeout=30) as img_resp:
                                if img_resp.status == 200 and img_resp.content_type.startswith('image/'):
                                    logger.info("✅ Изображение получено от Fal.ai")
                                    return await img_resp.read()
        except Exception as e:
            logger.error(f"Fal.ai error: {e}")

    return None

# ====================== FALLBACK OLLAMA ======================
async def ask_ollama(prompt: str, system: str = "") -> str:
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "llama3.1",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ],
                "stream": False
            }
            async with session.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=60) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("message", {}).get("content", "Ошибка Ollama")
    except Exception as e:
        logger.error(f"Ollama error: {e}")
    return "❌ ИИ временно недоступен. Попробуйте позже."

# ====================== ОСНОВНОЙ AI (GROQ + FALLBACK) ======================
async def ask_ai_with_fallback(messages: List[Dict]) -> str:
    try:
        completion = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            max_tokens=1100,
            temperature=0.73
        )
        return completion.choices[0].message.content
    except Exception as e:
        logger.error(f"Groq error: {e}, switching to Ollama")
        user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        return await ask_ollama(user_msg, system)

# ====================== SQLite ИСТОРИЯ ======================
async def add_history(user_id: int, role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chat_history (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (user_id, role, content, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    if user_id not in conversation_cache:
        conversation_cache[user_id] = []
    conversation_cache[user_id].append({"role": role, "content": content})
    if len(conversation_cache[user_id]) > 20:
        conversation_cache[user_id] = conversation_cache[user_id][-20:]

async def get_history(user_id: int, limit=20) -> List[Dict]:
    if user_id in conversation_cache:
        return conversation_cache[user_id][-limit:]
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT role, content FROM chat_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cur.fetchall()
    conn.close()
    history = [{"role": row[0], "content": row[1]} for row in reversed(rows)]
    conversation_cache[user_id] = history
    return history

# ====================== ПАМЯТЬ (CHROMA) ======================
async def save_memory(user_id: int, text: str, role: str):
    try:
        collection = get_user_collection(user_id)
        collection.add(
            documents=[text],
            metadatas=[{"role": role, "timestamp": datetime.now().isoformat()}],
            ids=[f"{uuid.uuid4()}"]
        )
    except Exception as e:
        logger.error(f"Memory save error: {e}")

async def get_relevant_memory(user_id: int, query: str, n=3) -> List[str]:
    try:
        collection = get_user_collection(user_id)
        results = collection.query(query_texts=[query], n_results=n)
        return results['documents'][0] if results['documents'] else []
    except Exception as e:
        logger.error(f"Memory query error: {e}")
        return []

# ====================== ПРОФИЛЬ И ФАКТЫ ======================
USER_PROFILES_DIR = "user_profiles"
os.makedirs(USER_PROFILES_DIR, exist_ok=True)

async def load_profile(user_id: int) -> Dict:
    path = os.path.join(USER_PROFILES_DIR, f"profile_{user_id}.json")
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            profile = json.load(f)
    else:
        profile = {"name": None, "city": None, "interests": []}
    # Дополним фактами из БД (на всякий случай)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT fact_key, fact_value FROM user_facts WHERE user_id = ?", (user_id,))
    rows = cur.fetchall()
    conn.close()
    for key, val in rows:
        if key not in profile:
            profile[key] = val
    return profile

async def save_profile(user_id: int, profile: Dict):
    path = os.path.join(USER_PROFILES_DIR, f"profile_{user_id}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

async def save_fact(user_id: int, key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO user_facts (user_id, fact_key, fact_value) VALUES (?, ?, ?)",
        (user_id, key, value)
    )
    conn.commit()
    conn.close()

async def extract_facts(user_id: int, user_message: str, ai_response: str):
    import re
    profile = await load_profile(user_id)
    changed = False
    # Имя
    name_match = re.search(r"меня зовут (\w+)", user_message, re.IGNORECASE)
    if not name_match:
        name_match = re.search(r"я (\w+)", user_message, re.IGNORECASE)
    if name_match:
        name = name_match.group(1).capitalize()
        if profile.get("name") != name:
            profile["name"] = name
            await save_fact(user_id, "name", name)
            changed = True
    # Город
    city_match = re.search(r"я живу в (\w+)", user_message, re.IGNORECASE)
    if city_match:
        city = city_match.group(1).capitalize()
        if profile.get("city") != city:
            profile["city"] = city
            await save_fact(user_id, "city", city)
            changed = True
    # Интересы
    for kw in ["люблю", "нравится", "интересуюсь", "увлекаюсь", "хобби"]:
        if kw in user_message.lower():
            parts = user_message.lower().split(kw)
            if len(parts) > 1:
                interest = parts[1].strip()[:40]
                if interest and interest not in profile.get("interests", []):
                    profile.setdefault("interests", []).append(interest)
                    await save_fact(user_id, f"interest_{len(profile['interests'])}", interest)
                    changed = True
    if changed:
        await save_profile(user_id, profile)

# ====================== БЕЗОПАСНЫЙ MARKDOWN ======================
def safe_markdown(text: str) -> str:
    special_chars = r'_*[]()~`>#+-=|{}.!'
    for ch in special_chars:
        text = text.replace(ch, f'\\{ch}')
    return text

# ====================== ОБРАБОТЧИК СООБЩЕНИЙ ======================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Друг"
    text = update.message.text.strip() if update.message.text else ""

    # --- ГОЛОС ---
    if update.message.voice:
        voice_file = await update.message.voice.get_file()
        file_path = f"voice_{user_id}_{uuid.uuid4()}.ogg"
        await voice_file.download_to_drive(file_path)
        transcribed = await transcribe_voice(file_path)
        os.remove(file_path)
        if transcribed:
            text = transcribed
        else:
            await update.message.reply_text("Не удалось распознать голос.")
            return

    # --- ФАЙЛЫ ---
    if update.message.document:
        doc = update.message.document
        ext = doc.file_name.split('.')[-1].lower()
        if ext in ("txt", "pdf", "docx", "xlsx", "xls"):
            file = await doc.get_file()
            file_path = f"temp_{user_id}_{uuid.uuid4()}.{ext}"
            await file.download_to_drive(file_path)
            content = await extract_text_from_file(file_path, ext)
            os.remove(file_path)
            text = f"Содержимое файла:\n{content}\n\nВопрос: {text}" if text else content
        else:
            await update.message.reply_text("Формат не поддерживается. Отправьте TXT, PDF, DOCX или Excel.")
            return

    if not text:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    text_lower = text.lower()

    # --- ГЕНЕРАЦИЯ ФОТО ---
    if any(k in text_lower for k in ['нарисуй', 'сгенерируй', 'картинку', 'фото', 'изображение']):
        msg = await update.message.reply_text("🎨 Генерирую изображение... ⏳")
        prompt = text
        for w in ['нарисуй', 'сгенерируй', 'картинку', 'фото', 'изображение']:
            prompt = prompt.replace(w, '').strip()
        img_data = await generate_image(prompt or "красивое изображение")
        await msg.delete()
        if img_data:
            try:
                await update.message.reply_photo(photo=io.BytesIO(img_data), caption=f"✨ {prompt[:100]}")
            except Exception:
                await update.message.reply_text(f"✅ [Изображение готово](https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)})", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Не удалось сгенерировать изображение. Попробуйте другой запрос.")
        return

    # --- ПОГОДА ---
    if 'погода' in text_lower:
        city = text_lower.split('погода')[-1].strip() or "Москва"
        weather = await get_weather(city)
        await update.message.reply_text(weather)
        return

    # --- ПОИСК ---
    if any(k in text_lower for k in ['найди', 'поищи', 'что такое', 'где', 'узнай']):
        search_result = await search_web(text)
        # Отправляем результаты поиска
        await update.message.reply_text(search_result, parse_mode='Markdown', disable_web_page_preview=True)
        # Затем дополняем AI-ответом (краткое резюме)
        messages = [
            {"role": "system", "content": "Ты — Elysium, полезный помощник. Пользователь задал поисковый запрос. Результаты поиска уже показаны, но если хочешь, можешь дать краткий комментарий или резюме."},
            {"role": "user", "content": text}
        ]
        answer = await ask_ai_with_fallback(messages)
        await update.message.reply_text(answer, parse_mode='Markdown', disable_web_page_preview=True)
        await add_history(user_id, "user", text)
        await add_history(user_id, "assistant", answer)
        await save_memory(user_id, text, "user")
        await save_memory(user_id, answer, "assistant")
        await extract_facts(user_id, text, answer)
        return

    # --- ОБЫЧНЫЙ ЧАТ С ПАМЯТЬЮ ---
    profile = await load_profile(user_id)
    if profile.get("name") is None and user_name != "Друг":
        profile["name"] = user_name
        await save_profile(user_id, profile)

    history = await get_history(user_id, limit=15)
    memory = await get_relevant_memory(user_id, text, n=3)

    system = f"""Ты — Elysium, тёплый, эмпатичный помощник.
Имя пользователя: {profile.get('name') or user_name}
Город: {profile.get('city', 'не указан')}
Интересы: {', '.join(profile.get('interests', []))}
Твоя задача — поддерживать диалог, помогать, иногда шутить."""
    
    messages = [
        {"role": "system", "content": system},
        *history,
        *[{"role": "system", "content": f"Воспоминание: {m}"} for m in memory],
        {"role": "user", "content": text}
    ]

    answer = await ask_ai_with_fallback(messages)

    await add_history(user_id, "user", text)
    await add_history(user_id, "assistant", answer)
    await save_memory(user_id, text, "user")
    await save_memory(user_id, answer, "assistant")
    await extract_facts(user_id, text, answer)

    try:
        await update.message.reply_text(answer, parse_mode='Markdown', disable_web_page_preview=True)
    except:
        await update.message.reply_text(safe_markdown(answer))

# ====================== СТАРТ ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌟 *Elysium v5 (финальная)* запущен\n\n"
        "Я умею:\n"
        "• отвечать на вопросы (Groq + Ollama fallback)\n"
        "• генерировать изображения (`нарисуй кота`)\n"
        "• искать информацию с описанием (`найди биткоин`)\n"
        "• показывать погоду (`погода Москва`)\n"
        "• читать файлы (PDF, DOCX, XLSX, TXT)\n"
        "• распознавать голос (отправьте голосовое)\n"
        "• запоминать факты о вас (имя, город, интересы)\n"
        "• хранить всю историю в SQLite и векторы в ChromaDB\n\n"
        "Просто пиши!",
        parse_mode='Markdown'
    )

def main():
    print("🚀 Elysium Ultimate v5 (исправленный) запущен...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE | filters.Document.ALL, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()