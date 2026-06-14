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
from supabase import create_client

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

# --- Supabase ---
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")
supabase = create_client(supabase_url, supabase_key)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
FAL_KEY = os.getenv("FAL_KEY")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

if not TELEGRAM_TOKEN or not GROQ_KEY:
    raise ValueError("❌ Ошибка: TELEGRAM_TOKEN или GROQ_KEY не найдены в .env файле!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = AsyncGroq(api_key=GROQ_KEY)

# ====================== НОВЫЕ ИНСТРУМЕНТЫ ======================
async def get_crypto_price(coin: str) -> str:
    """Получает текущую цену криптовалюты с CoinGecko"""
    try:
        coin_id = coin.lower().strip()
        # сопоставление названий
        mapping = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "ton": "the-open-network"}
        coin_id = mapping.get(coin_id, coin_id)
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = data.get(coin_id, {}).get("usd")
                    if price:
                        return f"💰 {coin.upper()} сейчас стоит **${price}**"
    except Exception as e:
        logger.error(f"Crypto error: {e}")
    return f"❌ Не удалось получить цену {coin.upper()}"

async def get_stock_price(symbol: str) -> str:
    """Получает цену акции через Yahoo Finance (бесплатно)"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = data['chart']['result'][0]['meta']['regularMarketPrice']
                    return f"📈 {symbol.upper()} сейчас стоит **${price}**"
    except:
        pass
    return f"❌ Не удалось получить цену {symbol.upper()}"

async def get_news(topic: str) -> str:
    """Поиск новостей через DuckDuckGo + парсинг"""
    return await search_web(topic)  # переиспользуем существующий поиск

# ====================== ОСТАЛЬНОЙ КОД (без изменений) ======================
# ... (весь ваш существующий код от `init_db` до `generate_image` остаётся без правок)
# В целях экономии места я пропущу повторение, но в финальном файле он должен быть.
# Ниже идёт только изменённая часть — обработчик сообщений с новыми инструментами и личностью.

# ====================== ОСНОВНОЙ AI (обновлённый промпт) ======================
SYSTEM_PROMPT_TEMPLATE = """Ты — Elysium. Твой стиль: спокойный, прямой, уважительный. Без мата и лишней воды.
Ты объясняешь сложные вещи простым языком. Прямой, честный, дружелюбный, с лёгким юмором.
Говоришь как умный друг, который не боится сказать правду.

Твои ценности: правда, полезность, юмор. Перед каждым ответом делай быстрый self-check:
1) Это правда? (нет галлюцинаций)
2) Это полезно?
3) Это уважительно?

Если не уверен — честно скажи "я не знаю" или "информация может быть устаревшей".

Ты имеешь доступ к инструментам:
- поиск в интернете (если пользователь просит «найди», «поищи», «что такое»)
- цена криптовалют (если спрашивают цену биткоина, эфира и т.д.)
- цена акций (если просят цену акции, например «акция Apple»)
- новости (по запросу «новости»)
- погода (по запросу «погода»)
- генерация изображений (по словам «нарисуй», «создай фото»)

Пользователь: {user_name}
Город: {city}
Интересы: {interests}

