import os
import re
import logging
from logging.handlers import RotatingFileHandler
import signal
import sys
import asyncio
import html
import json
import smtplib
import secrets
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import Counter, defaultdict
from typing import Optional, Dict, List
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, BotCommand, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, Chat
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
    PicklePersistence,
    TypeHandler
)
from telegram.request import HTTPXRequest
from telegram.error import Forbidden, BadRequest, TimedOut, NetworkError, ChatMigrated

from database import Database
from constants import (
    TIME_SLOTS,
    generate_date_options,
    UKRAINE_TZ,
    CREATE_EVENT_DATE,
    CREATE_EVENT_TIME,
    CREATE_EVENT_PROCEDURE,
    CREATE_EVENT_PHOTO_NEEDED,
    CREATE_EVENT_COMMENT,
    CREATE_EVENT_CONFIRM,
    CREATE_EVENT_REVIEW,
    APPLY_SELECT_EVENTS,
    APPLY_FULL_NAME,
    APPLY_PHONE,
    APPLY_PHOTOS,
    APPLY_CONFIRM,
    BLOCK_USER_ID,
    ADD_PROCEDURE_TYPE_NAME,
    EDIT_PROCEDURE_TYPE_NAME,
    CLEAR_DB_PASSWORD,
    MAX_FULL_NAME_LENGTH,
    MAX_COMMENT_LENGTH,
MAX_PROCEDURE_TYPE_NAME_LENGTH,
MAX_ACTIVE_APPLICATIONS_PER_USER
)

# Завантаження змінних середовища
load_dotenv()

KYIV_TZ = ZoneInfo("Europe/Kyiv")
# Глобально перевизначаємо конвертер часу для форматерів, щоб усі логери й сторонні бібліотеки
# використовували київський час, навіть якщо додають власні хендлери.
logging.Formatter.converter = lambda *args: datetime.now(KYIV_TZ).timetuple()


class KyivFormatter(logging.Formatter):
    """Форматер для логів у часовій зоні Europe/Kyiv"""
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, KYIV_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


# Налаштування логування
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
# Задати форматери: для консолі без часу (journald вже ставить префікс), для файлів — з часом
console_formatter = logging.Formatter('%(name)s - %(levelname)s - %(message)s')
default_formatter = KyivFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
for handler in logging.getLogger().handlers:
    if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
        handler.setFormatter(console_formatter)
    else:
        handler.setFormatter(default_formatter)
# Прибираємо шумні httpx-запити (getUpdates 200 OK), лишаємо попередження/помилки
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

LOG_FILE = os.getenv('BOT_LOG_FILE', 'bot-actions.log')
if LOG_FILE:
    try:
        # Ротація логів: максимум 10 МБ на файл, зберігаємо 5 резервних копій
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=10*1024*1024,  # 10 МБ
            backupCount=5,           # 5 резервних копій (bot-actions.log.1, .2, .3, .4, .5)
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(KyivFormatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s'))
        logging.getLogger().addHandler(file_handler)
        logger.info(f"Логування активовано. Файл: {LOG_FILE} (ротація: 10 МБ, 5 бекапів)")
    except Exception as err:
        logger.error(f"Не вдалося налаштувати файл логування {LOG_FILE}: {err}")

# Ініціалізація бази даних
db = Database()

# Отримання конфігурації з .env
# Підтримка кількох адмінів: ADMIN_IDS=123456789,987654321 або ADMIN_ID=123456789
ADMIN_IDS_STR = os.getenv('ADMIN_IDS', os.getenv('ADMIN_ID', ''))
if ADMIN_IDS_STR:
    ADMIN_IDS = [int(id.strip()) for id in ADMIN_IDS_STR.split(',') if id.strip().isdigit()]
else:
    ADMIN_IDS = []

# Для зворотної сумісності
ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else 0

if not ADMIN_IDS:
    logger.error("КРИТИЧНА ПОМИЛКА: ADMIN_IDS або ADMIN_ID не налаштовано в .env файлі!")
    logger.error("Додайте ADMIN_IDS=123456789,987654321 або ADMIN_ID=123456789 в .env файл")
CHANNEL_ID = os.getenv('CHANNEL_ID', '')  # Застаріло - тепер використовується EVENTS_GROUP_ID
EVENTS_GROUP_ID = os.getenv('EVENTS_GROUP_ID', '')  # Група для публікації подій
if EVENTS_GROUP_ID and EVENTS_GROUP_ID.lstrip('-').isdigit():
    EVENTS_GROUP_ID = int(EVENTS_GROUP_ID)
elif not EVENTS_GROUP_ID and CHANNEL_ID:
    # Fallback на CHANNEL_ID для зворотної сумісності
    EVENTS_GROUP_ID = int(CHANNEL_ID) if CHANNEL_ID.lstrip('-').isdigit() else CHANNEL_ID

# Перевірка наявності EVENTS_GROUP_ID
if not EVENTS_GROUP_ID:
    logger.error("КРИТИЧНА ПОМИЛКА: EVENTS_GROUP_ID не налаштовано в .env файлі!")
    logger.error("Додайте EVENTS_GROUP_ID=-your_group_id в .env файл")
    logger.error("Публікація подій не працюватиме без цього параметру")
GROUP_ID = os.getenv('GROUP_ID', '')
if GROUP_ID and GROUP_ID.lstrip('-').isdigit():
    GROUP_ID = int(GROUP_ID)
CHANNEL_LINK = os.getenv('CHANNEL_LINK', '')
APPLICATIONS_CHANNEL_ID = os.getenv('APPLICATIONS_CHANNEL_ID')
if not APPLICATIONS_CHANNEL_ID:
    APPLICATIONS_CHANNEL_ID = GROUP_ID

# Email конфігурація для повідомлень про заявки
EMAIL_ENABLED = os.getenv('EMAIL_ENABLED', 'false').lower() == 'true'
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
EMAIL_USER = os.getenv('EMAIL_USER', '')
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD', '')
EMAIL_TO = os.getenv('EMAIL_TO', '')  # Email(и) для отримання повідомлень, через кому

if EMAIL_ENABLED and not all([EMAIL_USER, EMAIL_PASSWORD, EMAIL_TO]):
    logger.warning("EMAIL_ENABLED=true, але не всі EMAIL змінні налаштовані. Email-повідомлення будуть вимкнені.")
    EMAIL_ENABLED = False

# Пароль для очистки БД (змінюється через .env для безпеки)
DB_CLEAR_PASSWORD = os.getenv('DB_CLEAR_PASSWORD', '')
if not DB_CLEAR_PASSWORD:
    logger.warning("DB_CLEAR_PASSWORD не налаштовано в .env. Очистка БД буде недоступна.")

ADMIN_MESSAGE_TTL = 15
MAX_APPLICATION_PHOTOS = 3
VERSION = '1.5.4'  # Privacy-fallback, безпечне пересилання, оновлення заявок із caption

# Rate Limiting налаштування
RATE_LIMIT_REQUESTS = 10  # максимум запитів
RATE_LIMIT_PERIOD = 60    # за період в секундах (1 хвилина)
RATE_LIMIT_BAN_DURATION = 300  # бан на 5 хвилин при перевищенні


class RateLimiter:
    """Клас для контролю частоти запитів користувачів"""
    def __init__(self, max_requests: int = RATE_LIMIT_REQUESTS, period: int = RATE_LIMIT_PERIOD):
        self.max_requests = max_requests
        self.period = period
        self.user_requests = defaultdict(list)
        self.banned_users = {}  # user_id -> timestamp коли закінчується бан

    def is_rate_limited(self, user_id: int) -> tuple[bool, Optional[int]]:
        """
        Перевірити чи користувач перевищив ліміт
        Повертає (True, seconds_until_unban) якщо заблоковано, інакше (False, None)
        """
        current_time = time.time()

        # Перевірити чи користувач забанений
        if user_id in self.banned_users:
            ban_end = self.banned_users[user_id]
            if current_time < ban_end:
                return True, int(ban_end - current_time)
            else:
                # Бан закінчився
                del self.banned_users[user_id]
                self.user_requests[user_id].clear()

        # Очистити старі запити
        self.user_requests[user_id] = [
            req_time for req_time in self.user_requests[user_id]
            if current_time - req_time < self.period
        ]

        # Перевірити кількість запитів
        if len(self.user_requests[user_id]) >= self.max_requests:
            # Забанити користувача
            self.banned_users[user_id] = current_time + RATE_LIMIT_BAN_DURATION
            logger.warning(f"User {user_id} rate limited - {len(self.user_requests[user_id])} requests in {self.period}s")
            return True, RATE_LIMIT_BAN_DURATION

        # Додати поточний запит
        self.user_requests[user_id].append(current_time)
        return False, None

    def reset_user(self, user_id: int):
        """Скинути ліміти для користувача (для адмінів)"""
        if user_id in self.user_requests:
            del self.user_requests[user_id]
        if user_id in self.banned_users:
            del self.banned_users[user_id]


# Глобальний rate limiter
rate_limiter = RateLimiter()

APPLICATION_STATUS_LABELS = {
    'pending': "⏳ Очікує",
    'approved': "✅ Схвалено",
    'primary': "✅ Схвалено",
    'rejected': "❌ Відхилено",
    'cancelled': "🚫 Скасовано"
}
STATUS_DISPLAY_ORDER = ['primary', 'approved', 'pending', 'cancelled', 'rejected']
APPLICATION_STATUS_EMOJI = {
    status: label.split()[0]
    for status, label in APPLICATION_STATUS_LABELS.items()
}


def is_admin(user_id: int) -> bool:
    """Перевірка чи користувач є адміністратором"""
    return user_id in ADMIN_IDS


def safe_html(text: str) -> str:
    """Безпечно екранувати HTML для Telegram"""
    if not isinstance(text, str):
        text = str(text)
    return html.escape(text)


def is_private_chat(update: Update) -> bool:
    """Перевіряє, чи є чат приватним"""
    return update.effective_chat and update.effective_chat.type == Chat.PRIVATE


async def require_private_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Перевіряє приватність чату та відповідає помилкою якщо ні.
    Повертає True якщо private, False якщо ні.
    """
    if not is_private_chat(update):
        if update.callback_query:
            await update.callback_query.answer(
                "Ця функція доступна лише в особистих повідомленнях з ботом",
                show_alert=True
            )
        return False
    return True


async def send_message_to_all_admins(context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    """Відправити повідомлення всім адміністраторам"""
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                **kwargs
            )
        except Exception as err:
            logger.error(f"Не вдалося надіслати повідомлення адміністратору {admin_id}: {err}")


async def send_email_notification(subject: str, body: str):
    """
    Відправити email-повідомлення адміністраторам

    Args:
        subject: Тема листа
        body: Текст листа (підтримує HTML)
    """
    if not EMAIL_ENABLED:
        return

    try:
        # Створюємо повідомлення
        msg = MIMEMultipart('alternative')
        msg['From'] = EMAIL_USER
        msg['To'] = EMAIL_TO
        msg['Subject'] = subject

        # Додаємо текстову та HTML версію
        text_part = MIMEText(body, 'plain', 'utf-8')
        html_part = MIMEText(body.replace('\n', '<br>'), 'html', 'utf-8')
        msg.attach(text_part)
        msg.attach(html_part)

        # Відправляємо через Gmail SMTP
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASSWORD)

            # Відправляємо на всі адреси (якщо їх кілька через кому)
            recipients = [email.strip() for email in EMAIL_TO.split(',')]
            server.sendmail(EMAIL_USER, recipients, msg.as_string())

        logger.info(f"Email-повідомлення відправлено: {subject}")
    except Exception as e:
        logger.error(f"Помилка при відправці email: {e}")


def format_date(date_str: str) -> str:
    """Форматування дати для відображення"""
    date = datetime.strptime(date_str, '%Y-%m-%d')
    return date.strftime('%d.%m.%Y')

UKRAINIAN_WEEKDAYS_ACCUSATIVE = [
    "понеділок",
    "вівторок",
    "середу",
    "четвер",
    "п'ятницю",
    "суботу",
    "неділю"
]


def get_weekday_accusative(date_str: str) -> str:
    """Повертає назву дня тижня у знахідному відмінку"""
    date = datetime.strptime(date_str, '%Y-%m-%d')
    return UKRAINIAN_WEEKDAYS_ACCUSATIVE[date.weekday()]


def chunk_list(lst, n):
    """Розбиття списку на частини по n елементів"""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def trim_text(text: Optional[str], limit: int = 200) -> str:
    """Обрізає текст до вказаної довжини для логів"""
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Базове логування кожного апдейту Telegram"""
    try:
        user = update.effective_user.id if update.effective_user else None
        chat = update.effective_chat.id if update.effective_chat else None
        update_dict = update.to_dict()
        logger.debug(
            "Отримано апдейт: user=%s chat=%s keys=%s payload=%s",
            user,
            chat,
            list(update_dict.keys()),
            json.dumps(update_dict, ensure_ascii=False)
        )
    except Exception as err:
        logger.debug(f"Не вдалося серіалізувати апдейт: {err}")


async def rate_limit_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перевірка rate limit для користувача"""
    if not update.effective_user:
        return

    user_id = update.effective_user.id

    # Адміни не підпадають під rate limit
    if is_admin(user_id):
        return

    # Перевірити rate limit
    is_limited, seconds = rate_limiter.is_rate_limited(user_id)

    if is_limited:
        logger.warning(f"User {user_id} is rate limited. Remaining: {seconds}s")
        try:
            if update.message:
                await update.message.reply_text(
                    f"⚠️ Ви надсилаєте запити занадто часто.\n"
                    f"Спробуйте знову через {seconds} секунд.",
                    parse_mode=ParseMode.HTML
                )
            elif update.callback_query:
                await update.callback_query.answer(
                    f"Ви надсилаєте запити занадто часто. Зачекайте {seconds}с",
                    show_alert=True
                )
        except Exception as e:
            logger.error(f"Помилка при відповіді на rate limit: {e}")

        # Припинити обробку апдейту
        raise Exception("Rate limit exceeded")


async def auto_delete_message(context, chat_id: int, message_id: int, delay: int = 3):
    """Автоматичне видалення повідомлення через вказану кількість секунд"""
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.debug(f"Не вдалося видалити повідомлення {message_id}: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальний обробник помилок"""
    logger.error(f"Exception while handling an update:", exc_info=context.error)

    # Обробка специфічних помилок
    if isinstance(context.error, Forbidden):
        # Користувач заблокував бота
        if update and hasattr(update, 'effective_user') and update.effective_user:
            user_id = update.effective_user.id
            db.block_user(user_id)
            logger.info(f"User {user_id} blocked the bot - marked as blocked in DB")

    elif isinstance(context.error, BadRequest):
        logger.warning(f"Bad request: {context.error}")

    elif isinstance(context.error, TimedOut):
        logger.warning(f"Request timed out: {context.error}")

    elif isinstance(context.error, NetworkError):
        logger.warning(f"Network error: {context.error}")

    # Повідомлення користувачу при можливості
    try:
        if update and hasattr(update, 'effective_message') and update.effective_message:
            effective_message = update.effective_message
            user = update.effective_user if hasattr(update, 'effective_user') else None

            if user and is_admin(user.id):
                await send_admin_message(
                    context,
                    effective_message.chat_id,
                    "Вибачте, сталася помилка. Спробуйте ще раз або зверніться до адміністратора.",
                    reply_to_message_id=effective_message.message_id
                )
            else:
                await effective_message.reply_text(
                    "Вибачте, сталася помилка. Спробуйте ще раз або зверніться до адміністратора."
                )
    except Exception as e:
        logger.error(f"Could not send error message to user: {e}")


async def answer_callback_query(query, *args, **kwargs):
    """Безпечна відповідь на callback_query (ігнорує мережеві збої Telegram)"""
    if not query:
        return

    try:
        await query.answer(*args, **kwargs)
    except NetworkError as err:
        logger.warning(f"Не вдалося відповісти на callback_query: {err}")


def should_auto_delete_admin_message(chat_id: int) -> bool:
    """Перевірка чи повідомлення має автоматично видалятись (для адмінських чатів)"""
    return chat_id in ADMIN_IDS


def schedule_admin_message_cleanup(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    try:
        if getattr(context, "application", None):
            context.application.create_task(
                auto_delete_message(context, chat_id, message_id, delay=ADMIN_MESSAGE_TTL)
            )
        else:
            asyncio.create_task(auto_delete_message(context, chat_id, message_id, delay=ADMIN_MESSAGE_TTL))
    except Exception as err:
        logger.debug(f"Не вдалося запланувати видалення повідомлення адміністратора: {err}")


async def send_admin_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    *,
    auto_delete: bool = True,
    **kwargs
):
    """Надіслати повідомлення та прибрати його через 15 сек, якщо потрібно"""
    logger.debug(
        "Відправка повідомлення адміну: chat_id=%s, auto_delete=%s, kwargs=%s, text=%s",
        chat_id,
        auto_delete,
        kwargs,
        trim_text(text)
    )
    try:
        message = await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except BadRequest as err:
        error_text = str(err)
        if "Message to be replied not found" in error_text and kwargs.get("reply_to_message_id"):
            # Повторити відправку без реплаю, якщо оригінал вже видалили
            retry_kwargs = dict(kwargs)
            retry_kwargs.pop("reply_to_message_id", None)
            logger.debug(
                "Повторна відправка без reply_to_message_id через помилку Telegram: %s", error_text
            )
            message = await context.bot.send_message(chat_id=chat_id, text=text, **retry_kwargs)
        else:
            raise
    logger.debug(
        "Повідомлення адміну надіслано: chat_id=%s, message_id=%s",
        chat_id,
        getattr(message, "message_id", None)
    )
    if should_auto_delete_admin_message(chat_id) and auto_delete:
        schedule_admin_message_cleanup(context, chat_id, message.message_id)
    return message


async def send_admin_message_from_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    auto_delete: bool = True,
    **kwargs
):
    """Відправити повідомлення, враховуючи чи є користувач адміністратором"""
    message = update.message
    user = update.effective_user

    if message is None:
        chat_id = update.effective_chat.id if update.effective_chat else ADMIN_ID
        logger.debug(
            "Відправка повідомлення (update без message): chat_id=%s text=%s kwargs=%s",
            chat_id,
            trim_text(text),
            kwargs
        )
        return await send_admin_message(context, chat_id, text, auto_delete=auto_delete, **kwargs)

    if user and is_admin(user.id):
        kwargs.setdefault("reply_to_message_id", message.message_id)
        logger.debug(
            "Відправка reply адміну (update): chat_id=%s text=%s kwargs=%s",
            message.chat_id,
            trim_text(text),
            kwargs
        )
        return await send_admin_message(context, message.chat_id, text, auto_delete=auto_delete, **kwargs)

    logger.debug(
        "Відправка повідомлення користувачу (update): chat_id=%s text=%s kwargs=%s",
        message.chat_id,
        trim_text(text),
        kwargs
    )
    return await message.reply_text(text, **kwargs)


async def send_admin_message_from_query(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    auto_delete: bool = True,
    **kwargs
):
    """Відправити повідомлення на callback, видаляючи його через 15 сек для адміністратора"""
    chat_id = query.message.chat_id
    user = query.from_user

    if user and is_admin(user.id):
        kwargs.setdefault("reply_to_message_id", query.message.message_id)
        logger.debug(
            "Відправка повідомлення через callback адміну: chat_id=%s text=%s kwargs=%s",
            chat_id,
            trim_text(text),
            kwargs
        )
        return await send_admin_message(context, chat_id, text, auto_delete=auto_delete, **kwargs)

    logger.debug(
        "Відправка повідомлення через callback користувачу: chat_id=%s text=%s kwargs=%s",
        chat_id,
        trim_text(text),
        kwargs
    )
    return await query.message.reply_text(text, **kwargs)


async def delete_admin_message(message):
    """Спроба видалити повідомлення адміністратора після обробки"""
    if not message or not message.from_user:
        return
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception as err:
        logger.debug(f"Не вдалося видалити повідомлення адміністратора: {err}")


async def clear_admin_dialog(context: ContextTypes.DEFAULT_TYPE, key: Optional[str] = None):
    """Видаляє активні адмінські повідомлення з контексту"""
    dialogs = context.chat_data.get('admin_dialogs')
    if not dialogs:
        return

    day_summary_cache = context.bot_data.get('day_summary_messages', {})
    protected_message_ids = {
        mid for mid in day_summary_cache.values()
        if isinstance(mid, int) and mid > 0
    }
    group_ids_raw = {GROUP_ID}
    stored_group_id = context.bot_data.get('group_id')
    if stored_group_id:
        group_ids_raw.add(stored_group_id)
    group_ids = {str(gid) for gid in group_ids_raw if gid}

    keys = [key] if key else list(dialogs.keys())
    keys_to_process = set(keys)
    keep_entries = {
        stored_key: entry
        for stored_key, entry in dialogs.items()
        if stored_key not in keys_to_process
    }

    for dialog_key in keys:
        entry = dialogs.get(dialog_key)
        if not entry:
            continue

        entry_chat_id = entry.get('chat_id')
        entry_message_id = entry.get('message_id')

        should_keep = (
            dialog_key.startswith('day_summary_')
            or (entry_message_id in protected_message_ids)
            or (group_ids and entry_chat_id is not None and str(entry_chat_id) in group_ids)
        )

        if should_keep:
            keep_entries[dialog_key] = entry
            continue

        try:
            await context.bot.delete_message(
                chat_id=entry_chat_id,
                message_id=entry_message_id
            )
            logger.debug("Видалено адмінський діалог: key=%s chat_id=%s message_id=%s", dialog_key, entry_chat_id, entry_message_id)
        except Exception as err:
            logger.debug(f"Не вдалося видалити адмінський діалог '{dialog_key}': {err}")

    if keep_entries:
        context.chat_data['admin_dialogs'] = keep_entries
    else:
        context.chat_data.pop('admin_dialogs', None)


async def register_admin_dialog(context: ContextTypes.DEFAULT_TYPE, key: str, message):
    """Реєструє нове адмінське діалогове вікно, замінивши попереднє"""
    if not message:
        return

    dialogs = context.chat_data.get('admin_dialogs')
    existing = dialogs.get(key) if dialogs else None

    if existing and existing['chat_id'] == message.chat_id and existing['message_id'] == message.message_id:
        return

    await clear_admin_dialog(context, key)

    dialogs = context.chat_data.setdefault('admin_dialogs', {})
    dialogs[key] = {'chat_id': message.chat_id, 'message_id': message.message_id}


# ==================== КОМАНДИ ====================

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = False):
    """Відображення головного меню адміністратора"""
    # Перевірка типу чату
    if not await require_private_chat(update, context):
        return

    if update.callback_query:
        query = update.callback_query
        await answer_callback_query(query)
        source_message = query.message
        chat_id = source_message.chat_id

        if edit_message:
            try:
                await source_message.edit_text(
                    "Оберіть дію:",
                    reply_markup=get_admin_keyboard()
                )
                await register_admin_dialog(context, 'admin_menu', source_message)
                dialogs = context.chat_data.get('admin_dialogs')
                if dialogs:
                    dialogs.pop('admin_dialog', None)
                return
            except Exception as err:
                logger.debug(f"Не вдалося оновити повідомлення меню: {err}")

        await clear_admin_dialog(context, 'admin_dialog')
        menu_message = await send_admin_message(
            context,
            chat_id,
            "Оберіть дію:",
            reply_markup=get_admin_keyboard(),
            auto_delete=False
        )
        await register_admin_dialog(context, 'admin_menu', menu_message)
        return

    message = update.message
    await clear_admin_dialog(context, 'admin_menu')
    await clear_admin_dialog(context, 'admin_dialog')
    menu_message = await send_admin_message(
        context,
        message.chat_id,
        "Оберіть дію:",
        reply_markup=get_admin_keyboard(),
        reply_to_message_id=message.message_id,
        auto_delete=False
    )
    await register_admin_dialog(context, 'admin_menu', menu_message)

    if update.message and is_admin(update.effective_user.id):
        await delete_admin_message(update.message)


async def show_admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Відображення меню налаштувань адміністратора"""
    # Перевірка типу чату
    if not await require_private_chat(update, context):
        return

    # Підтримка як для callback_query, так і для text
    if update.callback_query:
        query = update.callback_query
        await answer_callback_query(query)
        source_message = query.message
        is_callback = True
    else:
        source_message = update.message
        is_callback = False

    if not is_admin(update.effective_user.id):
        await send_admin_message(context, source_message.chat_id, "Немає доступу", reply_to_message_id=source_message.message_id)
        return

    keyboard = [
        [InlineKeyboardButton("💉 Типи процедур", callback_data="admin_procedure_types")],
        [InlineKeyboardButton("🚫 Заблокувати користувача", callback_data="admin_block_user")],
        [InlineKeyboardButton("🗑️ Очистити БД", callback_data="admin_clear_db")],
        [InlineKeyboardButton("❌ Закрити", callback_data="close_message")]
    ]

    text = f"Налаштування:\n\n📦 Версія бота: {VERSION}"

    if is_callback:
        await clear_admin_dialog(context, 'admin_dialog')
        await source_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        await register_admin_dialog(context, 'admin_dialog', source_message)
    else:
        await clear_admin_dialog(context, 'admin_dialog')
        dialog_message = await send_admin_message(
            context,
            source_message.chat_id,
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            reply_to_message_id=source_message.message_id,
            auto_delete=False
        )
        await register_admin_dialog(context, 'admin_dialog', dialog_message)
        await delete_admin_message(source_message)


async def handle_admin_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка текстових команд з меню адміністратора"""
    text = update.message.text
    user_id = update.effective_user.id

    # Перевірка чи користувач є адміном
    if not is_admin(user_id):
        return

    if text == "📋 Заходи":
        await clear_admin_dialog(context, 'admin_dialog')
        events = db.get_active_events()

        if not events:
            dialog_message = await send_admin_message_from_update(
                update,
                context,
                "Немає активних заходів",
                reply_markup=get_admin_keyboard(),
                auto_delete=False
            )
            await register_admin_dialog(context, 'admin_dialog', dialog_message)
            await delete_admin_message(update.message)
            return

        keyboard = []

        for event in events:
            keyboard.append([
                InlineKeyboardButton(
                    f"📅 {event['procedure_type']} - {format_date(event['date'])} о {event['time']}",
                    callback_data="noop"
                )
            ])

            keyboard.append([
                InlineKeyboardButton("📋 Заявки", callback_data=f"view_apps_{event['id']}"),
                InlineKeyboardButton("❌ Скасувати", callback_data=f"cancel_event_{event['id']}")
            ])

        keyboard.append([InlineKeyboardButton("📚 Минулі заходи", callback_data="past_events")])
        dialog_message = await send_admin_message_from_update(
            update,
            context,
            "Активні заходи:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            auto_delete=False
        )
        await register_admin_dialog(context, 'admin_dialog', dialog_message)
        await delete_admin_message(update.message)

    elif text == "⚙️":
        await show_admin_settings(update, context)
        await delete_admin_message(update.message)