Отвечай на языке пользователя. Будь максимально полезен.
"""

async def ask_ai_with_fallback_and_tools(messages: List[Dict], user_id: int, user_name: str, profile: Dict) -> str:
    """Обёртка, которая может вызвать инструменты перед генерацией ответа"""
    # Инструменты не вызываются здесь напрямую — они уже обработаны в handle_message
    # Но для self-check можно переслать сообщение в Groq с улучшенным промптом.
    full_messages = messages.copy()
    # Добавляем системный промпт с личностью
    system = SYSTEM_PROMPT_TEMPLATE.format(
        user_name=profile.get('name') or user_name,
        city=profile.get('city', 'не указан'),
        interests=', '.join(profile.get('interests', []))
    )
    full_messages.insert(0, {"role": "system", "content": system})
    # Вызываем стандартную функцию (она уже умеет падать на Ollama)
    return await ask_ai_with_fallback(full_messages)

# Переопределим ask_ai_with_fallback (старая версия переименована)
# В вашем коде уже есть async def ask_ai_with_fallback(messages: List[Dict]) -> str
# Переименуем её в _ask_groq_or_ollama, а новую сделаем основной.
# Но чтобы не ломать старые вызовы, просто заменим её содержимое.

# Я перепишу функцию ask_ai_with_fallback так, чтобы она использовала личностный промпт,
# но для обратной совместимости оставлю старый код. Ниже приведена полная замена.

# ====================== ПЕРЕОПРЕДЕЛЯЕМ ask_ai_with_fallback ======================
# (Старую функцию можно переименовать, но проще заменить её тело)
# Сначала сохраним старую под другим именем, чтобы не потерять.
_original_ask_ai_with_fallback = ask_ai_with_fallback

async def ask_ai_with_fallback(messages: List[Dict], profile: Optional[Dict] = None, user_name: str = "Друг") -> str:
    """Улучшенная версия с личностным промптом, если передан profile"""
    if profile is not None:
        system = SYSTEM_PROMPT_TEMPLATE.format(
            user_name=profile.get('name') or user_name,
            city=profile.get('city', 'не указан'),
            interests=', '.join(profile.get('interests', []))
        )
        new_messages = [{"role": "system", "content": system}] + messages
    else:
        new_messages = messages
    try:
        completion = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=new_messages,
            max_tokens=1100,
            temperature=0.73
        )
        return completion.choices[0].message.content
    except Exception as e:
        logger.error(f"Groq error: {e}, switching to Ollama")
        user_msg = next((m["content"] for m in reversed(new_messages) if m["role"] == "user"), "")
        system_msg = next((m["content"] for m in new_messages if m["role"] == "system"), "")
        return await ask_ollama(user_msg, system_msg)

# ====================== ОБРАБОТЧИК СООБЩЕНИЙ (обновлённый) ======================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Друг"
    text = update.message.text.strip() if update.message.text else ""

    # --- ГОЛОС --- (без изменений)
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

    # --- ФАЙЛЫ --- (без изменений)
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

    # --- ГЕНЕРАЦИЯ ФОТО --- (без изменений)
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

    # --- ПОГОДА --- (без изменений)
    if 'погода' in text_lower:
        city = text_lower.split('погода')[-1].strip() or "Москва"
        weather = await get_weather(city)
        await update.message.reply_text(weather)
        return

    # --- НОВЫЕ ИНСТРУМЕНТЫ ---
    # Цена криптовалюты
    if any(k in text_lower for k in ['цена биткоина', 'цена eth', 'цена sol', 'цена btc', 'сколько стоит', 'курс']):
        # извлечь название монеты
        words = text_lower.split()
        coin = None
        for w in words:
            if w in ('btc', 'bitcoin', 'eth', 'ethereum', 'sol', 'solana', 'ton', 'matic', 'dogecoin'):
                coin = w
                break
        if not coin and 'биткоин' in text_lower:
            coin = 'btc'
        if coin:
            price = await get_crypto_price(coin)
            await update.message.reply_text(price, parse_mode='Markdown')
            return
        # если не распознали, ничего не делаем, пойдёт в обычный чат

    # Цена акции
    if any(k in text_lower for k in ['акция', 'акции', 'stock', 'цена акции']):
        # извлечь символ (простое правило)
        words = text_lower.split()
        for w in words:
            if w.upper() in ('AAPL', 'GOOGL', 'MSFT', 'TSLA', 'AMZN', 'NVDA'):
                symbol = w.upper()
                price = await get_stock_price(symbol)
                await update.message.reply_text(price, parse_mode='Markdown')
                return

    # --- ПОИСК (уже есть, но улучшим распознавание) ---
    if any(k in text_lower for k in ['найди', 'поищи', 'что такое', 'где', 'узнай', 'новости']):
        search_result = await search_web(text)
        await update.message.reply_text(search_result, parse_mode='Markdown', disable_web_page_preview=True)
        messages = [
            {"role": "system", "content": "Ты — Elysium, полезный помощник. Пользователь задал поисковый запрос. Результаты поиска уже показаны, но если хочешь, можешь дать краткий комментарий или резюме."},
            {"role": "user", "content": text}
        ]
        profile = await load_profile(user_id)
        answer = await ask_ai_with_fallback(messages, profile, user_name)
        await update.message.reply_text(answer, parse_mode='Markdown', disable_web_page_preview=True)
        await save_to_history(user_id, "user", text)
        await save_to_history(user_id, "assistant", answer)
        await save_memory(user_id, text, "user")
        await save_memory(user_id, answer, "assistant")
        await extract_facts(user_id, text, answer)
        return

    # --- ОБЫЧНЫЙ ЧАТ С ПАМЯТЬЮ И ЛИЧНОСТЬЮ ---
    profile = await load_profile(user_id)
    if profile.get("name") is None and user_name != "Друг":
        profile["name"] = user_name
        await save_profile(user_id, profile)

    history = await get_history_supabase(user_id, limit=15)
    memory = await get_relevant_memory(user_id, text, n=3)

    messages = [
        *history,
        *[{"role": "system", "content": f"Воспоминание: {m}"} for m in memory],
        {"role": "user", "content": text}
    ]

    answer = await ask_ai_with_fallback(messages, profile, user_name)

    await save_to_history(user_id, "user", text)
    await save_to_history(user_id, "assistant", answer)
    await save_memory(user_id, text, "user")
    await save_memory(user_id, answer, "assistant")
    await extract_facts(user_id, text, answer)

    try:
        await update.message.reply_text(answer, parse_mode='Markdown', disable_web_page_preview=True)
    except Exception:
        await update.message.reply_text(safe_markdown(answer))

# ====================== СТАРТ (обновлён) ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌟 *Elysium v6* — ИИ с характером и реальными инструментами\n\n"
        "✅ **Что я умею:**\n"
        "• Отвечаю на вопросы прямо, честно, с юмором\n"
        "• Ищу в интернете (`найди новости`)\n"
        "• Показываю цены криптовалют (`цена биткоина`)\n"
        "• Цены акций (`акция Apple`)\n"
        "• Генерирую изображения (`нарисуй кота`)\n"
        "• Погода (`погода Москва`)\n"
        "• Читаю файлы, распознаю голос\n"
        "• Всё, что вы говорите, сохраняю в базу и помню\n\n"
        "Просто пиши — я помогу без воды и подхалимства.",
        parse_mode='Markdown'
    )

def main():
    print("🚀 Elysium v6 (Supabase + личность + крипто-инструменты) запущен...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE | filters.Document.ALL, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