def get_user_keyboard():
    """Отримати статичну клавіатуру користувача"""
    keyboard = [
        [
            KeyboardButton("📋 Мої заявки"),
            KeyboardButton("ℹ️ Інформація")
        ]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_admin_keyboard():
    """Отримати статичну клавіатуру адміністратора"""
    keyboard = [
        [
            KeyboardButton("🆕 Новий захід"),
            KeyboardButton("📋 Заходи"),
            KeyboardButton("⚙️")
        ]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def show_user_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = False):
    """Відображення головного меню користувача"""
    # Перевірка типу чату
    if not is_private_chat(update):
        return

    text = (
        "Вітаємо!\n\n"
        "Цей бот допоможе вам записатися на косметологічні процедури.\n\n"
        "Щоб подати заявку на участь, натисніть на повідомлення про захід в нашому каналі."
    )

    await send_admin_message_from_update(
        update,
        context,
        text,
        reply_markup=get_user_keyboard()
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка команди /start"""
    # Перевірка типу чату
    if not is_private_chat(update):
        logger.debug(f"start() ignored in non-private chat: {update.effective_chat.type}")
        return ConversationHandler.END

    user_id = update.effective_user.id
    db.create_user(user_id)
    admin_message = update.message if (update.message and is_admin(user_id)) else None

    logger.info(f"start() викликано для user_id={user_id}, args={context.args}")

    # Перевірка чи користувач заблокований
    user = db.get_user(user_id)
    if user and user.get('is_blocked'):
        await send_admin_message_from_update(update, context, "Вибачте, ваш доступ до бота заблоковано.")
        if admin_message:
            await delete_admin_message(admin_message)
        return ConversationHandler.END

    # Перевірка deep link для подачі заявки
    if context.args and len(context.args) > 0:
        payload = context.args[0]

        if payload.startswith('event_'):
            logger.info(f"Deep link на окрему процедуру: {payload}")
            try:
                parts = payload.split('_')
                event_id = int(parts[1])
                logger.info(f"Event ID: {event_id}")

                event = db.get_event(event_id)
                if not event:
                    logger.warning(f"Захід {event_id} не знайдено")
                    await send_admin_message_from_update(update, context, "Захід не знайдено або вже завершено.")
                    if admin_message:
                        await delete_admin_message(admin_message)
                    return ConversationHandler.END
                if event['status'] != 'published':
                    logger.warning(f"Захід {event_id} не опублікований, статус: {event['status']}")
                    await send_admin_message_from_update(update, context, "Цей захід більше не приймає заявки.")
                    if admin_message:
                        await delete_admin_message(admin_message)
                    return ConversationHandler.END

                context.user_data.pop('application', None)
                context.user_data.pop('selected_event_ids', None)
                context.user_data.pop('available_events', None)
                context.user_data['apply_event_ids'] = [event_id]
                logger.info(f"Викликаю apply_event_start для event_id={event_id}")
                if admin_message:
                    await delete_admin_message(admin_message)
                return await apply_event_start(update, context)
            except (ValueError, IndexError) as e:
                logger.error(f"Помилка парсингу event_id: {e}")
                await send_admin_message_from_update(update, context, "Невірне посилання на захід.")
                if admin_message:
                    await delete_admin_message(admin_message)
                return ConversationHandler.END

        if payload.startswith('day_'):
            logger.info(f"Deep link на розклад дня: {payload}")
            raw = payload[4:]
            if ',' in raw:
                raw = raw.replace(',', '_')
            parts = [part for part in raw.split('_') if part]
            if len(parts) < 2:
                await send_admin_message_from_update(update, context, "Посилання на розклад пошкоджено або застаріло.")
                if admin_message:
                    await delete_admin_message(admin_message)
                return ConversationHandler.END

            # Перший елемент — timestamp (ігноруємо), решта — ID заходів
            event_ids = []
            for part in parts[1:]:
                if part.isdigit():
                    event_ids.append(int(part))

            if not event_ids:
                await send_admin_message_from_update(update, context, "Посилання на розклад не містить активних процедур.")
                if admin_message:
                    await delete_admin_message(admin_message)
                return ConversationHandler.END

            events = db.get_events_by_ids(event_ids)
            events = [event for event in events if event['status'] == 'published']

            if not events:
                await send_admin_message_from_update(update, context, "На жаль, ці процедури вже недоступні.")
                if admin_message:
                    await delete_admin_message(admin_message)
                return ConversationHandler.END

            # Зберігаємо список доступних процедур для вибору
            events.sort(key=lambda item: (item['date'], item['time'], item['id']))
            context.user_data.pop('apply_event_ids', None)
            context.user_data.pop('application', None)
            context.user_data.pop('selection_message_id', None)
            context.user_data.pop('selection_chat_id', None)
            context.user_data['available_events'] = events
            context.user_data['selected_event_ids'] = set()
            if admin_message:
                await delete_admin_message(admin_message)
            return await show_multi_event_selection(update.message, context)

    if is_admin(user_id):
        await clear_admin_dialog(context, 'admin_dialog')
        await show_admin_menu(update, context)
    else:
        await show_user_menu(update, context)
    if admin_message:
        await delete_admin_message(admin_message)


async def admin_create_event_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка кнопки 'Створити захід'"""
    # Перевірка типу чату
    if not await require_private_chat(update, context):
        return ConversationHandler.END

    query = update.callback_query

    if not is_admin(query.from_user.id):
        await answer_callback_query(query)
        await send_admin_message_from_query(query, context, "Немає доступу")
        return ConversationHandler.END

    await answer_callback_query(query)

    # Видалити останнє повідомлення незакінченого створення заходу, якщо воно є
    if 'last_event_form_message' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=context.user_data['last_event_form_message']
            )
        except Exception as e:
            logger.debug(f"Не вдалося видалити форму створення заходу: {e}")

    # Видалити кнопки з поточного меню одразу (замінити на текст без кнопок)
    try:
        await query.edit_message_text("Створення нового заходу...")
    except:
        pass

    # Зберегти ID попереднього меню перед очищенням
    prev_menu_id = context.user_data.get('last_admin_menu_id')

    # Викликаємо логіку створення заходу
    await clear_admin_dialog(context)
    context.user_data.clear()
    context.user_data['schedule'] = {'date': None, 'events': []}
    context.user_data['event'] = {}
    context.user_data['menu_to_delete'] = prev_menu_id  # Зберегти ID меню для видалення після завершення

    date_options = generate_date_options()
    # Групуємо дати по 4 в рядок (4 стовпчики)
    date_buttons = [InlineKeyboardButton(opt['display'], callback_data=f"date_{opt['date']}")
                    for opt in date_options]
    keyboard = list(chunk_list(date_buttons, 4))
    keyboard.append([InlineKeyboardButton("❌ Закрити", callback_data="close_admin_dialog")])

    sent_msg = await send_admin_message_from_query(query, context, 
        "Оберіть дату заходу:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        auto_delete=False
    )
    await register_admin_dialog(context, 'admin_dialog', sent_msg)
    context.user_data['last_event_form_message'] = sent_msg.message_id

    return CREATE_EVENT_DATE


async def admin_manage_events_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка кнопки 'Переглянути заходи'"""
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return

    events = db.get_active_events()

    if not events:
        keyboard = [[InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")]]
        await query.edit_message_text(
            "Немає активних заходів",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    keyboard = []

    for event in events:
        # Широка кнопка з назвою заходу (без дії)
        keyboard.append([
            InlineKeyboardButton(
                f"📅 {event['procedure_type']} - {format_date(event['date'])} о {event['time']}",
                callback_data="noop"
            )
        ])

        # Дві кнопки по 50% ширини
        keyboard.append([
            InlineKeyboardButton("📋 Заявки", callback_data=f"view_apps_{event['id']}"),
            InlineKeyboardButton("❌ Скасувати", callback_data=f"cancel_event_{event['id']}")
        ])

    keyboard.append([InlineKeyboardButton("📚 Минулі заходи", callback_data="past_events")])
    keyboard.append([InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")])
    await query.edit_message_text("Активні заходи:", reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_past_events_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка кнопки 'Минулі заходи'"""
    query = update.callback_query
    await answer_callback_query(query)

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return

    events = db.get_past_events()

    if not events:
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="admin_manage_events")]]
        await query.edit_message_text(
            "Немає минулих заходів",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    keyboard = []

    for event in events:
        # Широка кнопка з назвою заходу (без дії)
        keyboard.append([
            InlineKeyboardButton(
                f"📅 {event['procedure_type']} - {format_date(event['date'])} о {event['time']}",
                callback_data="noop"
            )
        ])

        # Кнопка для перегляду заявок
        keyboard.append([
            InlineKeyboardButton("📋 Заявки", callback_data=f"view_apps_{event['id']}")
        ])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_manage_events")])
    await query.edit_message_text("Минулі заходи:", reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_block_user_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка кнопки 'Заблокувати користувача'"""
    query = update.callback_query
    await answer_callback_query(query)

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel_block")]]
    await query.edit_message_text(
        "Введіть ID користувача, якого потрібно заблокувати:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return BLOCK_USER_ID


async def block_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка введення ID користувача для блокування"""
    try:
        user_id_to_block = int(update.message.text)
        db.block_user(user_id_to_block)

        keyboard = [[InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")]]
        dialog_message = await send_admin_message_from_update(
            update,
            context,
            f"✅ Користувача {user_id_to_block} заблоковано",
            reply_markup=InlineKeyboardMarkup(keyboard),
            auto_delete=False
        )
        await register_admin_dialog(context, 'admin_dialog', dialog_message)
    except ValueError:
        keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel_block")]]
        error_msg = await send_admin_message_from_update(update, context, 
            "❌ Невірний ID користувача. Спробуйте ще раз:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        # Автоматично видалити через 3 секунди
        asyncio.create_task(auto_delete_message(context, update.effective_chat.id, error_msg.message_id))
        if update.message:
            await delete_admin_message(update.message)
        return BLOCK_USER_ID

    if update.message:
        await delete_admin_message(update.message)

    return ConversationHandler.END


async def cancel_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скасування блокування користувача"""
    query = update.callback_query
    await answer_callback_query(query)

    await show_admin_menu(update, context, edit_message=True)

    return ConversationHandler.END


# ==================== ОЧИСТКА БД ====================

async def admin_clear_db_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка кнопки очистки БД - запит пароля"""
    query = update.callback_query
    await answer_callback_query(query)

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel_clear_db")]]

    await query.edit_message_text(
        "⚠️ УВАГА! Очистка бази даних\n\n"
        "Будуть видалені:\n"
        "• Всі заходи\n"
        "• Всі заявки\n"
        "• Всі фото\n"
        "• Всі користувачі\n"
        "• Всі типи процедур (окрім початкових)\n\n"
        "❗️ Ця дія незворотна!\n\n"
        "Для підтвердження введіть пароль:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    await register_admin_dialog(context, 'admin_dialog', query.message)

    return CLEAR_DB_PASSWORD


async def clear_db_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перевірка пароля та виконання очистки БД"""
    if not is_admin(update.effective_user.id):
        await send_admin_message_from_update(update, context, "Немає доступу")
        return ConversationHandler.END

    password = update.message.text.strip()

    # Видалити повідомлення з паролем для безпеки
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Не вдалося видалити повідомлення з паролем: {e}")

    if not DB_CLEAR_PASSWORD:
        keyboard = [[InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")]]
        await send_admin_message_from_update(
            update,
            context,
            "❌ Очистка БД недоступна. DB_CLEAR_PASSWORD не налаштовано в .env",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END

    # Безпечне порівняння паролів з захистом від timing attacks
    if secrets.compare_digest(password, DB_CLEAR_PASSWORD):
        try:
            dialog_message = await send_admin_message(
                context,
                update.effective_chat.id,
                "⏳ Очистка бази даних..."
            )
            db.clear_all_data()
            await asyncio.sleep(1)
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=dialog_message.message_id,
                text="✅ База даних успішно очищена!"
            )
            await asyncio.sleep(2)

            # Відправити адмін меню через context.bot, бо update.message вже видалено
            await clear_admin_dialog(context)
            menu_message = await send_admin_message(
                context,
                update.effective_chat.id,
                "Оберіть дію:",
                reply_markup=get_admin_keyboard(),
                auto_delete=False
            )
            await register_admin_dialog(context, 'admin_dialog', menu_message)
        except Exception as e:
            logger.error(f"Помилка при очистці БД: {e}", exc_info=True)
            await send_admin_message(
                context,
                update.effective_chat.id,
                "❌ Помилка при очистці бази даних.\nДеталі записано в лог."
            )
            await asyncio.sleep(2)

            # Відправити адмін меню через context.bot
            await clear_admin_dialog(context)
            menu_message = await send_admin_message(
                context,
                update.effective_chat.id,
                "Оберіть дію:",
                reply_markup=get_admin_keyboard(),
                auto_delete=False
            )
            await register_admin_dialog(context, 'admin_dialog', menu_message)
    else:
        keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel_clear_db")]]
        await send_admin_message(
            context,
            update.effective_chat.id,
            "❌ Невірний пароль!\n\nСпробуйте ще раз або натисніть 'Скасувати':",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CLEAR_DB_PASSWORD

    return ConversationHandler.END


async def cancel_clear_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скасування очистки БД"""
    query = update.callback_query
    await answer_callback_query(query)

    await show_admin_menu(update, context, edit_message=True)

    return ConversationHandler.END


# ==================== ТИПИ ПРОЦЕДУР ====================

async def admin_procedure_types(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показ списку типів процедур"""
    query = update.callback_query

    # Безпечний виклик answer() - може вже бути викликаний
    try:
        await answer_callback_query(query)
    except Exception:
        pass

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return

    types = db.get_all_procedure_types()

    if not types:
        keyboard = [[InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")]]
        await query.edit_message_text(
            "Немає типів процедур",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    keyboard = []

    for proc_type in types:
        status = "✅" if proc_type['is_active'] else "❌"
        keyboard.append([
            InlineKeyboardButton(
                f"{status} {proc_type['name']}",
                callback_data=f"pt_view_{proc_type['id']}"
            )
        ])

    keyboard.append([InlineKeyboardButton("➕ Додати новий тип", callback_data="pt_add")])
    keyboard.append([InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")])

    await query.edit_message_text(
        "💉 Типи процедур:\n\n"
        "✅ - активний\n"
        "❌ - вимкнений\n\n"
        "Натисніть на тип для редагування",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def view_procedure_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перегляд та редагування типу процедури"""
    query = update.callback_query

    # Безпечний виклик answer() - може вже бути викликаний
    try:
        await answer_callback_query(query)
    except Exception:
        pass

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return

    type_id = int(query.data.split('_')[2])
    proc_type = db.get_procedure_type(type_id)

    if not proc_type:
        await query.edit_message_text("Тип не знайдено")
        return

    status_text = "✅ Активний" if proc_type['is_active'] else "❌ Вимкнений"
    toggle_text = "❌ Вимкнути" if proc_type['is_active'] else "✅ Увімкнути"

    keyboard = [
        [InlineKeyboardButton("✏️ Редагувати назву", callback_data=f"pt_edit_{type_id}")],
        [InlineKeyboardButton(toggle_text, callback_data=f"pt_toggle_{type_id}")],
        [InlineKeyboardButton("🗑 Видалити", callback_data=f"pt_delete_{type_id}")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_procedure_types")]
    ]

    await query.edit_message_text(
        f"💉 Тип процедури:\n\n"
        f"<b>Назва:</b> {proc_type['name']}\n"
        f"<b>Статус:</b> {status_text}\n"
        f"<b>Створено:</b> {proc_type['created_at']}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def toggle_procedure_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вимкнути/увімкнути тип процедури"""
    query = update.callback_query
    await answer_callback_query(query)

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return

    type_id = int(query.data.split('_')[2])
    db.toggle_procedure_type(type_id)

    # Оновити відображення
    await view_procedure_type(update, context)


async def delete_procedure_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Видалити тип процедури"""
    query = update.callback_query
    await answer_callback_query(query)

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return

    type_id = int(query.data.split('_')[2])
    proc_type = db.get_procedure_type(type_id)

    if not proc_type:
        await query.edit_message_text("Тип не знайдено")
        return

    keyboard = [
        [
            InlineKeyboardButton("✅ Так, видалити", callback_data=f"pt_delete_confirm_{type_id}"),
            InlineKeyboardButton("❌ Скасувати", callback_data=f"pt_view_{type_id}")
        ]
    ]

    await query.edit_message_text(
        f"⚠️ Видалити тип процедури '{proc_type['name']}'?\n\n"
        f"Якщо цей тип використовується в заходах, видалення буде неможливим.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def delete_procedure_type_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Підтвердження видалення типу"""
    query = update.callback_query
    await answer_callback_query(query)

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return

    type_id = int(query.data.split('_')[3])
    proc_type = db.get_procedure_type(type_id)

    if not proc_type:
        await query.edit_message_text("❌ Тип не знайдено")
        return

    success = db.delete_procedure_type(type_id)

    if success:
        keyboard = [[InlineKeyboardButton("◀️ Назад до списку", callback_data="admin_procedure_types")]]
        await query.edit_message_text(
            f"✅ Тип процедури '{proc_type['name']}' успішно видалено!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        status_icon = "✅" if proc_type['is_active'] else "❌"
        keyboard = [
            [InlineKeyboardButton(f"{status_icon} Активувати/Деактивувати", callback_data=f"pt_toggle_{type_id}")],
            [InlineKeyboardButton("⬅️ Назад до списку", callback_data="admin_procedure_types")]
        ]

        await query.edit_message_text(
            f"❌ Неможливо видалити тип процедури '{proc_type['name']}'.\n\n"
            f"Цей тип використовується в заходах. "
            f"Ви можете деактивувати його замість видалення.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ConversationHandler для додавання типу процедури
async def add_procedure_type_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Початок додавання нового типу процедури"""
    query = update.callback_query
    await answer_callback_query(query)

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="pt_cancel")]]

    await query.edit_message_text(
        "➕ Додавання нового типу процедури\n\n"
        "Введіть назву типу процедури:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return ADD_PROCEDURE_TYPE_NAME


async def add_procedure_type_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка назви нового типу"""
    from constants import ADD_PROCEDURE_TYPE_NAME

    if not is_admin(update.effective_user.id):
        await send_admin_message_from_update(update, context, "Немає доступу")
        return ConversationHandler.END

    name = update.message.text.strip()

    if not name or len(name) > MAX_PROCEDURE_TYPE_NAME_LENGTH:
        error_msg = await send_admin_message_from_update(update, context,
            f"❌ Назва має бути від 1 до {MAX_PROCEDURE_TYPE_NAME_LENGTH} символів.\n\n"
            "Спробуйте ще раз:"
        )
        asyncio.create_task(auto_delete_message(context, update.effective_chat.id, error_msg.message_id))
        return ADD_PROCEDURE_TYPE_NAME

    try:
        type_id = db.create_procedure_type(name)
        success_msg = await send_admin_message_from_update(update, context, f"✅ Тип процедури '{name}' додано успішно!")

        # Показати адмін меню
        await show_admin_menu(update, context, edit_message=False)

        # Видалити повідомлення про успіх через 3 секунди
        asyncio.create_task(auto_delete_message(context, update.effective_chat.id, success_msg.message_id))

        return ConversationHandler.END
    except Exception as e:
        if "UNIQUE constraint failed" in str(e):
            error_msg = await send_admin_message_from_update(update, context, 
                "❌ Тип процедури з такою назвою вже існує.\n\n"
                "Введіть іншу назву:"
            )
            asyncio.create_task(auto_delete_message(context, update.effective_chat.id, error_msg.message_id))
            return ADD_PROCEDURE_TYPE_NAME
        else:
            logger.error(f"Помилка додавання типу процедури: {e}", exc_info=True)
            error_msg = await send_admin_message_from_update(update, context, "❌ Помилка при додаванні типу")
            asyncio.create_task(auto_delete_message(context, update.effective_chat.id, error_msg.message_id))
            return ConversationHandler.END


async def cancel_procedure_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скасування додавання/редагування типу"""
    query = update.callback_query
    await answer_callback_query(query)

    await show_admin_menu(update, context, edit_message=True)
    return ConversationHandler.END


# ConversationHandler для редагування типу процедури
async def edit_procedure_type_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Початок редагування типу процедури"""
    query = update.callback_query
    await answer_callback_query(query)

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return ConversationHandler.END

    type_id = int(query.data.split('_')[2])
    proc_type = db.get_procedure_type(type_id)

    if not proc_type:
        await query.edit_message_text("Тип не знайдено")
        return ConversationHandler.END

    context.user_data['edit_type_id'] = type_id

    keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="pt_cancel")]]

    await query.edit_message_text(
        f"✏️ Редагування типу процедури\n\n"
        f"Поточна назва: {proc_type['name']}\n\n"
        f"Введіть нову назву:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return EDIT_PROCEDURE_TYPE_NAME


async def edit_procedure_type_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка нової назви типу"""
    from constants import EDIT_PROCEDURE_TYPE_NAME

    if not is_admin(update.effective_user.id):
        await send_admin_message_from_update(update, context, "Немає доступу")
        return ConversationHandler.END

    name = update.message.text.strip()
    type_id = context.user_data.get('edit_type_id')

    if not type_id:
        error_msg = await send_admin_message_from_update(update, context, "❌ Помилка: тип не знайдено")
        asyncio.create_task(auto_delete_message(context, update.effective_chat.id, error_msg.message_id))
        return ConversationHandler.END

    if not name or len(name) > MAX_PROCEDURE_TYPE_NAME_LENGTH:
        error_msg = await send_admin_message_from_update(update, context,
            f"❌ Назва має бути від 1 до {MAX_PROCEDURE_TYPE_NAME_LENGTH} символів.\n\n"
            "Спробуйте ще раз:"
        )
        asyncio.create_task(auto_delete_message(context, update.effective_chat.id, error_msg.message_id))
        return EDIT_PROCEDURE_TYPE_NAME

    try:
        db.update_procedure_type(type_id, name)
        success_msg = await send_admin_message_from_update(update, context, f"✅ Назву змінено на '{name}'")

        # Показати адмін меню
        await show_admin_menu(update, context, edit_message=False)

        # Видалити повідомлення про успіх через 3 секунди
        asyncio.create_task(auto_delete_message(context, update.effective_chat.id, success_msg.message_id))

        return ConversationHandler.END
    except Exception as e:
        if "UNIQUE constraint failed" in str(e):
            error_msg = await send_admin_message_from_update(update, context, 
                "❌ Тип процедури з такою назвою вже існує.\n\n"
                "Введіть іншу назву:"
            )
            asyncio.create_task(auto_delete_message(context, update.effective_chat.id, error_msg.message_id))
            return EDIT_PROCEDURE_TYPE_NAME
        else:
            logger.error(f"Помилка редагування типу процедури: {e}")
            error_msg = await send_admin_message_from_update(update, context, "❌ Помилка при редагуванні типу")
            asyncio.create_task(auto_delete_message(context, update.effective_chat.id, error_msg.message_id))
            return ConversationHandler.END


async def cancel_event_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Підтвердження скасування заходу"""
    query = update.callback_query
    await answer_callback_query(query)

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return

    event_id = int(query.data.split('_')[2])
    event = db.get_event(event_id)

    if not event:
        await query.edit_message_text("Захід не знайдено")
        return

    keyboard = [
        [
            InlineKeyboardButton("✅ Так, скасувати", callback_data=f"confirm_cancel_event_{event_id}"),
            InlineKeyboardButton("❌ Ні, залишити", callback_data="admin_manage_events")
        ]
    ]

    await query.edit_message_text(
        f"Ви впевнені, що хочете скасувати захід?\n\n"
        f"Процедура: {event['procedure_type']}\n"
        f"Дата: {format_date(event['date'])} о {event['time']}\n\n"
        f"Всі заявки на цей захід будуть також скасовані.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def confirm_cancel_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скасування заходу після підтвердження"""
    query = update.callback_query
    await answer_callback_query(query)

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return

    event_id = int(query.data.split('_')[3])
    event = db.get_event(event_id)

    if not event:
        await query.edit_message_text("Захід не знайдено")
        return

    # Оновити статус заходу на 'cancelled'
    db.update_event_status(event_id, 'cancelled')

    # Відправити повідомлення всім кандидатам про скасування
    applications = db.get_applications_by_event(event_id)
    for app in applications:
        try:
            await context.bot.send_message(
                chat_id=app['user_id'],
                text=f"Захід '{event['procedure_type']}' {format_date(event['date'])} о {event['time']} скасовано.\n\n"
                     f"Вибачте за незручності."
            )
        except Exception as e:
            logger.error(f"Не вдалося надіслати повідомлення користувачу {app['user_id']}: {e}")

    # Видалити повідомлення з групи подій, якщо є message_id
    if event.get('message_id'):
        try:
            await context.bot.delete_message(
                chat_id=EVENTS_GROUP_ID,
                message_id=event['message_id']
            )
        except Exception as e:
            logger.error(f"Не вдалося видалити повідомлення з групи подій: {e}")

    await query.edit_message_text(
        f"Захід '{event['procedure_type']}' {format_date(event['date'])} о {event['time']} успішно скасовано.\n\n"
        f"Всім кандидатам надіслано повідомлення про скасування."
    )

    # Показати головне меню
    await show_admin_menu(update, context, edit_message=False)
    await update_day_summary(context, event['date'])


async def user_my_applications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показати актуальні заявки користувача (тільки майбутні заходи)"""
    # Перевірка типу чату
    if not await require_private_chat(update, context):
        return

    query = update.callback_query
    await answer_callback_query(query)

    user_id = query.from_user.id

    # Отримати всі заявки користувача
    all_applications = db.get_user_applications(user_id)

    # Поточна дата та час для фільтрації
    now = datetime.now(UKRAINE_TZ)
    current_date = now.date()
    current_time = now.time()

    # Фільтрувати тільки актуальні (майбутні) заявки
    applications = []
    archived_count = 0

    for app in all_applications:
        event_date = datetime.strptime(app['date'], '%Y-%m-%d').date()

        # Якщо дата в майбутньому - додаємо
        if event_date > current_date:
            applications.append(app)
        # Якщо дата сьогодні - перевіряємо час
        elif event_date == current_date:
            event_time = datetime.strptime(app['time'], '%H:%M').time()
            if event_time >= current_time:
                applications.append(app)
            else:
                archived_count += 1
        else:
            # Дата в минулому
            archived_count += 1

    if not applications:
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="user_back_to_menu")]]

        if archived_count > 0:
            info_text = (
                "У вас немає актуальних заявок.\n\n"
                f"ℹ️ Заявки на минулі заходи ({archived_count}) автоматично приховані.\n\n"
                "Щоб подати нову заявку, натисніть на повідомлення про захід в нашому каналі."
            )
        else:
            info_text = (
                "У вас поки немає заявок.\n\n"
                "Щоб подати заявку, натисніть на повідомлення про захід в нашому каналі."
            )

        await query.edit_message_text(
            info_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # Формуємо повідомлення з інформацією про архівовані заявки
    message = "📋 Ваші актуальні заявки:\n\n"

    if archived_count > 0:
        message = f"📋 Ваші актуальні заявки:\nℹ️ Приховано {archived_count} заявок на минулі заходи\n\n"

    keyboard = []

    for app in applications:
        status_emoji = {
            'pending': '⏳',
            'approved': '✅',
            'primary': '✅',
            'rejected': '❌',
            'cancelled': '🚫'
        }.get(app['status'], '❓')

        status_text = {
            'pending': 'Очікує розгляду',
            'approved': 'Схвалено',
            'primary': 'Схвалено',
            'rejected': 'Відхилено',
            'cancelled': 'Скасовано'
        }.get(app['status'], 'Невідомо')

        event_status = " (Захід скасовано)" if app['event_status'] == 'cancelled' else ""

        safe_procedure = safe_html(app['procedure_type'])
        message += f"{status_emoji} {safe_procedure}\n"
        message += f"Номер заявки: №{app['id']}\n"
        message += f"📅 {format_date(app['date'])} о {app['time']}\n"
        message += f"Статус: {status_text}{event_status}\n\n"

        # Додати кнопку скасування тільки для активних заявок (pending, approved, primary)
        if app['status'] in ['pending', 'approved', 'primary'] and app['event_status'] == 'published':
            keyboard.append([
                InlineKeyboardButton(
                    f"❌ Скасувати заявку на {app['procedure_type'][:20]}",
                    callback_data=f"cancel_app_{app['id']}"
                )
            ])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="user_back_to_menu")])
    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показати інформацію про бота"""
    # Перевірка типу чату
    if not await require_private_chat(update, context):
        return

    query = update.callback_query
    await answer_callback_query(query)

    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="user_back_to_menu")]]

    channel_text = f" {CHANNEL_LINK}" if CHANNEL_LINK else ""
    text = (
        "ℹ️ Інформація про бота\n\n"
        "Цей бот допоможе вам записатися на безкоштовні косметологічні процедури.\n\n"
        "Як це працює:\n"
        f"1️⃣ Підпишіться на наш канал{channel_text}\n"
        "2️⃣ Натисніть кнопку 'Подати заявку' під оголошенням про захід\n"
        "3️⃣ Заповніть форму заявки\n"
        "4️⃣ Очікуйте на схвалення адміністратора\n\n"
        "Якщо у вас є питання, зв'яжіться з адміністратором."
    )

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_user_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка текстових команд з меню користувача"""
    text = update.message.text
    user_id = update.effective_user.id

    # Перевірка чи користувач не є адміном
    if is_admin(user_id):
        return

    if text == "📋 Мої заявки":
        # Отримати всі заявки користувача
        all_applications = db.get_user_applications(user_id)

        # Поточна дата та час для фільтрації
        now = datetime.now(UKRAINE_TZ)
        current_date = now.date()
        current_time = now.time()

        # Фільтрувати тільки актуальні (майбутні) заявки
        applications = []
        archived_count = 0

        for app in all_applications:
            event_date = datetime.strptime(app['date'], '%Y-%m-%d').date()

            # Якщо дата в майбутньому - додаємо
            if event_date > current_date:
                applications.append(app)
            # Якщо дата сьогодні - перевіряємо час
            elif event_date == current_date:
                event_time = datetime.strptime(app['time'], '%H:%M').time()
                if event_time >= current_time:
                    applications.append(app)
                else:
                    archived_count += 1
            else:
                # Дата в минулому
                archived_count += 1

        if not applications:
            if archived_count > 0:
                info_text = (
                    "У вас немає актуальних заявок.\n\n"
                    f"ℹ️ Заявки на минулі заходи ({archived_count}) автоматично приховані.\n\n"
                    "Щоб подати нову заявку, натисніть на повідомлення про захід в нашому каналі."
                )
            else:
                info_text = (
                    "У вас поки немає заявок.\n\n"
                    "Щоб подати заявку, натисніть на повідомлення про захід в нашому каналі."
                )

            await send_admin_message_from_update(update, context,
                info_text,
                reply_markup=get_user_keyboard()
            )
            return

        # Формуємо повідомлення з інформацією про архівовані заявки
        message = "📋 Ваші актуальні заявки:\n\n"

        if archived_count > 0:
            message = f"📋 Ваші актуальні заявки:\nℹ️ Приховано {archived_count} заявок на минулі заходи\n\n"

        keyboard = []

        for app in applications:
            status_emoji = {
                'pending': '⏳',
                'approved': '✅',
                'primary': '✅',
                'rejected': '❌',
                'cancelled': '🚫'
            }.get(app['status'], '❓')

            status_text = {
                'pending': 'Очікує розгляду',
                'approved': 'Схвалено',
                'primary': 'Схвалено',
                'rejected': 'Відхилено',
                'cancelled': 'Скасовано'
            }.get(app['status'], 'Невідомо')

            event_status = " (Захід скасовано)" if app['event_status'] == 'cancelled' else ""

            safe_procedure = safe_html(app['procedure_type'])
            message += f"{status_emoji} {safe_procedure}\n"
            message += f"Номер заявки: №{app['id']}\n"
            message += f"📅 {format_date(app['date'])} о {app['time']}\n"
            message += f"Статус: {status_text}{event_status}\n\n"

            # Додати кнопку скасування тільки для активних заявок (pending, approved, primary)
            if app['status'] in ['pending', 'approved', 'primary'] and app['event_status'] == 'published':
                keyboard.append([
                    InlineKeyboardButton(
                        f"❌ Скасувати заявку на {app['procedure_type'][:20]}",
                        callback_data=f"cancel_app_{app['id']}"
                    )
                ])

        await send_admin_message_from_update(update, context,
            message,
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else get_user_keyboard(),
            parse_mode=ParseMode.HTML
        )

    elif text == "ℹ️ Інформація":
        channel_text = f" {CHANNEL_LINK}" if CHANNEL_LINK else ""
        info_text = (
            "ℹ️ Інформація про бота\n\n"
            "Цей бот допоможе вам записатися на безкоштовні косметологічні процедури.\n\n"
            "Як це працює:\n"
            f"1️⃣ Підпишіться на наш канал{channel_text}\n"
            "2️⃣ Натисніть кнопку 'Подати заявку' під оголошенням про захід\n"
            "3️⃣ Заповніть форму заявки\n"
            "4️⃣ Очікуйте на схвалення адміністратора\n\n"
            "Якщо у вас є питання, зв'яжіться з адміністратором."
        )

        await send_admin_message_from_update(update, context, info_text, reply_markup=get_user_keyboard())


async def cancel_user_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скасування заявки користувачем"""
    query = update.callback_query

    user_id = query.from_user.id
    app_id = int(query.data.split('_')[2])

    # Перевірити що заявка належить користувачу
    app = db.get_application(app_id)
    if not app or app['user_id'] != user_id:
        await answer_callback_query(query, "Помилка: заявка не знайдена", show_alert=True)
        return

    # Перевірити чи заявка вже скасована
    if app['status'] == 'cancelled':
        await answer_callback_query(query, "Заявка вже скасована", show_alert=True)
        # Оновити список заявок без змін статусу
        await user_my_applications(update, context)
        return

    # Отримати інформацію про подію
    event = db.get_event(app['event_id'])

    # Зберегти статус для повідомлення
    was_primary = app['status'] == 'primary'

    # Оновити статус заявки
    db.update_application_status(app_id, 'cancelled')
    db.recalculate_application_positions(app['event_id'])

    # Оновити денне підсумок
    if event:
        await update_day_summary(context, event['date'])
        await sync_event_filled_state(context, event['id'])

    # Оновити повідомлення в групі заявок або одиночне
    if not await refresh_group_application_message(context, app_id):
        await refresh_single_application_message(context, app_id)

    # Відправити повідомлення всім адміністраторам
    if event:
        status_text = "основний кандидат" if was_primary else "кандидат"
        admin_message = (
            f"⚠️ Кандидат скасував свою заявку\n\n"
            f"👤 {app['full_name']}\n"
            f"📞 {app['phone']}\n"
            f"Минулий статус: {status_text}\n\n"
            f"Процедура: {event['procedure_type']}\n"
            f"Дата: {format_date(event['date'])}\n"
            f"Час: {event['time']}"
        )
        await send_message_to_all_admins(context, admin_message)

    await answer_callback_query(query, "Заявку скасовано", show_alert=True)

    # Повернутися до списку заявок
    await user_my_applications(update, context)


async def user_back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повернення до головного меню користувача"""
    query = update.callback_query
    await answer_callback_query(query)

    await show_user_menu(update, context, edit_message=True)


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повернення до головного меню адміністратора"""
    query = update.callback_query
    await answer_callback_query(query)

    await show_admin_menu(update, context, edit_message=True)


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник для кнопок без дії (тільки для відображення)"""
    query = update.callback_query
    await answer_callback_query(query)


async def close_message_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Закрити (видалити) повідомлення"""
    query = update.callback_query
    await answer_callback_query(query)

    try:
        await query.message.delete()
    except Exception as e:
        logger.error(f"Не вдалося видалити повідомлення: {e}")


async def close_admin_dialog_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Закриває активний адміністративний діалог та повертає до меню"""
    query = update.callback_query
    await answer_callback_query(query)

    await clear_admin_dialog(context, 'admin_dialog')

    menu_message = await send_admin_message(
        context,
        query.message.chat_id,
        "Оберіть дію:",
        reply_markup=get_admin_keyboard(),
        auto_delete=False
    )
    await register_admin_dialog(context, 'admin_menu', menu_message)
    context.user_data.clear()

    return ConversationHandler.END

# ==================== СТВОРЕННЯ ЗАХОДУ (АДМІН) ====================

async def create_event_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Початок створення заходу"""
    if not is_admin(update.effective_user.id):
        await send_admin_message_from_update(update, context, "Немає доступу")
        return ConversationHandler.END

    await clear_admin_dialog(context, 'admin_dialog')
    context.user_data.clear()
    context.user_data['event'] = {}
    context.user_data['schedule'] = {'date': None, 'events': []}

    date_options = generate_date_options()
    # Групуємо дати по 4 в рядок (4 стовпчики)
    date_buttons = [InlineKeyboardButton(opt['display'], callback_data=f"date_{opt['date']}")
                    for opt in date_options]
    keyboard = list(chunk_list(date_buttons, 4))
    keyboard.append([InlineKeyboardButton("❌ Закрити", callback_data="close_admin_dialog")])

    dialog_message = await send_admin_message_from_update(update, context, 
        "Оберіть дату заходу:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        auto_delete=False
    )
    await register_admin_dialog(context, 'admin_dialog', dialog_message)
    if update.message:
        await delete_admin_message(update.message)

    return CREATE_EVENT_DATE


async def show_date_selection(query, context: ContextTypes.DEFAULT_TYPE):
    """Показати вибір дати"""
    date_options = generate_date_options()
    # Групуємо дати по 4 в рядок (4 стовпчики)
    date_buttons = [InlineKeyboardButton(opt['display'], callback_data=f"date_{opt['date']}")
                    for opt in date_options]
    keyboard = list(chunk_list(date_buttons, 4))
    keyboard.append([InlineKeyboardButton("❌ Закрити", callback_data="close_admin_dialog")])

    if query:
        await query.edit_message_text(
            "Оберіть дату заходу:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    return CREATE_EVENT_DATE


async def create_event_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка вибору дати"""
    query = update.callback_query
    await answer_callback_query(query)

    # Якщо це повернення назад, просто показуємо вибір дати
    if query.data == "back_to_date":
        return await show_date_selection(query, context)

    date = query.data.split('_', 1)[1]
    schedule = context.user_data.setdefault('schedule', {'date': None, 'events': []})
    if schedule['date'] and schedule['date'] != date:
        schedule['events'].clear()
    schedule['date'] = date
    context.user_data['event']['date'] = date

    return await show_time_selection(query, context)


def get_available_time_slots(event_date: Optional[str]) -> List[str]:
    """Повернути список доступних часових слотів з урахуванням поточного часу"""
    if not event_date:
        return TIME_SLOTS

    try:
        selected_date = datetime.strptime(event_date, '%Y-%m-%d').date()
    except ValueError:
        return TIME_SLOTS

    today = datetime.now(UKRAINE_TZ).date()
    if selected_date > today or selected_date < today:
        return TIME_SLOTS

    current_time = datetime.now(UKRAINE_TZ).time()
    available = []
    for slot in TIME_SLOTS:
        try:
            slot_time = datetime.strptime(slot, '%H:%M').time()
        except ValueError:
            continue
        if slot_time > current_time:
            available.append(slot)
    return available


async def show_time_selection(query, context: ContextTypes.DEFAULT_TYPE):
    """Показати вибір часу"""
    event_date = (
        context.user_data.get('event', {}).get('date')
        or context.user_data.get('schedule', {}).get('date')
    )
    available_slots = get_available_time_slots(event_date)

    if not available_slots:
        keyboard = [
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_date")],
            [InlineKeyboardButton("❌ Закрити", callback_data="close_admin_dialog")]
        ]
        await query.edit_message_text(
            "На вибрану дату неможливо створити захід.\n"
            "Оберіть іншу дату.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CREATE_EVENT_TIME

    keyboard = list(chunk_list(
        [InlineKeyboardButton(time, callback_data=f"time_{time}") for time in available_slots],
        5
    ))
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_date")])
    keyboard.append([InlineKeyboardButton("❌ Закрити", callback_data="close_admin_dialog")])

    await query.edit_message_text(
        "Оберіть час заходу:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_EVENT_TIME


async def create_event_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка вибору часу"""
    query = update.callback_query
    await answer_callback_query(query)

    # Якщо це повернення назад з екрану процедур, просто показуємо вибір часу
    if query.data == "back_to_time":
        return await show_time_selection(query, context)

    time = query.data.split('_', 1)[1]
    context.user_data['event']['time'] = time

    # Показати типи процедур з БД
    return await show_procedure_selection(query, context)


async def show_procedure_selection(query, context: ContextTypes.DEFAULT_TYPE):
    """Показати вибір процедури"""
    # Отримати активні типи процедур з БД
    procedure_types = db.get_active_procedure_types()

    if not procedure_types:
        await query.edit_message_text(
            "❌ Немає доступних типів процедур.\n\n"
            "Адміністратор має додати типи процедур через меню.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Закрити", callback_data="close_admin_dialog")]])
        )
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(ptype['name'], callback_data=f"proc_{ptype['id']}")]
                for ptype in procedure_types]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_time")])
    keyboard.append([InlineKeyboardButton("❌ Закрити", callback_data="close_admin_dialog")])

    await query.edit_message_text(
        "Оберіть тип процедури:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_EVENT_PROCEDURE


async def create_event_procedure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка вибору процедури"""
    query = update.callback_query
    await answer_callback_query(query)

    # Якщо це повернення назад, просто показуємо вибір процедури
    if query.data == "back_to_procedure":
        return await show_procedure_selection(query, context)

    proc_type_id = int(query.data.split('_')[1])
    proc_type = db.get_procedure_type(proc_type_id)

    if not proc_type:
        await query.edit_message_text("❌ Тип процедури не знайдено")
        return ConversationHandler.END

    context.user_data['event']['procedure'] = proc_type['name']

    keyboard = [
        [
            InlineKeyboardButton("✅ Так", callback_data="photo_yes"),
            InlineKeyboardButton("❌ Ні", callback_data="photo_no")
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_procedure")],
        [InlineKeyboardButton("❌ Закрити", callback_data="close_admin_dialog")]
    ]

    await query.edit_message_text(
        "Чи потрібно кандидатам надавати фото зони?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_EVENT_PHOTO_NEEDED


async def show_photo_needed_selection(query, context: ContextTypes.DEFAULT_TYPE):
    """Показати вибір необхідності фото"""
    keyboard = [
        [
            InlineKeyboardButton("✅ Так", callback_data="photo_yes"),
            InlineKeyboardButton("❌ Ні", callback_data="photo_no")
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_procedure")],
        [InlineKeyboardButton("❌ Закрити", callback_data="close_admin_dialog")]
    ]

    await query.edit_message_text(
        "Чи потрібно кандидатам надавати фото зони?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_EVENT_PHOTO_NEEDED


async def create_event_photo_needed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка необхідності фото"""
    query = update.callback_query
    await answer_callback_query(query)

    # Якщо це повернення назад, просто показуємо вибір фото
    if query.data == "back_to_photo":
        return await show_photo_needed_selection(query, context)

    needs_photo = query.data == "photo_yes"
    context.user_data['event']['needs_photo'] = needs_photo
    return await show_comment_prompt(query, context)


async def show_comment_prompt(query, context: ContextTypes.DEFAULT_TYPE):
    """Показати запит на коментар до заходу"""
    event = context.user_data.setdefault('event', {})
    comment = event.get('comment')

    hint_lines = [
        "Додайте коментар до заходу (необов'язково).",
        "\nВи можете надіслати текст повідомлення або пропустити цей крок."
    ]
    if comment:
        hint_lines.append(f"\nПоточний коментар:\n{comment}")

    keyboard = [
        [InlineKeyboardButton("⏭ Пропустити", callback_data="skip_comment")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_photo")],
        [InlineKeyboardButton("❌ Закрити", callback_data="close_admin_dialog")]
    ]

    message = await query.edit_message_text(
        "\n".join(hint_lines),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    context.user_data['last_bot_message_id'] = message.message_id
    context.user_data['last_bot_chat_id'] = message.chat_id

    return CREATE_EVENT_COMMENT


async def skip_event_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пропустити додавання коментаря"""
    query = update.callback_query
    await answer_callback_query(query)
    context.user_data['event']['comment'] = None
    return await show_event_summary(update, context)


async def create_event_comment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробити введений коментар"""
    text = (update.message.text or "").strip()

    # Валідація довжини коментаря
    if text and len(text) > MAX_COMMENT_LENGTH:
        await send_admin_message_from_update(update, context,
            f"❌ Коментар занадто довгий. Максимум {MAX_COMMENT_LENGTH} символів.\n\n"
            "Введіть коментар заново або натисніть кнопку 'Пропустити':"
        )
        return CREATE_EVENT_COMMENT

    if text:
        context.user_data['event']['comment'] = text
    else:
        context.user_data['event']['comment'] = None

    try:
        await update.message.delete()
    except Exception:
        pass

    return await show_event_summary(update, context)


async def show_event_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показати підсумок заходу"""
    event = context.user_data['event']

    photo_required = "Обов'язкове" if event['needs_photo'] else "Не потрібне"
    date_display = format_date(event['date'])

    summary = (
        f"Підсумок заходу:\n\n"
        f"Дата: {date_display}\n"
        f"Час: {event['time']}\n"
        f"Процедура: {event['procedure']}\n"
        f"Фото від кандидатів: {photo_required}"
    )

    comment = event.get('comment')
    if comment:
        summary += f"\nКоментар: {comment}"

    keyboard = [
        [InlineKeyboardButton("➕ Додати до плану заходу", callback_data="add_event_to_day")],
        [InlineKeyboardButton("❌ Закрити", callback_data="close_admin_dialog")]
    ]

    # Використовуємо збережений message_id для редагування
    if update.callback_query:
        await update.callback_query.edit_message_text(
            summary,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        # Якщо це текстове повідомлення (коментар), редагуємо попереднє повідомлення бота
        if 'last_bot_message_id' in context.user_data and 'last_bot_chat_id' in context.user_data:
            await context.bot.edit_message_text(
                chat_id=context.user_data['last_bot_chat_id'],
                message_id=context.user_data['last_bot_message_id'],
                text=summary,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    context.user_data.pop('last_bot_message_id', None)
    context.user_data.pop('last_bot_chat_id', None)

    return CREATE_EVENT_CONFIRM


def build_schedule_overview(schedule: dict) -> str:
    """Створює текстовий опис запланованих процедур на день"""
    date_display = format_date(schedule['date']) if schedule.get('date') else "—"
    lines = [
        "Розклад дня:",
        "",
        f"Дата: {date_display}",
        ""
    ]

    events = schedule.get('events', [])

    if events:
        lines.append("Заплановані процедури:")
        for idx, item in enumerate(events, start=1):
            item_lines = [f"{idx}. {item['time']} — {item['procedure']}"]
            if item.get('needs_photo'):
                item_lines.append("   Потрібне фото зони")
            if item.get('comment'):
                item_lines.append(f"   Коментар: {item['comment']}")
            lines.extend(item_lines)
            lines.append("")
    else:
        lines.append("Поки що процедур немає.")
        lines.append("")

    lines.append("____________________________")
    lines.append("Оберіть подальшу дію:")

    # Видалити зайвий порожній рядок наприкінці (якщо є)
    if lines[-2] == "":
        lines.pop(-2)

    return "\n".join(lines)


async def show_schedule_overview(query, context: ContextTypes.DEFAULT_TYPE):
    """Показати поточний розклад дня"""
    schedule = context.user_data.get('schedule', {'events': []})
    text = build_schedule_overview(schedule)

    keyboard = [
        [InlineKeyboardButton("➕ Додати ще процедуру", callback_data="add_more_procedure")]
    ]

    if schedule.get('events'):
        keyboard.append([InlineKeyboardButton("✅ Опублікувати захід", callback_data="publish_schedule")])
        keyboard.append([InlineKeyboardButton("↩️ Видалити останню", callback_data="remove_last_procedure")])

    keyboard.append([InlineKeyboardButton("❌ Закрити", callback_data="close_admin_dialog")])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def add_event_to_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Додати сформовану процедуру до розкладу дня"""
    query = update.callback_query
    await answer_callback_query(query)

    event = context.user_data.get('event')
    if not event:
        await query.edit_message_text("Дані процедури не знайдені, спробуйте ще раз.")
        return ConversationHandler.END

    schedule = context.user_data.setdefault('schedule', {'date': event['date'], 'events': []})

    if schedule.get('date') and schedule['date'] != event['date']:
        schedule['events'].clear()
        schedule['date'] = event['date']
    elif not schedule.get('date'):
        schedule['date'] = event['date']

    schedule['events'].append({
        'date': event['date'],
        'time': event['time'],
        'procedure': event['procedure'],
        'needs_photo': event['needs_photo'],
        'comment': event.get('comment')

    })

    # Зберегти дату для наступної процедури, але очистити інші поля
    context.user_data['event'] = {'date': event['date']}

    await show_schedule_overview(query, context)
    return CREATE_EVENT_REVIEW


async def remove_last_procedure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Видалити останню додану процедуру"""
    query = update.callback_query
    schedule = context.user_data.get('schedule')

    if not schedule or not schedule.get('events'):
        await answer_callback_query(query, "Немає процедур для видалення", show_alert=True)
        return CREATE_EVENT_REVIEW

    schedule['events'].pop()
    await answer_callback_query(query, "Останню процедуру видалено")

    if schedule['events']:
        await show_schedule_overview(query, context)
        return CREATE_EVENT_REVIEW

    # Якщо все видалено, повертаємося до вибору часу
    context.user_data['event'] = {'date': schedule['date']}
    return await show_time_selection(query, context)


async def add_more_procedure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Почати додавання ще однієї процедури на той самий день"""
    query = update.callback_query
    await answer_callback_query(query)

    schedule = context.user_data.get('schedule')
    if not schedule or not schedule.get('date'):
        await query.edit_message_text("Дата не визначена, розпочніть спочатку.")
        return ConversationHandler.END

    context.user_data['event'] = {'date': schedule['date']}
    return await show_time_selection(query, context)


async def publish_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Опублікувати всі процедури дня в канал"""
    query = update.callback_query
    await answer_callback_query(query)

    schedule = context.user_data.get('schedule')
    if not schedule or not schedule.get('events'):
        await answer_callback_query(query, "Немає процедур для публікації", show_alert=True)
        return CREATE_EVENT_REVIEW

    try:
        logger.info(
            "Публікація розкладу: дата=%s, кількість процедур=%s",
            schedule.get('date'),
            len(schedule.get('events', []))
        )
        created_events = []
        for item in schedule['events']:
            event_id = db.create_event(
                date=item['date'],
                time=item['time'],
                procedure_type=item['procedure'],
                needs_photo=item['needs_photo'],
                comment=item.get('comment')
            )
            created_events.append((event_id, item))
        logger.debug(
            "Створені заходи: %s",
            created_events
        )

        created_events.sort(key=lambda pair: pair[1]['time'])
        await publish_day_schedule_to_channel(context, schedule['date'], created_events)
        await update_day_summary(context, schedule['date'])
        logger.info(
            "Розклад опубліковано: дата=%s, events=%s",
            schedule['date'],
            [item['procedure'] for _, item in created_events]
        )

        await query.edit_message_text("✅ Розклад успішно опубліковано в каналі.")

        keyboard = [[InlineKeyboardButton(
            "➕ Створити ще розклад на цю дату",
            callback_data=f"same_date_{schedule['date']}"
        )]]

        await clear_admin_dialog(context, 'admin_dialog')
        context.user_data.clear()
        await show_admin_menu(update, context)


    except Exception as e:
        logger.error(f"Помилка публікації розкладу: {e}", exc_info=True)
        await query.edit_message_text("Сталася помилка під час публікації. Спробуйте ще раз.")

    return ConversationHandler.END


async def create_event_same_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Створення ще одного заходу на ту саму дату"""
    query = update.callback_query
    await answer_callback_query(query)

    if not is_admin(query.from_user.id):
        await send_admin_message_from_query(query, context, "Немає доступу")
        return ConversationHandler.END

    # Отримати дату з callback_data
    date_str = query.data.split('_', 2)[2]  # same_date_2024-01-15 -> 2024-01-15

    # Видалити повідомлення з кнопкою
    await query.delete_message()

    # Ініціалізувати нові дані для заходу з попередньою датою
    await clear_admin_dialog(context)
    context.user_data.clear()
    context.user_data['event'] = {'date': date_str}
    context.user_data['schedule'] = {'date': date_str, 'events': []}

    # Показати вибір часу
    time_buttons = [InlineKeyboardButton(time, callback_data=f"time_{time}")
                    for time in TIME_SLOTS]
    keyboard = list(chunk_list(time_buttons, 6))
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel")])

    sent_msg = await send_admin_message_from_query(
        query,
        context,
        f"Дата: {format_date(date_str)}\n\nОберіть час заходу:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        auto_delete=False
    )
    await register_admin_dialog(context, 'admin_dialog', sent_msg)
    context.user_data['last_event_form_message'] = sent_msg.message_id

    return CREATE_EVENT_TIME


async def publish_day_schedule_to_channel(
    context: ContextTypes.DEFAULT_TYPE,
    date: str,
    created_events: list
):
    """Публікує розклад дня одним повідомленням з окремими кнопками для процедур"""
    bot_username = (await context.bot.get_me()).username

    logger.debug(
        "Публікація дня до групи: date=%s, events=%s",
        date,
        [(event_id, item['time'], item['procedure']) for event_id, item in created_events]
    )

    base_timestamp = int(time.time())

    schedule_events = []
    for event_id, item in created_events:
        schedule_events.append({
            'id': event_id,
            'time': item['time'],
            'procedure_type': item['procedure'],
            'comment': item.get('comment'),
            'needs_photo': item.get('needs_photo'),
            'status': item.get('status', 'published'),
            'date': date
        })

    message_text, keyboard = build_day_schedule_message(
        bot_username,
        date,
        schedule_events,
        timestamp=base_timestamp
    )

    message = await context.bot.send_message(
        chat_id=EVENTS_GROUP_ID,
        text=message_text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )

    logger.info(
        "Повідомлення в групу надіслано: group=%s, message_id=%s",
        EVENTS_GROUP_ID,
        getattr(message, "message_id", None)
    )

    for event_id, _ in created_events:
        db.update_event_message_id(event_id, message.message_id)
        db.update_event_status(event_id, 'published')
        logger.debug("Оновлено стан заходу: event_id=%s, message_id=%s", event_id, message.message_id)


def build_day_schedule_message(
    bot_username: str,
    date: str,
    events: List[dict],
    *,
    timestamp: Optional[int] = None
) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Побудувати текст і клавіатуру розкладу дня з урахуванням заповнених слотів"""
    formatted_date = format_date(date)
    weekday_acc = get_weekday_accusative(date)
    header = [
        "БЕЗКОШТОВНО!",
        f"На {weekday_acc} ({formatted_date}) потрібні моделі",
        ""
    ]

    sorted_events = sorted(events, key=lambda item: item['time'])
    event_lines: List[str] = []

    for item in sorted_events:
        procedure_name = html.escape(item['procedure_type'])
        line = f"{item['time']} — <b>{procedure_name}</b>"
        comment = item.get('comment')
        if comment:
            line += f" ({html.escape(comment)})"
        line += "."
        if item.get('needs_photo'):
            line += " Фото ОБОВ'ЯЗКОВО!"

        status = item.get('status', 'published')
        if status == 'filled':
            line = f"<s>{line}</s> МОДЕЛЬ ЗНАЙДЕНО!"
        elif status == 'cancelled':
            line = f"<s>{line}</s> (скасовано)"

        event_lines.append(line)
        event_lines.append("")

    if event_lines and event_lines[-1] == "":
        event_lines.pop()

    open_events = [e for e in sorted_events if e.get('status') == 'published']
    if open_events:
        payload_timestamp = timestamp or int(time.time())
        if len(open_events) == 1:
            deep_link = f"https://t.me/{bot_username}?start=event_{open_events[0]['id']}_{payload_timestamp}"
        else:
            payload = "_".join([str(payload_timestamp)] + [str(event['id']) for event in open_events])
            deep_link = f"https://t.me/{bot_username}?start=day_{payload}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Подати заявку", url=deep_link)]])
    else:
        keyboard = None

    return "\n".join(header + event_lines), keyboard


async def refresh_day_schedule_message(context: ContextTypes.DEFAULT_TYPE, date: str) -> None:
    """Оновити повідомлення в групі пошуку моделей після змін слотів"""
    events = db.get_events_by_date(date)
    if not events:
        return

    message_id = next((item.get('message_id') for item in events if item.get('message_id')), None)
    if not message_id:
        return

    bot_username = (await context.bot.get_me()).username
    message_text, keyboard = build_day_schedule_message(bot_username, date, events)

    try:
        await context.bot.edit_message_text(
            chat_id=EVENTS_GROUP_ID,
            message_id=message_id,
            text=message_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
    except BadRequest as err:
        error_msg = str(err).lower()
        if "message is not modified" in error_msg:
            return
        logger.debug("Не вдалося оновити повідомлення з розкладом: %s", err)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скасування діалогу"""
    query = update.callback_query
    user_id = update.effective_user.id

    if query:
        await answer_callback_query(query)
        await query.edit_message_text("Скасовано")
    else:
        await send_admin_message_from_update(update, context, "Скасовано")

    context.user_data.clear()

    # Показуємо головне меню
    if is_admin(user_id):
        if query:
            await show_admin_menu(update, context, edit_message=False)
        else:
            await send_admin_message_from_update(update, context, "Використовуйте /start для повернення в меню")
    else:
        # Для звичайних користувачів показуємо меню користувача
        if query:
            await show_user_menu(update, context, edit_message=False)
        else:
            await send_admin_message_from_update(update, context, "Використовуйте /start для повернення в меню")

    return ConversationHandler.END


# ==================== ПОДАЧА ЗАЯВКИ (МОДЕЛЬ) ====================

def build_multi_event_selection_text(events, selected_ids) -> str:
    """Створює текст для вибору кількох процедур"""
    if not events:
        return "Процедури недоступні. Спробуйте пізніше."

    selected_ids = set(selected_ids or [])
    lines = ["Оберіть одну або кілька процедур:", ""]

    for event in events:
        marker = "✅" if event['id'] in selected_ids else "▫️"
        photo_note = " (фото обов'язково)" if event.get('needs_photo') else ""
        line = (
            f"{marker} {format_date(event['date'])} {event['time']} — "
            f"{event['procedure_type']}{photo_note}"
        )
        lines.append(line)


    lines.append("")
    lines.append("Після вибору натисніть «Продовжити».")
    return "\n".join(lines)


def build_multi_event_selection_keyboard(events, selected_ids):
    """Створює клавіатуру для вибору кількох процедур"""
    selected_ids = set(selected_ids or [])
    keyboard = []

    for event in events:
        is_selected = event['id'] in selected_ids
        prefix = "✅" if is_selected else "⬜️"
        label = f"{prefix} {event['time']} · {event['procedure_type']}"
        keyboard.append([
            InlineKeyboardButton(label, callback_data=f"toggle_event_{event['id']}")
        ])

    actions_row = [InlineKeyboardButton("➡️ Продовжити", callback_data="event_selection_continue")]
    if selected_ids:
        actions_row.insert(0, InlineKeyboardButton("🔄 Скинути", callback_data="event_selection_reset"))

    keyboard.append(actions_row)
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel")])
    return keyboard


async def show_multi_event_selection(target, context: ContextTypes.DEFAULT_TYPE, replace: bool = False):
    """Показує (або оновлює) повідомлення з вибором процедур"""
    events = context.user_data.get('available_events', [])
    selected_ids = context.user_data.get('selected_event_ids', set())

    if not events:
        if replace:
            await target.edit_message_text("Список процедур недоступний. Спробуйте пізніше.")
        else:
            await target.reply_text("Список процедур недоступний. Спробуйте пізніше.")
        return ConversationHandler.END

    text = build_multi_event_selection_text(events, selected_ids)
    keyboard = InlineKeyboardMarkup(build_multi_event_selection_keyboard(events, selected_ids))

    if replace:
        await target.edit_message_text(text, reply_markup=keyboard)
    else:
        sent = await target.reply_text(text, reply_markup=keyboard)
        context.user_data['selection_message_id'] = sent.message_id
        context.user_data['selection_chat_id'] = sent.chat_id

    return APPLY_SELECT_EVENTS


async def toggle_event_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перемикає вибір конкретної процедури"""
    query = update.callback_query
    await answer_callback_query(query)

    event_id = int(query.data.split('_')[2])

    selected = context.user_data.get('selected_event_ids', set())
    if not isinstance(selected, set):
        selected = set(selected)

    if event_id in selected:
        selected.remove(event_id)
    else:
        selected.add(event_id)

    context.user_data['selected_event_ids'] = selected
    return await show_multi_event_selection(query, context, replace=True)


async def event_selection_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скидає вибір процедур"""
    query = update.callback_query
    await answer_callback_query(query)

    context.user_data['selected_event_ids'] = set()
    return await show_multi_event_selection(query, context, replace=True)


async def event_selection_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переходить до оформлення заявки після вибору процедур"""
    query = update.callback_query
    selected = context.user_data.get('selected_event_ids', set())

    if not isinstance(selected, set):
        selected = set(selected)

    if not selected:
        await answer_callback_query(query, "Оберіть хоча б одну процедуру.", show_alert=True)
        return APPLY_SELECT_EVENTS

    events = context.user_data.get('available_events', [])
    selected_events = [event for event in events if event['id'] in selected]

    if not selected_events:
        await answer_callback_query(query, "Обрані процедури стали недоступними. Спробуйте ще раз.", show_alert=True)
        return await show_multi_event_selection(query, context, replace=True)

    selected_events.sort(key=lambda item: (item['date'], item['time'], item['id']))
    context.user_data['apply_event_ids'] = [event['id'] for event in selected_events]

    # Очистити допоміжні дані
    context.user_data.pop('available_events', None)
    context.user_data.pop('selected_event_ids', None)
    context.user_data.pop('selection_message_id', None)
    context.user_data.pop('selection_chat_id', None)

    await answer_callback_query(query)
    await query.edit_message_text("Готуємо форму заявки…")

    return await apply_event_start(update, context)


# ---------------------- Допоміжні функції для фото ----------------------

PHOTO_INSTRUCTIONS_BASE = (
    "📸 Надішліть фото зони процедури\n\n"
    "Як прикріпити фото:\n"
    "1. Натисніть кнопку 📎 (скріпка) знизу\n"
    "2. Оберіть \"Галерея\" або \"Камера\"\n"
    "3. Виберіть фото зони процедури\n"
    f"4. Надішліть фото (до {MAX_APPLICATION_PHOTOS} шт.)\n\n"
    "Після завантаження всіх фото натисніть кнопку \"✅ Готово\""
)


def build_application_summary_text(app: dict) -> str:
    """Формує текст підсумку заявки"""
    events = app.get('events', [])
    event_lines = []

    for event in events:
        photo_note = " (фото обов'язково)" if event.get('needs_photo') else ""
        event_lines.append(
            f"- {event['procedure_type']} — {format_date(event['date'])} {event['time']}{photo_note}"
        )

    events_block = "\n".join(event_lines) if event_lines else "—"
    full_name = app.get('full_name') or "—"
    phone = app.get('phone') or "—"
    photos_count = len(app.get('photos', []))

    return (
        "Підсумок заявки:\n\n"
        f"Процедури:\n{events_block}\n\n"
        f"ПІБ: {full_name}\n"
        f"Телефон: {phone}\n"
        f"Фото додано: {photos_count}\n\n"
        "Підтверджую, що мені виповнилось 18 років"
    )


def build_application_summary_keyboard(can_go_back: bool) -> InlineKeyboardMarkup:
    """Побудувати клавіатуру для підтвердження заявки"""
    rows = [[InlineKeyboardButton("📤 Надіслати заявку", callback_data="submit_application")]]
    if can_go_back:
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_photos")])
    rows.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def build_photo_prompt_text(application: dict, count: int, mode: str) -> str:
    """Формує текст підказки або підсумку залежно від режиму"""
    if mode == 'summary':
        return build_application_summary_text(application)

    text = PHOTO_INSTRUCTIONS_BASE

    if application.get('multi_event'):
        text += "\n\nФото буде використане для всіх обраних процедур."

    text += f"\n\nЗавантажено фото: {count}/{MAX_APPLICATION_PHOTOS}"
    return text


def build_photo_prompt_keyboard(count: int, mode: str) -> InlineKeyboardMarkup:
    """Створює клавіатуру для етапу завантаження фото"""
    if mode == 'summary':
        return build_application_summary_keyboard(can_go_back=True)

    keyboard = [
        [InlineKeyboardButton("✅ Готово", callback_data="photos_done")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)


async def update_photo_prompt_message(
    context: ContextTypes.DEFAULT_TYPE,
    application: dict,
    *,
    chat_id: int,
    mode: Optional[str] = None,
    reply_to_message_id: Optional[int] = None
) -> None:
    """Оновлює текст повідомлення з інструкцією щодо фото"""
    prompt_info = context.user_data.get('photos_prompt')
    mode = mode or (prompt_info.get('mode') if prompt_info else 'instructions')

    if prompt_info:
        try:
            await context.bot.delete_message(
                chat_id=prompt_info['chat_id'],
                message_id=prompt_info['message_id']
            )
        except Exception as err:
            logger.debug(f"Не вдалося видалити попереднє фото-повідомлення: {err}")

    actual_chat_id = prompt_info['chat_id'] if prompt_info else chat_id
    if not actual_chat_id:
        logger.debug("chat_id для фото-повідомлення відсутній, пропускаю оновлення")
        return

    count = len(application.get('photos', []))
    logger.debug(
        "Оновлення повідомлення для фото: chat_id=%s, mode=%s, count=%s, reply_to=%s",
        actual_chat_id,
        mode,
        count,
        reply_to_message_id
    )
    send_kwargs = {
        "chat_id": actual_chat_id,
        "text": build_photo_prompt_text(application, count, mode),
        "reply_markup": build_photo_prompt_keyboard(count, mode)
    }

    if reply_to_message_id:
        send_kwargs["reply_to_message_id"] = reply_to_message_id

    try:
        new_message = await context.bot.send_message(**send_kwargs)
        context.user_data['photos_prompt'] = {
            'chat_id': new_message.chat_id,
            'message_id': new_message.message_id,
            'mode': mode
        }
        logger.debug(
            "Фото-повідомлення оновлено: chat_id=%s, message_id=%s, mode=%s",
            new_message.chat_id,
            new_message.message_id,
            mode
        )
    except Exception as err:
        logger.debug(f"Не вдалося надіслати фото-повідомлення: {err}")

async def apply_event_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Початок подачі заявки"""
    logger.info(f"apply_event_start() викликано, user_data: {context.user_data}")
    event_ids = context.user_data.get('apply_event_ids')

    if not event_ids:
        logger.error("Список заходів для заявки не знайдено в user_data")
        await update.effective_message.reply_text("Обрані заходи не знайдені або вже недоступні.")
        return ConversationHandler.END

    user_id = update.effective_user.id
    logger.info(f"Починаю обробку заявки для user_id={user_id}, events={event_ids}")

    # Перевірка блокування
    if db.is_user_blocked(user_id):
        logger.warning(f"Користувач {user_id} заблокований")
        await update.effective_message.reply_text("Ви заблоковані і не можете подавати заявки.")
        return ConversationHandler.END

    # Завантажити інформацію про заходи та перевірити їх доступність
    events = db.get_events_by_ids(event_ids)
    events_by_id = {event['id']: event for event in events if event['status'] == 'published'}
    ordered_events = [events_by_id[event_id] for event_id in event_ids if event_id in events_by_id]

    if not ordered_events:
        logger.warning(f"Жоден із заходів {event_ids} недоступний")
        await update.effective_message.reply_text("На жаль, вибрані процедури вже недоступні.")
        return ConversationHandler.END

    missing_count = len(event_ids) - len(ordered_events)
    if missing_count > 0:
        logger.info(f"{missing_count} з обраних процедур стали недоступними під час оформлення")
        await update.effective_message.reply_text(
            "Деякі з обраних процедур вже недоступні, тому вони були вилучені із заявки."
        )

    duplicate_events: List[Dict] = []
    filtered_events: List[Dict] = []
    for event in ordered_events:
        if db.user_has_application_for_event(user_id, event['id']):
            duplicate_events.append(event)
        else:
            filtered_events.append(event)

    if not filtered_events:
        logger.info(
            "Всі обрані процедури вже мають заявки користувача %s: %s",
            user_id,
            [event['id'] for event in duplicate_events]
        )
        lines = ["ℹ️ Нова заявка не створена.", ""]
        lines.append("Ви вже маєте заявки на такі процедури:")
        for event in duplicate_events:
            lines.append(
                f"- {event['procedure_type']} — {format_date(event['date'])} {event['time']}"
            )
        lines.append("")
        lines.append("Повторна подача заявки на ту саму процедуру недоступна.")

        context.user_data.clear()

        await update.effective_message.reply_text(
            "\n".join(lines),
            reply_markup=get_user_keyboard()
        )
        return ConversationHandler.END

    ordered_events = filtered_events

    context.user_data['apply_event_ids'] = [event['id'] for event in ordered_events]
    context.user_data['application'] = {
        'event_ids': [event['id'] for event in ordered_events],
        'events': ordered_events,
        'photos': [],
        'needs_photo': any(event.get('needs_photo') for event in ordered_events),
        'multi_event': len(ordered_events) > 1
    }

    # Відображення короткого підсумку вибраних процедур
    summary_lines = ["Ви обрали:"]
    for event in ordered_events:
        photo_note = " (фото обов'язково)" if event.get('needs_photo') else ""
        summary_lines.append(
            f"• {format_date(event['date'])} {event['time']} — {event['procedure_type']}{photo_note}"
        )

    if duplicate_events:
        summary_lines.append("")
        summary_lines.append("Ви вже маєте заявки на такі процедури, тому їх пропущено:")
        for event in duplicate_events:
            summary_lines.append(
                f"• {format_date(event['date'])} {event['time']} — {event['procedure_type']}"
            )

    summary_text = "\n".join(summary_lines)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(summary_text)
        except Exception as edit_error:
            logger.debug(f"Не вдалося відредагувати повідомлення із вибором процедур: {edit_error}")
            await update.effective_message.reply_text(summary_text)
    else:
        await update.effective_message.reply_text(summary_text)

    # Перевірка чи є збережені дані користувача
    user = db.get_user(user_id)

    if user and user['full_name'] and user['phone']:
        logger.info(f"Користувач {user_id} має збережені дані")
        keyboard = [
            [
                InlineKeyboardButton("✅ Так", callback_data="use_saved_data"),
                InlineKeyboardButton("✏️ Ввести нові", callback_data="enter_new_data")
            ],
            [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
        ]

        await update.effective_message.reply_text(
            f"У нас є ваші дані:\n\n"
            f"ПІБ: {user['full_name']}\n"
            f"Телефон: {user['phone']}\n\n"
            f"Використати ці дані?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.info("Показано запит на використання збережених даних")
        return APPLY_FULL_NAME

    logger.info(f"Користувач {user_id} не має збережених даних")
    await update.effective_message.reply_text(
        "Введіть ваше повне ім'я (Прізвище Ім'я По батькові):"
    )
    logger.info("Показано запит на введення ПІБ")
    return APPLY_FULL_NAME


async def apply_use_saved_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Використання збережених даних"""
    query = update.callback_query
    await answer_callback_query(query)

    # Перевірка наявності даних заявки
    if 'application' not in context.user_data:
        await send_admin_message_from_query(query, context, 
            "⚠️ Дані заявки втрачено (можливо, бот було перезапущено).\n\n"
            "Будь ласка, почніть процес заново командою /start"
        )
        return ConversationHandler.END

    user = db.get_user(update.effective_user.id)
    context.user_data['application']['full_name'] = user['full_name']
    context.user_data['application']['phone'] = user['phone']

    await query.delete_message()

    application = context.user_data['application']
    needs_photo = application.get('needs_photo', False)

    if needs_photo:
        count = len(application.get('photos', []))
        prompt_text = build_photo_prompt_text(application, count, mode='instructions')
        prompt_keyboard = build_photo_prompt_keyboard(count, mode='instructions')
        message = await send_admin_message_from_query(query, context, prompt_text, reply_markup=prompt_keyboard)
        context.user_data['photos_prompt'] = {
            'chat_id': message.chat_id,
            'message_id': message.message_id,
            'mode': 'instructions'
        }
        return APPLY_PHOTOS

    return await show_application_summary(query.message, context)


async def apply_enter_new_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввести нові дані"""
    query = update.callback_query
    await answer_callback_query(query)

    await query.edit_message_text("Введіть ваше повне ім'я (Прізвище Ім'я По батькові):")
    return APPLY_FULL_NAME


async def apply_full_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка ПІБ"""
    # Перевірка наявності даних заявки (можуть бути втрачені при перезапуску бота)
    if 'application' not in context.user_data:
        await send_admin_message_from_update(update, context,
            "⚠️ Дані заявки втрачено (можливо, бот було перезапущено).\n\n"
            "Будь ласка, почніть процес заново командою /start"
        )
        return ConversationHandler.END

    full_name = update.message.text.strip()

    # Валідація довжини ПІБ
    if len(full_name) > MAX_FULL_NAME_LENGTH:
        await send_admin_message_from_update(update, context,
            f"❌ ПІБ занадто довге. Максимум {MAX_FULL_NAME_LENGTH} символів.\n\n"
            "Введіть ваше повне ім'я (Прізвище Ім'я По батькові):"
        )
        return APPLY_FULL_NAME

    if len(full_name) < 3:
        await send_admin_message_from_update(update, context,
            "❌ ПІБ занадто коротке. Введіть коректне ПІБ (мінімум 3 символи):"
        )
        return APPLY_FULL_NAME

    context.user_data['application']['full_name'] = full_name
    await send_admin_message_from_update(update, context, "ПІБ збережено")

    # Клавіатура з кнопкою для надсилання контакту
    keyboard = [
        [KeyboardButton("📱 Надіслати мій номер", request_contact=True)],
        [KeyboardButton("✍️ Ввести номер вручну")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

    await send_admin_message_from_update(update, context, 
        "Введіть ваш номер телефону або натисніть кнопку нижче:",
        reply_markup=reply_markup
    )
    return APPLY_PHONE


def validate_ukrainian_phone(phone: str) -> bool:
    """Перевірка українського номера телефону"""
    # Очистити номер від зайвих символів
    cleaned = re.sub(r'[\s\-\(\)]', '', phone)

    # Патерни для українських номерів
    patterns = [
        r'^(\+380|380|0)(39|50|63|66|67|68|73|91|92|93|94|95|96|97|98|99)\d{7}$',
    ]

    for pattern in patterns:
        if re.match(pattern, cleaned):
            return True

    return False


async def apply_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка телефону"""
    # Перевірка наявності даних заявки (можуть бути втрачені при перезапуску бота)
    if 'application' not in context.user_data:
        await send_admin_message_from_update(update, context, 
            "⚠️ Дані заявки втрачено (можливо, бот було перезапущено).\n\n"
            "Будь ласка, почніть процес заново командою /start",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    # Обробка контакту (якщо користувач натиснув кнопку "Надіслати мій номер")
    if update.message.contact:
        phone = update.message.contact.phone_number
        # Якщо номер не починається з +, додаємо +
        if not phone.startswith('+'):
            phone = '+' + phone
    # Обробка тексту "✍️ Ввести номер вручну" - повторно показуємо інструкцію
    elif update.message.text == "✍️ Ввести номер вручну":
        await send_admin_message_from_update(update, context, 
            "Введіть ваш номер телефону:\n\n"
            "Приклади правильного формату:\n"
            "+380501234567\n"
            "0501234567\n"
            "050 123 45 67",
            reply_markup=ReplyKeyboardRemove()
        )
        return APPLY_PHONE
    # Обробка текстового номера
    else:
        phone = update.message.text

        # Перевірка українського номера
        if not validate_ukrainian_phone(phone):
            await send_admin_message_from_update(update, context, 
                "Невірний формат телефону.\n\n"
                "Приклади правильного формату:\n"
                "+380501234567\n"
                "0501234567\n"
                "050 123 45 67\n\n"
                "Введіть номер українського оператора:",
                reply_markup=ReplyKeyboardRemove()
            )
            return APPLY_PHONE

    context.user_data['application']['phone'] = phone
    await send_admin_message_from_update(update, context, "Телефон збережено", reply_markup=ReplyKeyboardRemove())

    # Зберегти дані користувача
    db.update_user(
        update.effective_user.id,
        context.user_data['application']['full_name'],
        phone
    )

    application = context.user_data['application']
    needs_photo = application.get('needs_photo', False)

    if needs_photo:
        count = len(application.get('photos', []))
        prompt_text = build_photo_prompt_text(application, count, mode='instructions')
        prompt_keyboard = build_photo_prompt_keyboard(count, mode='instructions')
        message = await send_admin_message_from_update(update, context, prompt_text, reply_markup=prompt_keyboard)
        context.user_data['photos_prompt'] = {
            'chat_id': message.chat_id,
            'message_id': message.message_id,
            'mode': 'instructions'
        }
        return APPLY_PHOTOS

    return await show_application_summary(update.message, context)


async def apply_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка фото від моделі"""
    if 'application' not in context.user_data:
        await send_admin_message_from_update(update, context, "Сесія застаріла. Будь ласка, почніть заново з посилання в каналі.")
        return ConversationHandler.END

    application = context.user_data['application']
    photos = application.get('photos', [])

    if len(photos) >= MAX_APPLICATION_PHOTOS:
        # Спочатку показуємо попередження
        if not application.get('photo_warning_sent'):
            await update.message.reply_text(
                f"⚠️ Можна надіслати не більше {MAX_APPLICATION_PHOTOS} фото.\n\n"
                f"Перші {MAX_APPLICATION_PHOTOS} вже збережено, решту ігноруємо.\n\n"
                "Натисніть «📤 Надіслати заявку» для підтвердження."
            )
            application['photo_warning_sent'] = True
            application['extra_photos_ignored'] = True
            logger.debug(
                "Перевищено ліміт фото: user=%s, total=%s",
                update.effective_user.id if update.effective_user else None,
                len(photos)
            )

            # А вже потім оновлюємо повідомлення з кнопками
            await update_photo_prompt_message(
                context,
                application,
                chat_id=update.effective_chat.id,
                mode='summary',
                reply_to_message_id=None  # Не прив'язуємо до фото-повідомлення
            )
        return APPLY_PHOTOS

    file_id = update.message.photo[-1].file_id
    photos.append(file_id)
    application['photos'] = photos
    logger.debug(
        "Отримано фото від користувача: user=%s, total=%s",
        update.effective_user.id if update.effective_user else None,
        len(photos)
    )

    await update_photo_prompt_message(
        context,
        application,
        chat_id=update.effective_chat.id,
        mode='summary',
        reply_to_message_id=update.message.message_id
    )

    application.pop('photo_warning_sent', None)
    application.pop('extra_photos_ignored', None)

    return APPLY_PHOTOS


async def apply_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершення додавання фото"""
    query = update.callback_query

    # Перевірка наявності даних заявки
    if 'application' not in context.user_data:
        await answer_callback_query(query, "Дані заявки втрачено. Спробуйте почати заново.", show_alert=True)
        await send_admin_message_from_query(query, context, 
            "⚠️ Дані заявки втрачено (можливо, бот було перезапущено).\n\n"
            "Будь ласка, почніть процес заново командою /start"
        )
        return ConversationHandler.END

    application = context.user_data['application']
    photos = application.get('photos', [])

    extra_removed = False
    if len(photos) > MAX_APPLICATION_PHOTOS:
        application['photos'] = photos[:MAX_APPLICATION_PHOTOS]
        photos = application['photos']
        extra_removed = True

    if application.get('needs_photo') and len(photos) == 0:
        await answer_callback_query(query, "Фото є обов'язковим. Додайте хоча б одне фото.", show_alert=True)
        return APPLY_PHOTOS

    if application.get('extra_photos_ignored') or extra_removed:
        await answer_callback_query(query, f"Збережено {len(photos)} фото. Зайві зображення проігноровано.", show_alert=True)
    else:
        await answer_callback_query(query)

    application.pop('photo_warning_sent', None)
    application.pop('extra_photos_ignored', None)

    logger.debug(
        "Завершення додавання фото: user=%s, total=%s, extra_removed=%s",
        query.from_user.id if query and query.from_user else None,
        len(photos),
        extra_removed
    )

    await update_photo_prompt_message(
        context,
        application,
        chat_id=query.message.chat_id,
        mode='summary'
    )

    return APPLY_CONFIRM


async def back_to_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повернення до етапу завантаження фото"""
    query = update.callback_query
    await answer_callback_query(query)

    application = context.user_data.get('application')
    if not application:
        await send_admin_message_from_query(query, context, 
            "⚠️ Дані заявки втрачено. Будь ласка, почніть процес заново.")
        return ConversationHandler.END

    await update_photo_prompt_message(
        context,
        application,
        chat_id=query.message.chat_id,
        mode='instructions'
    )

    return APPLY_PHOTOS


async def show_application_summary(message, context: ContextTypes.DEFAULT_TYPE):
    """Показати підсумок заявки зі згодою"""
    app = context.user_data['application']

    events = app.get('events', [])
    chat_id = message.chat_id

    if not events:
        await context.bot.send_message(chat_id=chat_id, text="Обрані процедури не знайдені. Спробуйте почати заявку заново.")
        context.user_data.clear()
        return ConversationHandler.END

    summary_text = build_application_summary_text(app)
    keyboard = build_application_summary_keyboard(can_go_back=app.get('needs_photo', False))

    await context.bot.send_message(chat_id=chat_id, text=summary_text, reply_markup=keyboard)

    return APPLY_CONFIRM


async def submit_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Відправити заявку"""
    query = update.callback_query
    await answer_callback_query(query)

    app = context.user_data.get('application')

    if not app:
        # Видалити попереднє повідомлення та надіслати нове з клавіатурою
        try:
            await query.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Дані заявки втрачено. Будь ласка, почніть процес заново.",
            reply_markup=get_user_keyboard()
        )
        return ConversationHandler.END

    # Перевірка кількості активних заявок користувача
    user_id = update.effective_user.id
    active_count = db.count_user_active_applications(user_id)

    if active_count >= MAX_ACTIVE_APPLICATIONS_PER_USER:
        # Видалити попереднє повідомлення та надіслати нове з клавіатурою
        try:
            await query.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"⚠️ Ви маєте забагато активних заявок ({active_count}/{MAX_ACTIVE_APPLICATIONS_PER_USER}).\n\n"
                "Дочекайтесь розгляду попередніх заявок перед тим, як подавати нові.\n\n"
                "Активні заявки: ті, що мають статус 'Очікує', 'Резерв' або 'Основний'."
            ),
            reply_markup=get_user_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    try:
        selected_event_ids = app.get('event_ids', [])
        if not selected_event_ids:
            # Видалити попереднє повідомлення та надіслати нове з клавіатурою
            try:
                await query.message.delete()
            except Exception:
                pass

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Обрані процедури не знайдені. Спробуйте почати знову.",
                reply_markup=get_user_keyboard()
            )
            return ConversationHandler.END

        stored_events = {event['id']: event for event in app.get('events', [])}
        valid_events = []
        unavailable_events = []

        for event_id in selected_event_ids:
            event = db.get_event(event_id)
            if not event or event['status'] != 'published':
                unavailable_events.append(stored_events.get(event_id, {'id': event_id}))
                continue
            valid_events.append(event)

        if not valid_events:
            # Видалити попереднє повідомлення та надіслати нове з клавіатурою
            try:
                await query.message.delete()
            except Exception:
                pass

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="На жаль, жодна з обраних процедур вже не приймає заявки. Спробуйте обрати інші дати.",
                reply_markup=get_user_keyboard()
            )
            context.user_data.clear()
            return ConversationHandler.END

        application_results = []
        events_for_update: Dict[int, str] = {}
        submitted_events: List[Dict] = []
        already_applied_events: List[Dict] = []
        for event in valid_events:
            if db.user_has_application_for_event(update.effective_user.id, event['id']):
                already_applied_events.append(event)
                continue

            application_id = db.create_application(
                event_id=event['id'],
                user_id=update.effective_user.id,
                full_name=app['full_name'],
                phone=app['phone']
            )

            for file_id in app.get('photos', []):
                db.add_application_photo(application_id, file_id)

            application_results.append((application_id, event))
            events_for_update[event['id']] = event['date']
            submitted_events.append(event)

        for event_id in events_for_update.keys():
            db.recalculate_application_positions(event_id)

        # Повідомлення користувачу
        if not application_results:
            lines = ["ℹ️ Нова заявка не створена.", ""]

            if already_applied_events:
                lines.append("Ви вже маєте заявки на такі процедури:")
                for event in already_applied_events:
                    lines.append(
                        f"- {event['procedure_type']} — {format_date(event['date'])} {event['time']}"
                    )

            if unavailable_events:
                if lines and lines[-1] != "":
                    lines.append("")
                lines.append("Ці процедури зараз недоступні для подачі заявки:")
                for event in unavailable_events:
                    if event:
                        date_part = format_date(event['date']) if event.get('date') else "—"
                        time_part = event.get('time', "—")
                        procedure = event.get('procedure_type', f"ID {event.get('id')}")
                        lines.append(f"- {procedure} — {date_part} {time_part}")

            if lines and lines[-1] != "":
                lines.append("")
            lines.append("Якщо потрібно оновити дані, зверніться до адміністратора.")
        else:
            lines = ["✅ Вашу заявку успішно подано!", ""]

            if len(submitted_events) == 1:
                event = submitted_events[0]
                lines.extend([
                    f"📋 Процедура: {event['procedure_type']}",
                    f"📅 Дата: {format_date(event['date'])}",
                    f"🕐 Час: {event['time']}"
                ])
            else:
                lines.append("Процедури:")
                for event in submitted_events:
                    lines.append(f"- {event['procedure_type']} — {format_date(event['date'])} {event['time']}")

            if already_applied_events:
                lines.append("")
                lines.append("Ви вже маєте заявки на такі процедури:")
                for event in already_applied_events:
                    lines.append(
                        f"- {event['procedure_type']} — {format_date(event['date'])} {event['time']}"
                    )

            if unavailable_events:
                lines.append("")
                lines.append("Не вдалося подати заявку на такі процедури:")
                for event in unavailable_events:
                    if event:
                        date_part = format_date(event['date']) if event.get('date') else "—"
                        time_part = event.get('time', "—")
                        procedure = event.get('procedure_type', f"ID {event.get('id')}")
                        lines.append(f"- {procedure} — {date_part} {time_part}")

            lines.append("")
            lines.append("Очікуйте на розгляд адміністратором.")

        # Видалити попереднє повідомлення та надіслати нове з клавіатурою
        try:
            await query.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="\n".join(lines),
            reply_markup=get_user_keyboard()
        )

        # Опублікувати заявку/заявки в групу
        if application_results:
            if len(application_results) == 1:
                await publish_application_to_channel(context, application_results[0][0])
            else:
                candidate_info = {
                    'full_name': app['full_name'],
                    'phone': app['phone'],
                    'user_id': update.effective_user.id
                }
                await publish_group_application_to_channel(
                    context,
                    application_results,
                    candidate_info,
                    app.get('photos', [])
                )

        for event_id, event_date in events_for_update.items():
            await update_day_summary(context, event_date)

        # Відправка email-повідомлення адміністраторам
        if EMAIL_ENABLED and submitted_events:
            email_subject = f"Нова заявка від {app['full_name']}"
            email_lines = [
                f"Отримано нову заявку!",
                "",
                f"👤 Ім'я: {app['full_name']}",
                f"📞 Телефон: {app['phone']}",
                f"🆔 User ID: {update.effective_user.id}",
                ""
            ]

            if len(submitted_events) == 1:
                event = submitted_events[0]
                email_lines.extend([
                    f"📋 Процедура: {event['procedure_type']}",
                    f"📅 Дата: {format_date(event['date'])}",
                    f"🕐 Час: {event['time']}"
                ])
            else:
                email_lines.append("Процедури:")
                for event in submitted_events:
                    email_lines.append(f"  • {event['procedure_type']} — {format_date(event['date'])} {event['time']}")

            await send_email_notification(email_subject, "\n".join(email_lines))

    except Exception as e:
        logger.error(f"Помилка подачі заявки: {e}", exc_info=True)

        # Видалити попереднє повідомлення та надіслати нове з клавіатурою
        try:
            await query.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Помилка при подачі заявки. Спробуйте ще раз або зверніться до адміністратора.",
            reply_markup=get_user_keyboard()
        )

    context.user_data.clear()
    return ConversationHandler.END


async def publish_application_to_channel(context: ContextTypes.DEFAULT_TYPE, application_id: int):
    """Публікація заявки в канал із заявками"""
    global APPLICATIONS_CHANNEL_ID
    app = db.get_application(application_id)
    event = db.get_event(app['event_id'])
    photos = db.get_application_photos(application_id)
    channel_id = context.bot_data.get('applications_channel_id', APPLICATIONS_CHANNEL_ID)
    if isinstance(channel_id, str):
        try:
            channel_id = int(channel_id)
        except ValueError:
            channel_id = int(APPLICATIONS_CHANNEL_ID)

    # Формат як у групових заявках
    status_icon = format_application_status(app['status'], app.get('is_primary', False))
    # Екранувати user input для безпеки
    safe_name = safe_html(app['full_name'])
    safe_phone = safe_html(app['phone'])
    safe_procedure = safe_html(event['procedure_type'])

    message_text = (
        f"Нова заявка від {safe_name}\n"
        f"Телефон: {safe_phone}\n"
        f"ID користувача: {app['user_id']}\n\n"
        f"Обрана процедура:\n"
        f"1. (№{application_id}) {format_date(event['date'])} {event['time']} — {safe_procedure} {status_icon}"
    )

    keyboard = build_single_application_keyboard(app, event)
    fallback_keyboard = remove_profile_button(keyboard)

    async def send_single_message(markup: InlineKeyboardMarkup):
        return await context.bot.send_message(
            chat_id=channel_id,
            text=message_text,
            reply_markup=markup
        )

    async def send_single_photo(markup: InlineKeyboardMarkup):
        return await context.bot.send_photo(
            chat_id=channel_id,
            photo=photos[0],
            caption=message_text,
            reply_markup=markup
        )

    if photos:
        if len(photos) == 1:
            try:
                message = await send_single_photo(keyboard)
            except ChatMigrated as e:
                new_id = e.new_chat_id
                context.bot_data['applications_channel_id'] = new_id
                APPLICATIONS_CHANNEL_ID = new_id
                return await publish_application_to_channel(context, application_id)
            except BadRequest as err:
                if "Button_user_privacy_restricted" in str(err):
                    logger.warning(
                        "Публікація заявки без кнопки профілю (privacy): application_id=%s, user_id=%s",
                        application_id,
                        app['user_id']
                    )
                    try:
                        message = await send_single_photo(fallback_keyboard)
                    except Exception as retry_err:
                        logger.error(
                            "Не вдалося опублікувати заявку навіть без профілю: application_id=%s, err=%s",
                            application_id,
                            retry_err
                        )
                        return
                else:
                    raise
        else:
            # Для медіагрупи прибираємо підпис, щоб уникнути дублювання тексту:
            # інформація буде в окремому повідомленні з клавіатурою.
            media = [InputMediaPhoto(media=photo_id, caption='') for photo_id in photos]
            try:
                messages = await context.bot.send_media_group(chat_id=channel_id, media=media)
                message = await send_single_message(keyboard)
            except ChatMigrated as e:
                new_id = e.new_chat_id
                context.bot_data['applications_channel_id'] = new_id
                APPLICATIONS_CHANNEL_ID = new_id
                return await publish_application_to_channel(context, application_id)
            except BadRequest as err:
                if "Button_user_privacy_restricted" in str(err):
                    logger.warning(
                        "Публікація заявки без кнопки профілю (media, privacy): application_id=%s, user_id=%s",
                        application_id,
                        app['user_id']
                    )
                    try:
                        message = await send_single_message(fallback_keyboard)
                    except Exception as retry_err:
                        logger.error(
                            "Не вдалося опублікувати заявку навіть без профілю (media): application_id=%s, err=%s",
                            application_id,
                            retry_err
                        )
                        return
                else:
                    raise
    else:
        try:
            message = await send_single_message(keyboard)
        except ChatMigrated as e:
            new_id = e.new_chat_id
            context.bot_data['applications_channel_id'] = new_id
            APPLICATIONS_CHANNEL_ID = new_id
            return await publish_application_to_channel(context, application_id)
        except BadRequest as err:
            if "Button_user_privacy_restricted" in str(err):
                logger.warning(
                    "Публікація заявки без кнопки профілю (no photo, privacy): application_id=%s, user_id=%s",
                    application_id,
                    app['user_id']
                )
                try:
                    message = await send_single_message(fallback_keyboard)
                except Exception as retry_err:
                    logger.error(
                        "Не вдалося опублікувати заявку навіть без профілю (no photo): application_id=%s, err=%s",
                        application_id,
                        retry_err
                    )
                    return
            else:
                logger.error(f"Не вдалося опублікувати заявку в канал: {err}")
                return
        except Exception as err:
            logger.error(f"Не вдалося опублікувати заявку в канал: {err}")
            return

    db.update_application_group_message_id(application_id, message.message_id)
    await update_day_summary(context, event['date'])


def format_application_status(status: str, is_primary: bool = False) -> str:
    """Повернути текстовий статус заявки з піктограмою"""
    if is_primary or status == 'primary':
        return APPLICATION_STATUS_LABELS['primary']
    return APPLICATION_STATUS_LABELS.get(status, APPLICATION_STATUS_LABELS['pending'])


def get_rejected_procedures_for_related_applications(application: dict, event: dict) -> List[str]:
    """Отримати перелік відхилених процедур серед пов'язаних заявок користувача."""
    related_applications: List[dict] = []
    group_message_id = application.get('group_message_id')
    if group_message_id:
        related_applications = db.get_applications_by_group_message(group_message_id)
    else:
        related_applications = db.get_user_applications_for_date(application['user_id'], event['date'])

    rejected_procedures: List[str] = []
    for app in related_applications:
        if app.get('status') != 'rejected':
            continue
        procedure = (app.get('procedure_type') or '').strip()
        if procedure and procedure not in rejected_procedures:
            rejected_procedures.append(procedure)

    return rejected_procedures


def get_related_applications_for_review(application: dict, event: Optional[dict]) -> List[dict]:
    """Отримати пов'язані заявки для формування підсумкового результату."""
    group_message_id = application.get('group_message_id')
    if group_message_id:
        related = db.get_applications_by_group_message(group_message_id)
        if related:
            return related

    app_with_event = db.get_application_with_event(application['id'])
    return [app_with_event] if app_with_event else []


def build_final_review_notification_text(related_applications: List[dict]) -> Optional[str]:
    """Побудувати підсумковий текст після розгляду всіх пов'язаних заявок."""
    if not related_applications:
        return None

    if any(app.get('status') == 'pending' for app in related_applications):
        return None

    approved_items: List[str] = []
    rejected_items: List[str] = []

    for app in related_applications:
        procedure = (app.get('procedure_type') or '').strip()
        event_time = (app.get('time') or '').strip()
        if not procedure:
            continue

        item_text = f"{event_time} — {procedure}" if event_time else procedure
        if app.get('status') in ('approved', 'primary'):
            if item_text not in approved_items:
                approved_items.append(item_text)
        elif app.get('status') == 'rejected':
            if item_text not in rejected_items:
                rejected_items.append(item_text)

    if not approved_items and not rejected_items:
        return None

    lines: List[str] = ["Вітаємо! Дякуємо за вашу заявку."]

    if approved_items:
        lines.append("")
        lines.append("✅ <b>Підтверджено запис на наступні процедури:</b>")
        lines.extend([f"• {html.escape(item)}" for item in approved_items])

    if rejected_items:
        lines.append("")
        lines.append("❌ <b>На жаль, ми не можемо підтвердити запис на наступні процедури:</b>")
        lines.extend([f"• {html.escape(item)}" for item in rejected_items])
        lines.append(
            "Оскільки ми працюємо з великою кількістю різного обладнання, "
            "вибір моделей здійснюється за певними технічними принципами під конкретну методику дня."
        )
        lines.append("Ми будемо щиро раді розглянути вашу кандидатуру наступного разу!")

    if approved_items:
        lines.append("")
        lines.append("Інструкції:")
        lines.append("• Будь ласка, прийдіть за 15 хвилин до початку")
        lines.append("• Майте при собі документ, що підтверджує особу")
        lines.append("• У разі неможливості прийти - повідомте нас заздалегідь")
        lines.append("")
        lines.append("Адреса: Оболонський проспект, 28, Medicalaser")
        lines.append("https://maps.app.goo.gl/2spzGUtcyYP38bHh8?g_st=ic")
        lines.append("Контакти адміністратора: +380962201240 (Вікторія)")
        lines.append("До зустрічі!")

    return "\n".join(lines)


async def maybe_send_final_review_notification(
    context: ContextTypes.DEFAULT_TYPE,
    application: dict,
    event: Optional[dict]
) -> bool:
    """Надіслати одне підсумкове повідомлення користувачу після завершення розгляду заявок."""
    related_applications = get_related_applications_for_review(application, event)
    text = build_final_review_notification_text(related_applications)
    if not text:
        return False

    try:
        await context.bot.send_message(
            chat_id=application['user_id'],
            text=text,
            reply_markup=get_user_keyboard(),
            parse_mode=ParseMode.HTML
        )

        has_approved = any(app.get('status') in ('approved', 'primary') for app in related_applications)
        if has_approved:
            video_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "find.mp4")
            if os.path.exists(video_path):
                try:
                    with open(video_path, "rb") as video_file:
                        await context.bot.send_video(
                            chat_id=application['user_id'],
                            video=video_file,
                            caption="На відео показано, як нас знайти! Інструкція щодо візиту вище"
                        )
                except Exception as video_err:
                    logger.error(
                        "Не вдалося надіслати відеоінструкцію у підсумковому повідомленні: user_id=%s, err=%s",
                        application.get('user_id'),
                        video_err
                    )
            else:
                logger.warning("Відеоінструкцію не надіслано: файл find.mp4 не знайдено поруч з bot.py")

        return True
    except Exception as err:
        logger.debug(
            "Не вдалося надіслати підсумкове повідомлення користувачу: user_id=%s, err=%s",
            application.get('user_id'),
            err
        )
        return False


def build_group_application_text(applications: list, candidate: dict) -> str:
    """Побудувати текст групової заявки"""
    # Екранувати всі user input для безпеки
    safe_name = safe_html(candidate['full_name'])
    safe_phone = safe_html(candidate['phone'])

    lines = [
        f"Нова заявка від {safe_name}",
        f"Телефон: {safe_phone}",
        f"ID користувача: {candidate['user_id']}",
        "",
        "Обрані процедури:"
    ]

    for idx, item in enumerate(applications, start=1):
        event = item['event']
        app_id = item['id']
        status_icon = format_application_status(item['status'], item.get('is_primary', False))
        photo_note = " (фото обов'язково)" if event.get('needs_photo') else ""
        safe_procedure = safe_html(event['procedure_type'])
        lines.append(
            f"{idx}. (№{app_id}) {format_date(event['date'])} {event['time']} — {safe_procedure}{photo_note} {status_icon}"
        )

    return "\n".join(lines)


def build_group_application_keyboard(applications: list, candidate: dict) -> InlineKeyboardMarkup:
    """Зібрати клавіатуру для групової заявки"""
    rows = []

    for item in applications:
        application_id = item['id']
        event = item['event']
        label = f"{event['time']} · {event['procedure_type']}"
        status = item['status']

        row = [
            InlineKeyboardButton(
                label,
                callback_data="noop",
                switch_inline_query_current_chat=f"Процедура: {event['procedure_type']} ({event['time']})"
            )
        ]

        if status == 'pending':
            row.append(InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{application_id}"))
            row.append(InlineKeyboardButton("❌", callback_data=f"reject_{application_id}"))
        elif status == 'approved':
            row.append(InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{application_id}"))
            row.append(InlineKeyboardButton("❌", callback_data=f"reject_{application_id}"))
        elif status == 'primary':
            row.append(InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{application_id}"))
            row.append(InlineKeyboardButton("❌", callback_data=f"reject_{application_id}"))
        elif status in ('rejected', 'cancelled'):
            row.append(InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{application_id}"))
            row.append(InlineKeyboardButton("❌", callback_data=f"reject_{application_id}"))

        rows.append(row)

    rows.append([InlineKeyboardButton("👤 Профіль кандидата", url=f"tg://user?id={candidate['user_id']}")])

    return InlineKeyboardMarkup(rows)


def build_single_application_keyboard(application: dict, event: dict) -> InlineKeyboardMarkup:
    """Побудувати клавіатуру для заявки з однією процедурою"""
    label = f"{event['time']} · {event['procedure_type']}"
    row = [
        InlineKeyboardButton(
            label,
            callback_data="noop",
            switch_inline_query_current_chat=f"Процедура: {event['procedure_type']} ({event['time']})"
        )
    ]

    status = application.get('status', 'pending')
    if status == 'pending':
        row.append(InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{application['id']}"))
        row.append(InlineKeyboardButton("❌", callback_data=f"reject_{application['id']}"))
    elif status == 'approved':
        row.append(InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{application['id']}"))
        row.append(InlineKeyboardButton("❌", callback_data=f"reject_{application['id']}"))
    elif status == 'primary':
        row.append(InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{application['id']}"))
        row.append(InlineKeyboardButton("❌", callback_data=f"reject_{application['id']}"))
    elif status in ('rejected', 'cancelled'):
        row.append(InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{application['id']}"))
        row.append(InlineKeyboardButton("❌", callback_data=f"reject_{application['id']}"))

    keyboard = [
        row,
        [InlineKeyboardButton("👤 Профіль кандидата", url=f"tg://user?id={application['user_id']}")]
    ]
    return InlineKeyboardMarkup(keyboard)


def remove_profile_button(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Повернути клавіатуру без кнопки відкриття профілю (tg://user?id=...)"""
    rows = []
    for row in markup.inline_keyboard:
        filtered = [btn for btn in row if not (getattr(btn, "url", "") or "").startswith("tg://user?id=")]
        if filtered:
            rows.append(filtered)
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


def format_day_count_text(count: int) -> str:
    """Повернути текст з кількістю заявок кандидата за день"""
    if count <= 0:
        return ""
    if count == 1:
        return ""
    if 2 <= count <= 4:
        return f" ({count} заявки від цього кандидата на цей день)"
    return f" ({count} заявок від цього кандидата на цей день)"


def build_message_link(chat_identifier, message_id: Optional[int]) -> Optional[str]:
    """Побудувати посилання на повідомлення Telegram"""
    if not message_id or not chat_identifier:
        return None

    chat_id = chat_identifier
    if isinstance(chat_id, str):
        if chat_id.startswith('@'):
            return f"https://t.me/{chat_id.lstrip('@')}/{message_id}"
        try:
            chat_id = int(chat_id)
        except ValueError:
            return None

    if chat_id > 0:
        return f"https://t.me/c/{chat_id}/{message_id}"

    chat_id_str = str(chat_id)
    if chat_id_str.startswith('-100'):
        return f"https://t.me/c/{chat_id_str[4:]}/{message_id}"

    return None

def format_status_counts(counter: Counter) -> str:
    """Повернути компактне представлення кількості заявок за статусами"""
    parts = []
    for status in STATUS_DISPLAY_ORDER:
        count = counter.get(status, 0)
        if count:
            parts.append(f"{APPLICATION_STATUS_EMOJI.get(status, '')}{count}")
    return " ".join(parts) if parts else "заявок поки немає"


def _get_event_datetime(event: dict) -> Optional[datetime]:
    """Повернути datetime події в київському часі"""
    try:
        dt = datetime.strptime(f"{event['date']} {event['time']}", "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=UKRAINE_TZ)
    except Exception:
        return None


def cancel_primary_reminders(job_queue, application_id: int) -> None:
    """Скасувати заплановані нагадування для заявки"""
    if job_queue is None:
        return
    for tag in ("24h", "3h"):
        name = f"reminder_app_{application_id}_{tag}"
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()


async def send_primary_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Надіслати нагадування основному кандидату, якщо він досі primary"""
    data = context.job.data or {}
    application_id = data.get("application_id")
    hours = data.get("hours")
    if not application_id:
        return

    app = db.get_application(application_id)
    if not app or app.get("status") != "primary":
        return

    event = db.get_event(app["event_id"])
    if not event:
        return

    event_dt = _get_event_datetime(event)
    if event_dt and event_dt <= datetime.now(UKRAINE_TZ):
        return

    text = (
        "Нагадування про вашу участь!\n\n"
        f"Процедура: {event['procedure_type']}\n"
        f"Дата: {format_date(event['date'])}\n"
        f"Час: {event['time']}\n"
    )
    if hours:
        text = f"Нагадування (за {hours} год до початку).\n\n" + text

    try:
        await context.bot.send_message(chat_id=app["user_id"], text=text)
    except Exception as err:
        logger.debug("Не вдалося надіслати нагадування primary: app_id=%s err=%s", application_id, err)


async def schedule_primary_reminders(context: ContextTypes.DEFAULT_TYPE, app: dict, event: dict) -> None:
    """Запланувати нагадування основному кандидату за 24h та 3h"""
    job_queue = context.application.job_queue
    if job_queue is None:
        logger.debug("JobQueue відсутній, нагадування не заплановано: app_id=%s", app["id"])
        return
    cancel_primary_reminders(job_queue, app["id"])

    event_dt = _get_event_datetime(event)
    if not event_dt:
        return

    now = datetime.now(UKRAINE_TZ)
    for hours in (24, 3):
        run_at = event_dt - timedelta(hours=hours)
        if run_at <= now:
            continue
        job_queue.run_once(
            send_primary_reminder,
            when=run_at,
            data={"application_id": app["id"], "hours": hours},
            name=f"reminder_app_{app['id']}_{hours}h",
        )


def build_day_summary_text(context: ContextTypes.DEFAULT_TYPE, date: str) -> Optional[str]:
    """Сформувати підсумкове повідомлення по всіх процедурах дня"""
    all_events = db.get_events_by_date(date)
    # Фільтруємо cancelled заходи
    events = [e for e in all_events if e.get('status') != 'cancelled']
    if not events:
        return None

    lines = [f"📅 {format_date(date)}", "", "Процедури дня:"]
    user_day_counts: Dict[int, int] = {}
    channel_id = context.bot_data.get('applications_channel_id', APPLICATIONS_CHANNEL_ID)

    for idx, event in enumerate(events, start=1):
        applications = db.get_applications_by_event(event['id'])
        photo_note = " (фото обов'язково)" if event.get('needs_photo') else ""
        header = f"{idx}. {event['time']} — {event['procedure_type']}{photo_note}"
        lines.append(header)

        if event.get('comment'):
            lines.append(f"   Коментар: {html.escape(event['comment'])}")

        if applications:
            status_counter = Counter(app['status'] for app in applications)
            lines.append(f"   Статуси: {format_status_counts(status_counter)}")

            for app_record in applications:
                status = app_record['status']
                emoji = APPLICATION_STATUS_EMOJI.get(status, '•')

                user_id = app_record['user_id']
                if user_id not in user_day_counts:
                    user_day_counts[user_id] = len(db.get_user_applications_for_date(user_id, date))
                day_count = user_day_counts[user_id]

                name = html.escape(app_record['full_name'])
                phone = html.escape(app_record['phone'] or "—")
                count_text = format_day_count_text(day_count)

                extras = []
                if status == 'primary':
                    extras.append("схвалено")
                elif status == 'approved':
                    extras.append("схвалено")

                extras_text = " ".join(html.escape(part) for part in extras) if extras else ""

                link = build_message_link(channel_id, app_record.get('group_message_id'))
                emoji_markup = f'<a href="{link}">{html.escape(emoji)}</a>' if link else html.escape(emoji)

                parts = [f"   • {name} — {phone}{count_text}".strip()]
                if extras_text:
                    parts.append(extras_text)
                parts.append(emoji_markup)
                line = " ".join(part for part in parts if part)
                lines.append(line)
        else:
            lines.append("   Заявок поки немає.")

        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


async def update_day_summary(context: ContextTypes.DEFAULT_TYPE, date: str) -> None:
    """Створити або оновити підсумкове повідомлення по дню в адмінській групі"""
    global GROUP_ID
    summary_text = build_day_summary_text(context, date)
    if not summary_text:
        return

    day_summary_cache: Dict[str, Optional[int]] = context.bot_data.setdefault('day_summary_messages', {})
    updating_dates: set = context.bot_data.setdefault('day_summary_updating', set())
    if date in updating_dates:
        return

    message_id = day_summary_cache.get(date)
    if message_id is None:
        message_id = db.get_day_message_id(date)
        if message_id is not None:
            day_summary_cache[date] = message_id

    logger.debug(
        "Оновлення денного підсумку: дата=%s, cached_message_id=%s",
        date,
        message_id
    )

    while True:
        group_id = context.bot_data.get('group_id', GROUP_ID)
        if isinstance(group_id, str):
            if group_id.startswith('@'):
                resolved_group_id = group_id
            else:
                try:
                    resolved_group_id = int(group_id)
                except ValueError:
                    base_group_id = GROUP_ID
                    if isinstance(base_group_id, str) and base_group_id.startswith('@'):
                        resolved_group_id = base_group_id
                    else:
                        try:
                            resolved_group_id = int(base_group_id)
                        except ValueError:
                            logger.error(f"Некоректний GROUP_ID: {base_group_id}")
                            return
        else:
            resolved_group_id = group_id

        logger.debug(
            "Спроба оновлення підсумку: date=%s, resolved_group_id=%s, message_id=%s",
            date,
            resolved_group_id,
            message_id
        )

        updating_dates.add(date)
        try:
            if message_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=resolved_group_id,
                        message_id=message_id,
                        text=summary_text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                    dialogs = context.chat_data.setdefault('admin_dialogs', {})
                    dialogs[f'day_summary_{date}'] = {'chat_id': resolved_group_id, 'message_id': message_id}
                    return
                except BadRequest as err:
                    if "message is not modified" in str(err).lower():
                        return
                    logger.debug(f"Підсумкове повідомлення дня відсутнє або не редагується, надсилаємо нове: {err}")
                    message_id = None
                    db.delete_day_message(date)
                    day_summary_cache.pop(date, None)
                    continue

            try:
                message = await context.bot.send_message(
                    chat_id=resolved_group_id,
                    text=summary_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
                logger.info(
                    "Підсумок дня опубліковано: date=%s, chat_id=%s, message_id=%s",
                    date,
                    resolved_group_id,
                    getattr(message, "message_id", None)
                )
                db.update_day_message_id(date, message.message_id)
                day_summary_cache[date] = message.message_id
                dialogs = context.chat_data.setdefault('admin_dialogs', {})
                dialogs[f'day_summary_{date}'] = {'chat_id': resolved_group_id, 'message_id': message.message_id}
                return
            except ChatMigrated as err:
                new_id = err.new_chat_id
                context.bot_data['group_id'] = new_id
                day_summary_cache.pop(date, None)
                db.delete_day_message(date)
                GROUP_ID = new_id
                message_id = None
                logger.info(f"Групу перенесено до нового chat_id={new_id}. Оновлюю підсумкове повідомлення.")
                continue
        except ChatMigrated as err:
            new_id = err.new_chat_id
            context.bot_data['group_id'] = new_id
            day_summary_cache.pop(date, None)
            db.delete_day_message(date)
            GROUP_ID = new_id
            message_id = None
            logger.info(f"Групу перенесено до нового chat_id={new_id}. Оновлюю підсумкове повідомлення.")
            continue
        except Exception as err:
            day_summary_cache.pop(date, None)
            logger.error(f"Не вдалося оновити підсумкове повідомлення дня {date}: {err}")
            return
        finally:
            updating_dates.discard(date)


async def send_primary_instruction(context: ContextTypes.DEFAULT_TYPE, app: dict, event: dict) -> bool:
    """Надіслати кандидату інструкцію для основного учасника"""
    safe_procedure = html.escape(str(event.get('procedure_type', '')))
    safe_time = html.escape(str(event.get('time', '')))
    instruction = (
        f"Вітаємо! Вашу заявку схвалено!\n\n"
        f"Процедура: {safe_procedure}\n"
        f"Дата: {format_date(event['date'])}\n"
        f"Час: {safe_time}\n\n"
        "Адреса: Оболонський проспект, 28, Medicalaser\n\n"
        "https://maps.app.goo.gl/2spzGUtcyYP38bHh8?g_st=ic\n\n"
        "Контакти адміністратора: +380962201240 (Вікторія)\n\n"
        "Інструкції:\n"
        "• Будь ласка, прийдіть за 15 хвилин до початку\n"
        "• Майте при собі документ, що підтверджує особу\n"
        "• У разі неможливості прийти - повідомте нас заздалегідь\n\n"
        "До зустрічі!"
    )

    rejected_procedures = get_rejected_procedures_for_related_applications(app, event)
    if rejected_procedures:
        rejected_text = ", ".join(rejected_procedures)
        instruction += (
            "\n\n❌ <b>На жаль, ми не можемо підтвердити запис на наступні процедури:</b>\n"
            f"{html.escape(rejected_text)}"
        )

    try:
        await context.bot.send_message(
            chat_id=app['user_id'],
            text=instruction,
            reply_markup=get_user_keyboard(),
            parse_mode=ParseMode.HTML
        )

        video_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "find.mp4")
        if os.path.exists(video_path):
            try:
                with open(video_path, "rb") as video_file:
                    await context.bot.send_video(
                        chat_id=app['user_id'],
                        video=video_file,
                        caption="На відео показано, як нас знайти! Інструкція щодо візиту вище"
                    )
            except Exception as video_err:
                logger.error(f"Не вдалося надіслати відеоінструкцію основному кандидату: {video_err}")
        else:
            logger.warning("Відеоінструкцію не надіслано: файл find.mp4 не знайдено поруч з bot.py")

        return True
    except Exception as err:
        logger.error(f"Помилка відправки інструкції основному кандидату: {err}")
        return False


async def promote_candidate_to_primary(
    context: ContextTypes.DEFAULT_TYPE,
    application_id: int,
    *,
    notify_user: bool = True
) -> Optional[dict]:
    """Позначити кандидата основним, оновивши повідомлення та сповістивши користувача"""
    app = db.get_application(application_id)
    if not app:
        return None

    event = db.get_event(app['event_id'])
    if not event:
        return None

    db.set_primary_application(application_id)
    db.recalculate_application_positions(event['id'])
    await sync_event_filled_state(context, event['id'])

    instruction_sent = True
    if notify_user:
        instruction_sent = await send_primary_instruction(context, app, event)

    app = db.get_application(application_id)
    await update_day_summary(context, event['date'])
    await schedule_primary_reminders(context, app, event)
    group_updated = await refresh_group_application_message(context, application_id)
    return {
        'app': app,
        'event': event,
        'instruction_sent': instruction_sent,
        'group_updated': group_updated
    }


async def sync_event_filled_state(context: ContextTypes.DEFAULT_TYPE, event_id: int) -> None:
    """Синхронізувати статус заходу (published/filled) та оновити публічне повідомлення"""
    event = db.get_event(event_id)
    if not event:
        return

    applications = db.get_applications_by_event(event_id)
    has_primary = any(app.get('status') == 'primary' for app in applications)
    new_status = 'filled' if has_primary else 'published'

    if event.get('status') != new_status:
        db.update_event_status(event_id, new_status)

    await refresh_day_schedule_message(context, event['date'])


async def promote_next_candidate(context: ContextTypes.DEFAULT_TYPE, event_id: int) -> Optional[int]:
    """Зробити наступного кандидата основним, якщо поточний скасований"""
    applications = db.get_applications_by_event(event_id)

    # Якщо вже є основний кандидат – нічого не робимо
    for app in applications:
        if app['status'] == 'primary':
            return app['id']

    for app in applications:
        if app['status'] == 'approved':
            result = await promote_candidate_to_primary(context, app['id'])
            if result:
                return app['id']
    return None


async def publish_group_application_to_channel(
    context: ContextTypes.DEFAULT_TYPE,
    application_results: list,
    candidate: dict,
    photos: list
) -> None:
    """Публікація комбінованої заявки в канал"""
    global APPLICATIONS_CHANNEL_ID

    channel_id = context.bot_data.get('applications_channel_id', APPLICATIONS_CHANNEL_ID)
    if isinstance(channel_id, str):
        try:
            channel_id = int(channel_id)
        except ValueError:
            channel_id = int(APPLICATIONS_CHANNEL_ID)

    applications_data = [
        {
            'id': app_id,
            'event': event,
            'status': 'pending',
            'is_primary': False
        }
        for app_id, event in application_results
    ]

    # Формуємо список номерів заявок для підпису фото
    app_numbers = ", ".join([f"№{app['id']}" for app in applications_data])
    photo_caption = f"Фото до заявки від {candidate['full_name']}\nЗаявки: {app_numbers}"

    if photos:
        try:
            if len(photos) == 1:
                await context.bot.send_photo(
                    chat_id=channel_id,
                    photo=photos[0],
                    caption=photo_caption
                )
            else:
                media = [
                    InputMediaPhoto(
                        media=photo_id,
                        caption=photo_caption if idx == 0 else ''
                    )
                    for idx, photo_id in enumerate(photos)
                ]
                await context.bot.send_media_group(chat_id=channel_id, media=media)
        except ChatMigrated as e:
            new_id = e.new_chat_id
            context.bot_data['applications_channel_id'] = new_id
            APPLICATIONS_CHANNEL_ID = new_id
            return await publish_group_application_to_channel(context, application_results, candidate, photos)
        except Exception as err:
            logger.error(f"Не вдалося надіслати фото заявки в канал: {err}")

    message_text = build_group_application_text(applications_data, candidate)
    keyboard = build_group_application_keyboard(applications_data, candidate)

    try:
        message = await context.bot.send_message(
            chat_id=channel_id,
            text=message_text,
            reply_markup=keyboard
        )
    except ChatMigrated as e:
        new_id = e.new_chat_id
        context.bot_data['applications_channel_id'] = new_id
        APPLICATIONS_CHANNEL_ID = new_id
        return await publish_group_application_to_channel(context, application_results, candidate, photos)
    except BadRequest as err:
        if "Button_user_privacy_restricted" in str(err):
            logger.warning(
                "Публікація комбінованої заявки без кнопки профілю (privacy): user_id=%s, apps=%s",
                candidate.get('user_id'),
                [a['id'] for a in applications_data]
            )
            safe_keyboard = remove_profile_button(keyboard)
            try:
                message = await context.bot.send_message(
                    chat_id=channel_id,
                    text=message_text,
                    reply_markup=safe_keyboard
                )
            except Exception as retry_err:
                logger.error(f"Не вдалося опублікувати комбіновану заявку навіть без профілю: {retry_err}")
                return
        else:
            logger.error(f"Не вдалося опублікувати заявку в канал: {err}")
            return
    except Exception as err:
        logger.error(f"Не вдалося опублікувати заявку в канал: {err}")
        return

    for application_id, _ in application_results:
        db.update_application_group_message_id(application_id, message.message_id)

    updated_dates = {event['date'] for _, event in application_results}
    for event_date in updated_dates:
        await update_day_summary(context, event_date)


async def refresh_group_application_message(
    context: ContextTypes.DEFAULT_TYPE,
    application_id: int
) -> bool:
    """Оновити комбіноване повідомлення, якщо заявка є частиною групи"""
    app = db.get_application_with_event(application_id)
    if not app or not app.get('group_message_id'):
        return False

    group_message_id = app['group_message_id']
    channel_id = context.bot_data.get('applications_channel_id', APPLICATIONS_CHANNEL_ID)
    if isinstance(channel_id, str):
        try:
            channel_id = int(channel_id)
        except ValueError:
            channel_id = int(APPLICATIONS_CHANNEL_ID)

    applications = db.get_applications_by_group_message(group_message_id)
    if not applications:
        return False

    candidate = {
        'full_name': applications[0]['full_name'],
        'phone': applications[0]['phone'],
        'user_id': applications[0]['user_id']
    }

    applications_data = [
        {
            'id': item['id'],
            'event': {
                'id': item['event_id'],
                'procedure_type': item['procedure_type'],
                'date': item['date'],
                'time': item['time'],
                'needs_photo': bool(item.get('needs_photo'))
            },
            'status': item['status'],
            'is_primary': bool(item.get('is_primary'))
        }
        for item in applications
    ]

    text = build_group_application_text(applications_data, candidate)
    keyboard = build_group_application_keyboard(applications_data, candidate)

    try:
        await context.bot.edit_message_text(
            chat_id=channel_id,
            message_id=group_message_id,
            text=text,
            reply_markup=keyboard
        )
    except BadRequest as err:
        error_msg = str(err).lower()

        # Якщо повідомлення видалене
        if "message to edit not found" in error_msg or "message not found" in error_msg:
            logger.debug(f"Повідомлення {group_message_id} не знайдено (можливо видалено)")
            return False

        # Якщо текст не змінено
        if "message is not modified" in error_msg:
            return True

        # Обмеження приватності — прибираємо кнопку профілю
        if "button_user_privacy_restricted" in error_msg:
            logger.warning(
                "Оновлення групової заявки без профільної кнопки (privacy): group_msg_id=%s, user_id=%s",
                group_message_id,
                candidate['user_id']
            )
            safe_keyboard = remove_profile_button(keyboard)
            try:
                await context.bot.edit_message_text(
                    chat_id=channel_id,
                    message_id=group_message_id,
                    text=text,
                    reply_markup=safe_keyboard
                )
                return True
            except Exception as retry_err:
                logger.debug(
                    "Не вдалося оновити комбіноване повідомлення заявки навіть без профільної кнопки: %s",
                    retry_err
                )
                return False

        # Повідомлення може бути фото з caption
        if "no text" in error_msg:
            try:
                await context.bot.edit_message_caption(
                    chat_id=channel_id,
                    message_id=group_message_id,
                    caption=text,
                    reply_markup=keyboard
                )
                return True
            except BadRequest as caption_err:
                caption_error = str(caption_err).lower()
                if "message is not modified" in caption_error:
                    return True
                if "button_user_privacy_restricted" in caption_error:
                    logger.warning(
                        "Оновлення групової заявки без профільної кнопки (privacy/caption): group_msg_id=%s, user_id=%s",
                        group_message_id,
                        candidate['user_id']
                    )
                    safe_keyboard = remove_profile_button(keyboard)
                    try:
                        await context.bot.edit_message_caption(
                            chat_id=channel_id,
                            message_id=group_message_id,
                            caption=text,
                            reply_markup=safe_keyboard
                        )
                        return True
                    except Exception as retry_err:
                        logger.debug(
                            "Не вдалося оновити caption комбінованого повідомлення навіть без профільної кнопки: %s",
                            retry_err
                        )
                        return False
                logger.debug(f"Не вдалося оновити caption комбінованого повідомлення: {caption_err}")
                return False

        logger.debug(f"Не вдалося оновити комбіноване повідомлення заявки: {err}")
        return False
    except Exception as err:
        logger.debug(f"Не вдалося оновити комбіноване повідомлення заявки: {err}")
        return False

    return True


async def refresh_single_application_message(
    context: ContextTypes.DEFAULT_TYPE,
    application_id: int
) -> bool:
    """Оновити повідомлення одиночної заявки (текст і клавіатуру)"""
    app = db.get_application_with_event(application_id)
    if not app or not app.get('group_message_id'):
        return False

    channel_id = context.bot_data.get('applications_channel_id', APPLICATIONS_CHANNEL_ID)
    if isinstance(channel_id, str):
        try:
            channel_id = int(channel_id)
        except ValueError:
            channel_id = int(APPLICATIONS_CHANNEL_ID)

    event = db.get_event(app['event_id'])
    if not event:
        return False

    # Формуємо оновлений текст
    status_icon = format_application_status(app['status'], app.get('is_primary', False))
    # Екранувати user input для безпеки
    safe_name = safe_html(app['full_name'])
    safe_phone = safe_html(app['phone'])
    safe_procedure = safe_html(event['procedure_type'])

    message_text = (
        f"Нова заявка від {safe_name}\n"
        f"Телефон: {safe_phone}\n"
        f"ID користувача: {app['user_id']}\n\n"
        f"Обрана процедура:\n"
        f"1. (№{application_id}) {format_date(event['date'])} {event['time']} — {safe_procedure} {status_icon}"
    )

    keyboard = build_single_application_keyboard(app, event)

    try:
        # Перевіряємо чи повідомлення є фото чи текст
        try:
            # Спробуємо оновити як текст
            await context.bot.edit_message_text(
                chat_id=channel_id,
                message_id=app['group_message_id'],
                text=message_text,
                reply_markup=keyboard
            )
            return True
        except BadRequest as e:
            error_msg = str(e).lower()

            # Якщо повідомлення не знайдено - воно було видалено
            if "message to edit not found" in error_msg or "message not found" in error_msg:
                logger.debug(f"Повідомлення {app['group_message_id']} не знайдено (можливо видалено)")
                return False

            # Якщо повідомлення вже актуальне
            if "message is not modified" in error_msg:
                return True

            # Обмеження приватності — прибираємо кнопку профілю і пробуємо знову
            if "button_user_privacy_restricted" in error_msg:
                logger.warning(
                    "Оновлення одиночної заявки без профільної кнопки (privacy/text): msg_id=%s, user_id=%s",
                    app['group_message_id'],
                    app['user_id']
                )
                safe_keyboard = remove_profile_button(keyboard)
                try:
                    await context.bot.edit_message_text(
                        chat_id=channel_id,
                        message_id=app['group_message_id'],
                        text=message_text,
                        reply_markup=safe_keyboard
                    )
                    return True
                except BadRequest as retry_err:
                    retry_msg = str(retry_err).lower()
                    if "message is not modified" in retry_msg:
                        return True
                    if "no text" not in retry_msg:
                        logger.debug(f"Не вдалося оновити текст одиночної заявки без профілю: {retry_err}")
                        return False
                    error_msg = retry_msg  # продовжимо як фото

            # Інакше це може бути фото - спробуємо оновити caption
            try:
                await context.bot.edit_message_caption(
                    chat_id=channel_id,
                    message_id=app['group_message_id'],
                    caption=message_text,
                    reply_markup=keyboard
                )
                return True
            except BadRequest as caption_err:
                caption_error_msg = str(caption_err).lower()
                if "message is not modified" in caption_error_msg:
                    return True
                if "button_user_privacy_restricted" in caption_error_msg:
                    logger.warning(
                        "Оновлення одиночної заявки без профільної кнопки (privacy/caption): msg_id=%s, user_id=%s",
                        app['group_message_id'],
                        app['user_id']
                    )
                    safe_keyboard = remove_profile_button(keyboard)
                    try:
                        await context.bot.edit_message_caption(
                            chat_id=channel_id,
                            message_id=app['group_message_id'],
                            caption=message_text,
                            reply_markup=safe_keyboard
                        )
                        return True
                    except BadRequest as retry_err:
                        if "message is not modified" in str(retry_err).lower():
                            return True
                        logger.debug(f"Не вдалося оновити caption одиночної заявки без профілю: {retry_err}")
                        return False
                logger.debug(f"Не вдалося оновити caption: {caption_err}")
                return False
    except Exception as err:
        logger.debug(f"Не вдалося оновити повідомлення одиночної заявки: {err}")
        return False


# ==================== УПРАВЛІННЯ ЗАЯВКАМИ ====================

async def approve_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прийняти заявку"""
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await answer_callback_query(query, "Немає доступу", show_alert=True)
        return

    await answer_callback_query(query)

    application_id = int(query.data.split('_')[1])
    app = db.get_application(application_id)
    if not app:
        await send_admin_message_from_query(query, context, "Заявка не знайдена або вже оброблена.")
        return
    if app.get('status') in ('approved', 'primary'):
        return

    # "Схвалити" = погодити заявку для заходу (статус primary)
    db.set_primary_application(application_id)
    db.recalculate_application_positions(app['event_id'])
    event = db.get_event(app['event_id'])
    if event:
        await sync_event_filled_state(context, event['id'])
        await update_day_summary(context, event['date'])

    app = db.get_application(application_id)
    await maybe_send_final_review_notification(context, app, event)

    # Оновлюємо групове повідомлення або одиночне
    if await refresh_group_application_message(context, application_id):
        return

    # Якщо це не групова заявка, оновлюємо одиночну заявку
    if not await refresh_single_application_message(context, application_id):
        # Якщо не вдалося оновити текст, оновимо хоча б клавіатуру
        if event:
            keyboard = build_single_application_keyboard(app, event)
            try:
                await query.edit_message_reply_markup(reply_markup=keyboard)
            except BadRequest as err:
                if "Button_user_privacy_restricted" in str(err):
                    safe_keyboard = remove_profile_button(keyboard)
                    try:
                        await query.edit_message_reply_markup(reply_markup=safe_keyboard)
                    except Exception as retry_err:
                        logger.debug(
                            "Не вдалося оновити клавіатуру навіть без профільної кнопки: %s",
                            retry_err
                        )
                else:
                    raise
        else:
            await query.edit_message_reply_markup(reply_markup=None)


async def reject_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Відхилити заявку"""
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await answer_callback_query(query, "Немає доступу", show_alert=True)
        return

    await answer_callback_query(query)

    application_id = int(query.data.split('_')[1])
    application = db.get_application(application_id)
    if not application:
        await send_admin_message_from_query(query, context, "Заявка не знайдена або вже оброблена.")
        return
    if application.get('status') == 'rejected':
        return

    # Якщо це основний кандидат - показати попередження
    if application['status'] == 'primary':
        event = db.get_event(application['event_id'])
        if event:
            warning_text = (
                "⚠️ <b>УВАГА!</b>\n\n"
                f"Ви намагаєтесь відхилити <b>основного кандидата</b>.\n\n"
                f"👤 {html.escape(application['full_name'])}\n"
                f"📞 {html.escape(application['phone'])}\n"
                f"📅 {format_date(event['date'])}\n"
                f"🕐 {event['time']} - {event['procedure_type']}\n\n"
                f"Продовжити?"
            )
            keyboard = [
                [
                    InlineKeyboardButton("✅ Підтвердити", callback_data=f"confirm_reject_primary_{application_id}"),
                    InlineKeyboardButton("❌ Скасувати", callback_data=f"cancel_reject_primary_{application_id}")
                ]
            ]
            # Перевіряємо тип повідомлення (фото чи текст)
            try:
                if query.message.photo:
                    # Якщо це фото, редагуємо caption
                    await query.edit_message_caption(
                        caption=warning_text,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode=ParseMode.HTML
                    )
                else:
                    # Якщо це текст, редагуємо текст
                    await query.edit_message_text(
                        text=warning_text,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode=ParseMode.HTML
                    )
            except Exception as err:
                logger.error(f"Помилка при показі попередження про відхилення primary: {err}")
                await send_admin_message_from_query(query, context, warning_text)
        return

    # Якщо не primary - відхиляємо як зазвичай
    db.update_application_status(application_id, 'rejected')
    db.recalculate_application_positions(application['event_id'])
    event = db.get_event(application['event_id'])
    await maybe_send_final_review_notification(context, application, event)

    if event:
        await update_day_summary(context, event['date'])

    # Оновлюємо повідомлення в групі або одиночне
    if await refresh_group_application_message(context, application_id):
        return

    # Якщо це не групова заявка, оновлюємо одиночну заявку
    refreshed = db.get_application(application_id)
    if not await refresh_single_application_message(context, application_id):
        # Якщо не вдалося оновити текст, оновимо хоча б клавіатуру
        if event:
            try:
                await query.edit_message_reply_markup(
                    reply_markup=build_single_application_keyboard(refreshed, event)
                )
            except BadRequest as err:
                err_msg = str(err).lower()
                if "button_user_privacy_restricted" in err_msg:
                    safe_keyboard = remove_profile_button(build_single_application_keyboard(refreshed, event))
                    await query.edit_message_reply_markup(reply_markup=safe_keyboard)
                else:
                    raise
        else:
            await query.edit_message_reply_markup(reply_markup=None)


async def set_primary_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Встановити заявку як основну"""
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await answer_callback_query(query, "Немає доступу", show_alert=True)
        return

    await answer_callback_query(query, "Заявку схвалено")

    application_id = int(query.data.split('_')[1])
    result = await promote_candidate_to_primary(context, application_id, notify_user=False)

    if not result:
        await send_admin_message_from_query(query, context, "Не вдалося оновити заявку")
        return

    await maybe_send_final_review_notification(context, result['app'], result['event'])

    if not result['group_updated']:
        # Якщо це не групова заявка, оновлюємо одиночну заявку
        if not await refresh_single_application_message(context, application_id):
            # Якщо не вдалося оновити текст, оновимо хоча б клавіатуру
            try:
                await query.edit_message_reply_markup(
                    reply_markup=build_single_application_keyboard(result['app'], result['event'])
                )
            except Exception as err:
                logger.debug(f"Не вдалося оновити клавіатуру після призначення основного кандидата: {err}")


async def confirm_reject_primary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Підтвердити відхилення основного кандидата"""
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await answer_callback_query(query, "Немає доступу", show_alert=True)
        return

    await answer_callback_query(query, "Основного кандидата відхилено")

    application_id = int(query.data.split('_')[-1])
    application = db.get_application(application_id)
    if not application:
        await send_admin_message_from_query(query, context, "Заявка не знайдена або вже оброблена.")
        return

    # Відхиляємо основного кандидата
    db.update_application_status(application_id, 'rejected')
    cancel_primary_reminders(context.application.job_queue, application_id)
    db.recalculate_application_positions(application['event_id'])

    event = db.get_event(application['event_id'])
    if event:
        await update_day_summary(context, event['date'])
        await sync_event_filled_state(context, event['id'])

    await maybe_send_final_review_notification(context, application, event)

    # Повідомляємо адміністратора про успішне відхилення
    await send_admin_message_from_query(
        query,
        context,
        "✅ Основного кандидата відхилено."
    )

    # Оновлюємо повідомлення в групі або одиночне
    if await refresh_group_application_message(context, application_id):
        return

    # Якщо це не групова заявка, оновлюємо одиночну заявку
    refreshed = db.get_application(application_id)
    if not await refresh_single_application_message(context, application_id):
        # Якщо не вдалося оновити текст, оновимо хоча б клавіатуру
        if event:
            await query.edit_message_reply_markup(
                reply_markup=build_single_application_keyboard(refreshed, event)
            )
        else:
            await query.edit_message_reply_markup(reply_markup=None)


async def cancel_reject_primary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скасувати відхилення основного кандидата"""
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await answer_callback_query(query, "Немає доступу", show_alert=True)
        return

    await answer_callback_query(query, "Дію скасовано")

    application_id = int(query.data.split('_')[-1])
    application = db.get_application(application_id)
    if not application:
        await send_admin_message_from_query(query, context, "Заявка не знайдена.")
        return

    event = db.get_event(application['event_id'])
    if not event:
        await send_admin_message_from_query(query, context, "Захід не знайдений.")
        return

    # Повертаємося до звичайного відображення заявки
    text = (
        f"Заявка (№{application_id})\n\n"
        f"Процедура: {event['procedure_type']}\n"
        f"Дата: {format_date(event['date'])} {event['time']}\n\n"
        f"ПІБ: {application['full_name']}\n"
        f"Телефон: {application['phone']}"
    )
    keyboard = build_single_application_keyboard(application, event)

    # Перевіряємо тип повідомлення (фото чи текст)
    try:
        if query.message.photo:
            # Якщо це фото, редагуємо caption
            await query.edit_message_caption(
                caption=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
        else:
            # Якщо це текст, редагуємо текст
            await query.edit_message_text(
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
    except Exception as err:
        logger.error(f"Помилка при відновленні відображення заявки: {err}")
        # Fallback - оновлюємо тільки клавіатуру
        await query.edit_message_reply_markup(reply_markup=keyboard)


async def _finalize_application_cancellation(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    app: dict,
    *,
    apology: bool = False
) -> None:
    """Допоміжна функція для завершення скасування заявки"""
    application_id = app['id']
    event = db.get_event(app['event_id'])

    if app.get('status') == 'primary':
        cancel_primary_reminders(context.application.job_queue, application_id)

    db.update_application_status(application_id, 'cancelled')
    db.recalculate_application_positions(app['event_id'])

    if event:
        await update_day_summary(context, event['date'])

    # Формуємо повідомлення з деталями процедури
    if apology and event:
        message_text = (
            "Вибачте, але вашу раніше підтверджену заявку було скасовано. "
            "Просимо вибачення за незручності.\n\n"
            f"Процедура: {event['procedure_type']}\n"
            f"Дата: {format_date(event['date'])}\n"
            f"Час: {event['time']}"
        )
    elif event:
        message_text = (
            "Ваша заявка позначена як скасована.\n\n"
            f"Процедура: {event['procedure_type']}\n"
            f"Дата: {format_date(event['date'])}\n"
            f"Час: {event['time']}"
        )
    else:
        message_text = (
            "Вибачте, але вашу заявку було скасовано. Просимо вибачення за незручності."
            if apology else
            "Ваша заявка позначена як скасована."
        )

    try:
        await context.bot.send_message(
            chat_id=app['user_id'],
            text=message_text,
            reply_markup=get_user_keyboard()
        )
    except Exception as err:
        logger.debug(f"Не вдалося повідомити користувача про скасування заявки: {err}")

    group_updated = await refresh_group_application_message(context, application_id)
    promoted_id = await promote_next_candidate(context, app['event_id'])
    promoted_app = db.get_application(promoted_id) if promoted_id else None
    promoted_event = db.get_event(app['event_id']) if promoted_id else None

    if promoted_id:
        await send_admin_message_from_query(query, context, "Заявку скасовано. Наступного кандидата призначено основним.")
        if promoted_app and promoted_event:
            try:
                await context.bot.send_message(
                    chat_id=promoted_app['user_id'],
                    text=(
                        "Вітаємо! Вас призначено основним кандидатом.\n\n"
                        f"Процедура: {promoted_event['procedure_type']}\n"
                        f"Дата: {format_date(promoted_event['date'])}\n"
                        f"Час: {promoted_event['time']}"
                    ),
                    reply_markup=get_user_keyboard()
                )
            except Exception as err:
                logger.debug(f"Не вдалося повідомити нового основного кандидата: {err}")
    else:
        await send_admin_message_from_query(query, context, "Заявку скасовано. Резервних кандидатів немає.")

    if not group_updated:
        # Якщо це не групова заявка, оновлюємо одиночну заявку
        refreshed_app = db.get_application(application_id)
        refreshed_event = db.get_event(app['event_id'])
        if not await refresh_single_application_message(context, application_id):
            # Якщо не вдалося оновити текст, оновимо хоча б клавіатуру
            if refreshed_app and refreshed_event:
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=build_single_application_keyboard(refreshed_app, refreshed_event)
                    )
                except Exception as err:
                    logger.debug(f"Не вдалося оновити клавіатуру після скасування заявки: {err}")


async def cancel_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Позначити заявку як скасовану та за потреби призначити нового основного"""
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await answer_callback_query(query, "Немає доступу", show_alert=True)
        return

    application_id = int(query.data.split('_')[1])
    app = db.get_application(application_id)

    if not app:
        await answer_callback_query(query, "Заявка не знайдена", show_alert=True)
        await send_admin_message_from_query(query, context, "Заявка не знайдена або вже видалена.")
        return

    logger.debug(
        "cancel_application: application_id=%s status=%s",
        application_id,
        app.get('status')
    )

    # Перевірити чи заявка вже скасована
    if app.get('status') == 'cancelled':
        await answer_callback_query(query, "Заявка вже скасована", show_alert=True)
        return

    if app.get('status') == 'primary':
        confirm_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Так, скасувати", callback_data=f"confirm_cancel_primary_{application_id}")],
            [InlineKeyboardButton("⬅️ Залишити без змін", callback_data=f"cancel_primary_back_{application_id}")]
        ])
        try:
            await query.edit_message_reply_markup(reply_markup=confirm_keyboard)
        except Exception as err:
            logger.debug(f"Не вдалося показати підтвердження скасування: {err}")
        await answer_callback_query(
            query,
            "Цьому кандидату вже надіслано повідомлення. Ви впевнені, що хочете скасувати?",
            show_alert=True
        )
        return

    await answer_callback_query(query, "Заявку скасовано")
    await _finalize_application_cancellation(query, context, app, apology=False)


async def confirm_cancel_primary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Підтвердження скасування заявки основного кандидата"""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await answer_callback_query(query, "Немає доступу", show_alert=True)
        return

    application_id = int(query.data.split('_')[3])
    app = db.get_application(application_id)
    if not app:
        await answer_callback_query(query, "Заявка не знайдена", show_alert=True)
        return

    await answer_callback_query(query, "Заявку скасовано")
    await _finalize_application_cancellation(query, context, app, apology=True)


async def cancel_primary_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повернутися без скасування основного кандидата"""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await answer_callback_query(query, "Немає доступу", show_alert=True)
        return

    application_id = int(query.data.split('_')[3])
    app = db.get_application(application_id)
    if not app:
        await answer_callback_query(query, "Заявка не знайдена", show_alert=True)
        return

    # Оновлюємо групове або одиночне повідомлення
    if not await refresh_group_application_message(context, application_id):
        # Якщо це не групова заявка, оновлюємо одиночну заявку
        if not await refresh_single_application_message(context, application_id):
            # Якщо не вдалося оновити текст, оновимо хоча б клавіатуру
            refreshed_event = db.get_event(app['event_id'])
            if refreshed_event:
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=build_single_application_keyboard(app, refreshed_event)
                    )
                except Exception as err:
                    logger.debug(f"Не вдалося відновити клавіатуру після скасування підтвердження: {err}")

    await answer_callback_query(query, "Скасування відмінено")


async def view_event_applications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переглянути заявки на захід"""
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await answer_callback_query(query, "Немає доступу", show_alert=True)
        return

    event_id = int(query.data.split('_')[2])
    all_applications = db.get_applications_by_event(event_id)

    if not all_applications:
        await answer_callback_query(query, "Немає заявок на цей захід", show_alert=True)
        return

    await answer_callback_query(query)

    # Отримати інформацію про захід
    event = db.get_event(event_id)

    # Сортуємо заявки: спочатку основний, потім схвалені, потім решта
    primary = [app for app in all_applications if app['is_primary'] == 1]
    approved = [app for app in all_applications if app['status'] == 'approved' and app['is_primary'] == 0]
    other = [app for app in all_applications if app['status'] != 'approved']

    message = f"📋 Заявки на захід:\n"
    message += f"📅 {event['procedure_type']}\n"
    message += f"🕐 {format_date(event['date'])} о {event['time']}\n\n"

    # Основний кандидат (червоним через HTML)
    if primary:
        app = primary[0]
        message += f"🔴 <b>ОСНОВНИЙ КАНДИДАТ:</b>\n"
        message += f"   👤 {app['full_name']}\n"
        message += f"   📱 {app['phone']}\n\n"

    # Схвалені заявки (жирним)
    if approved:
        message += "<b>✅ СХВАЛЕНІ ЗАЯВКИ:</b>\n"
        for i, app in enumerate(approved, 1):
            message += f"<b>{i}. {app['full_name']}</b>\n"
            message += f"   📱 {app['phone']}\n"
        message += "\n"

    # Інші заявки (pending, rejected, cancelled)
    if other:
        message += "📥 ІНШІ ЗАЯВКИ:\n"
        for app in other:
            status_map = {
                'pending': ('⏳', 'очікує'),
                'rejected': ('❌', 'відхилено'),
                'cancelled': ('🚫', 'скасовано')
            }
            status_emoji, status_text = status_map.get(app['status'], ('❓', 'невідомо'))

            message += f"{status_emoji} {app['full_name']}\n"
            message += f"   📱 {app['phone']}\n"
            message += f"   Статус: {status_text}\n"

    keyboard = [[InlineKeyboardButton("❌ Закрити", callback_data="close_message")]]

    # Відправити нове повідомлення замість редагування, бо вихідне повідомлення може містити фото
    try:
        await send_admin_message_from_query(query, context, 
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Помилка відображення заявок: {e}")
        await answer_callback_query(query, "Помилка відображення заявок", show_alert=True)


# ==================== ПОВІДОМЛЕННЯ КАНДИДАТУ ====================

async def forward_candidate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пересилання повідомлень від кандидатів в групу"""
    user_id = update.effective_user.id
    msg = update.effective_message

    # Перевірка що це не адмін
    if is_admin(user_id):
        return

    # Ігнорувати команди з меню користувача та адміна
    menu_commands = ["📋 Мої заявки", "ℹ️ Інформація", "🆕 Новий захід", "📋 Заходи", "⚙️"]
    if msg and msg.text and msg.text in menu_commands:
        return

    # Ігнорувати якщо це приватний чат (conversation активний)
    # Тільки обробляємо повідомлення які НЕ в контексті conversation
    if 'application' in context.user_data or 'event' in context.user_data:
        return

    # Перевірка що користувач є в базі (подавав заявку)
    user = db.get_user(user_id)
    if not user or not user['full_name']:
        return

    # Обробляємо лише текст або caption; інші типи пропускаємо
    text_content = None
    if msg:
        text_content = msg.text or msg.caption
    if not text_content:
        return

    # Переслати повідомлення в групу
    try:
        message_text = (
            f"💬 Повідомлення від кандидата:\n\n"
            f"👤 {user['full_name']}\n"
            f"📱 {user['phone']}\n"
            f"🆔 User ID: {user_id}\n\n"
            f"Текст: {text_content}"
        )

        keyboard = [[InlineKeyboardButton("👤 Профіль кандидата", url=f"tg://user?id={user_id}")]]

        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except ChatMigrated as e:
        logger.warning(f"Група мігрувала в супергрупу. Новий ID: {e.new_chat_id}")
        logger.warning(f"Оновіть GROUP_ID в .env файлі на: {e.new_chat_id}")
    except Exception as e:
        logger.error(f"Помилка пересилання повідомлення: {e}")


# ==================== MAIN ====================

async def on_bot_added_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка події додавання бота до чату (групи/каналу)"""
    my_chat_member = update.my_chat_member
    if not my_chat_member:
        return

    old_status = my_chat_member.old_chat_member.status
    new_status = my_chat_member.new_chat_member.status
    chat = my_chat_member.chat

    # Перевірка чи бот був доданий до чату
    if old_status in ['left', 'kicked'] and new_status in ['member', 'administrator']:
        logger.info(f"Бот додано до чату: {chat.title} (ID: {chat.id}, тип: {chat.type})")

        # Відправити привітальне повідомлення в групу
        try:
            welcome_text = (
                "Привіт! Я бот для запису на косметологічні процедури.\n\n"
                "Тепер я готовий обробляти заявки в цій групі."
            )
            await context.bot.send_message(
                chat_id=chat.id,
                text=welcome_text
            )
            logger.info(f"Відправлено привітальне повідомлення в чат {chat.id}")
        except Exception as e:
            logger.error(f"Помилка при відправці привітального повідомлення: {e}")


def main():
    """Запуск бота"""
    token = os.getenv('BOT_TOKEN')
    if not token:
        logger.error("BOT_TOKEN не знайдено в .env файлі!")
        return

    # Налаштування HTTP запитів з таймаутами
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=10.0,
        read_timeout=10.0,
        write_timeout=10.0
    )

    # Налаштування persistence для збереження стану
    persistence = PicklePersistence(filepath="bot_data.pickle")

    # Створення додатку з усіма налаштуваннями
    application = (
        Application.builder()
        .token(token)
        .request(request)
        .persistence(persistence)
        .build()
    )

    # Перекриваємо можливі застарілі значення з persistence актуальними з .env
    application.bot_data['group_id'] = GROUP_ID
    application.bot_data['applications_channel_id'] = APPLICATIONS_CHANNEL_ID

    # Обробник створення заходу
    create_event_handler = ConversationHandler(
        entry_points=[
            CommandHandler('create_event', create_event_start, filters=filters.ChatType.PRIVATE),
            CommandHandler('new_event', create_event_start, filters=filters.ChatType.PRIVATE),
            CallbackQueryHandler(admin_create_event_button, pattern='^admin_create_event$'),
            MessageHandler(filters.TEXT & filters.Regex('^🆕 Новий захід$') & filters.ChatType.PRIVATE, create_event_start),
            CallbackQueryHandler(create_event_same_date, pattern='^same_date_')
        ],
        states={
            CREATE_EVENT_DATE: [
                CallbackQueryHandler(create_event_date, pattern='^date_'),
                CallbackQueryHandler(create_event_date, pattern='^back_to_date$'),
                CallbackQueryHandler(close_admin_dialog_button, pattern='^close_admin_dialog$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            CREATE_EVENT_TIME: [
                CallbackQueryHandler(create_event_time, pattern='^time_'),
                CallbackQueryHandler(create_event_date, pattern='^back_to_date$'),
                CallbackQueryHandler(close_admin_dialog_button, pattern='^close_admin_dialog$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            CREATE_EVENT_PROCEDURE: [
                CallbackQueryHandler(create_event_procedure, pattern='^proc_'),
                CallbackQueryHandler(create_event_time, pattern='^back_to_time$'),
                CallbackQueryHandler(close_admin_dialog_button, pattern='^close_admin_dialog$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            CREATE_EVENT_PHOTO_NEEDED: [
                CallbackQueryHandler(create_event_photo_needed, pattern='^photo_'),
                CallbackQueryHandler(create_event_procedure, pattern='^back_to_procedure$'),
                CallbackQueryHandler(close_admin_dialog_button, pattern='^close_admin_dialog$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            CREATE_EVENT_COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, create_event_comment_text),
                CallbackQueryHandler(skip_event_comment, pattern='^skip_comment$'),
                CallbackQueryHandler(create_event_photo_needed, pattern='^back_to_photo$'),
                CallbackQueryHandler(close_admin_dialog_button, pattern='^close_admin_dialog$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            CREATE_EVENT_CONFIRM: [
                CallbackQueryHandler(add_event_to_day, pattern='^add_event_to_day$'),
                CallbackQueryHandler(close_admin_dialog_button, pattern='^close_admin_dialog$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            CREATE_EVENT_REVIEW: [
                CallbackQueryHandler(add_more_procedure, pattern='^add_more_procedure$'),
                CallbackQueryHandler(publish_schedule, pattern='^publish_schedule$'),
                CallbackQueryHandler(remove_last_procedure, pattern='^remove_last_procedure$'),
                CallbackQueryHandler(close_admin_dialog_button, pattern='^close_admin_dialog$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel, pattern='^cancel$')],
        name="create_event_conversation",
        persistent=True,
        allow_reentry=True,
        conversation_timeout=1800  # 30 хвилин
    )

    # Обробник подачі заявки
    apply_event_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start, filters=filters.ChatType.PRIVATE)],
        states={
            APPLY_SELECT_EVENTS: [
                CallbackQueryHandler(toggle_event_selection, pattern='^toggle_event_\\d+$'),
                CallbackQueryHandler(event_selection_reset, pattern='^event_selection_reset$'),
                CallbackQueryHandler(event_selection_continue, pattern='^event_selection_continue$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            APPLY_FULL_NAME: [
                CallbackQueryHandler(apply_use_saved_data, pattern='^use_saved_data$'),
                CallbackQueryHandler(apply_enter_new_data, pattern='^enter_new_data$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, apply_full_name),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            APPLY_PHONE: [
                MessageHandler(filters.CONTACT, apply_phone),
                MessageHandler(filters.TEXT & ~filters.COMMAND, apply_phone),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            APPLY_PHOTOS: [
                MessageHandler(filters.PHOTO, apply_photo),
                CallbackQueryHandler(apply_photos_done, pattern='^photos_done$'),
                CallbackQueryHandler(submit_application, pattern='^submit_application$'),
                CallbackQueryHandler(back_to_photos, pattern='^back_to_photos$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            APPLY_CONFIRM: [
                MessageHandler(filters.PHOTO, apply_photo),
                CallbackQueryHandler(submit_application, pattern='^submit_application$'),
                CallbackQueryHandler(back_to_photos, pattern='^back_to_photos$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel, pattern='^cancel$')],
        name="apply_event_conversation",
        persistent=True,
        allow_reentry=True,
        conversation_timeout=1800  # 30 хвилин
    )

    # Обробник блокування користувача
    block_user_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_block_user_button, pattern='^admin_block_user$')],
        states={
            BLOCK_USER_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, block_user_id),
                CallbackQueryHandler(cancel_block, pattern='^cancel_block$')
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel_block, pattern='^cancel_block$')],
        name="block_user_conversation",
        persistent=True,
        allow_reentry=True
    )

    # Обробник додавання типу процедури
    add_procedure_type_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_procedure_type_start, pattern='^pt_add$')],
        states={
            ADD_PROCEDURE_TYPE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_procedure_type_name),
                CallbackQueryHandler(cancel_procedure_type, pattern='^pt_cancel$')
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel_procedure_type, pattern='^pt_cancel$')],
        name="add_procedure_type_conversation",
        persistent=True,
        allow_reentry=True
    )

    # Обробник редагування типу процедури
    edit_procedure_type_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_procedure_type_start, pattern='^pt_edit_')],
        states={
            EDIT_PROCEDURE_TYPE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_procedure_type_name),
                CallbackQueryHandler(cancel_procedure_type, pattern='^pt_cancel$')
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel_procedure_type, pattern='^pt_cancel$')],
        name="edit_procedure_type_conversation",
        persistent=True,
        allow_reentry=True
    )

    # Обробник очистки БД з паролем
    clear_db_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_clear_db_button, pattern='^admin_clear_db$')],
        states={
            CLEAR_DB_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, clear_db_password),
                CallbackQueryHandler(cancel_clear_db, pattern='^cancel_clear_db$')
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel_clear_db, pattern='^cancel_clear_db$')],
        name="clear_db_conversation",
        persistent=True,
        allow_reentry=True
    )

    # Додати обробники (ConversationHandlers мають вищий пріоритет - group 0)
    application.add_handler(TypeHandler(Update, log_update), group=-1)

    # Обробник додавання бота до чату
    application.add_handler(ChatMemberHandler(on_bot_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER))

    application.add_handler(create_event_handler, group=0)
    application.add_handler(apply_event_handler, group=0)
    application.add_handler(block_user_handler, group=0)
    application.add_handler(add_procedure_type_handler, group=0)
    application.add_handler(edit_procedure_type_handler, group=0)
    application.add_handler(clear_db_handler, group=0)

    # Обробники кнопок адміністратора
    application.add_handler(CallbackQueryHandler(back_to_menu, pattern='^back_to_menu$'))
    application.add_handler(CallbackQueryHandler(noop_callback, pattern='^noop$'))
    application.add_handler(CallbackQueryHandler(close_message_callback, pattern='^close_message$'))
    application.add_handler(CallbackQueryHandler(show_admin_settings, pattern='^admin_settings$'))
    application.add_handler(CallbackQueryHandler(admin_manage_events_button, pattern='^admin_manage_events$'))
    application.add_handler(CallbackQueryHandler(admin_past_events_button, pattern='^past_events$'))
    application.add_handler(CallbackQueryHandler(cancel_event_confirm, pattern='^cancel_event_'))
    application.add_handler(CallbackQueryHandler(confirm_cancel_event, pattern='^confirm_cancel_event_'))
    application.add_handler(CallbackQueryHandler(admin_procedure_types, pattern='^admin_procedure_types$'))
    application.add_handler(CallbackQueryHandler(view_procedure_type, pattern='^pt_view_'))
    application.add_handler(CallbackQueryHandler(toggle_procedure_type_handler, pattern='^pt_toggle_'))
    application.add_handler(CallbackQueryHandler(delete_procedure_type_confirm, pattern='^pt_delete_confirm_'))
    application.add_handler(CallbackQueryHandler(delete_procedure_type_handler, pattern='^pt_delete_'))
    application.add_handler(CallbackQueryHandler(close_admin_dialog_button, pattern='^close_admin_dialog$'))

    # Обробники кнопок користувача
    application.add_handler(CallbackQueryHandler(user_my_applications, pattern='^user_my_applications$'))
    application.add_handler(CallbackQueryHandler(user_info, pattern='^user_info$'))
    application.add_handler(CallbackQueryHandler(user_back_to_menu, pattern='^user_back_to_menu$'))
    application.add_handler(CallbackQueryHandler(cancel_user_application, pattern='^cancel_app_'))

    # Обробники callback для управління заявками
    application.add_handler(CallbackQueryHandler(approve_application, pattern='^approve_'))
    application.add_handler(CallbackQueryHandler(reject_application, pattern='^reject_'))
    application.add_handler(CallbackQueryHandler(confirm_reject_primary, pattern='^confirm_reject_primary_'))
    application.add_handler(CallbackQueryHandler(cancel_reject_primary, pattern='^cancel_reject_primary_'))
    application.add_handler(CallbackQueryHandler(set_primary_application, pattern='^primary_'))
    application.add_handler(CallbackQueryHandler(confirm_cancel_primary, pattern='^confirm_cancel_primary_\\d+$'))
    application.add_handler(CallbackQueryHandler(cancel_primary_back, pattern='^cancel_primary_back_\\d+$'))
    application.add_handler(CallbackQueryHandler(cancel_application, pattern='^cancel_'))
    application.add_handler(CallbackQueryHandler(view_event_applications, pattern='^view_apps_'))

    # Обробник текстових команд меню адміністратора
    application.add_handler(MessageHandler(
        filters.TEXT & filters.Regex('^(📋 Заходи|⚙️)$') & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_admin_menu_text
    ))

    # Обробник текстових команд меню користувача
    application.add_handler(MessageHandler(
        filters.TEXT & filters.Regex('^(📋 Мої заявки|ℹ️ Інформація)$') & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_user_menu_text
    ))

    # Rate limiting middleware - найвищий пріоритет
    application.add_handler(TypeHandler(Update, rate_limit_check), group=-1)

    # Обробник повідомлень від кандидатів (пересилання в групу) - нижчий пріоритет
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forward_candidate_message), group=1)

    # Глобальний обробник помилок
    application.add_error_handler(error_handler)

    # Встановлення меню команд (тільки /start)
    async def post_init(app: Application) -> None:
        """Налаштування бота після ініціалізації"""
        await app.bot.set_my_commands([
            BotCommand("start", "Почати роботу з ботом")
        ])

    application.post_init = post_init

    # Налаштування graceful shutdown
    def signal_handler(sig, frame):
        logger.info("Отримано сигнал зупинки. Зупиняю бота...")
        application.stop_running()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Запуск бота
    logger.info("Бот запущено!")
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True  # Ігнорувати старі updates після перезапуску
        )
    except KeyboardInterrupt:
        logger.info("Бот зупинено користувачем")
    finally:
        logger.info("Завершення роботи бота")


if __name__ == '__main__':
    main()
