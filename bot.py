import os
import re
import logging
import signal
import sys
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
    PicklePersistence
)
from telegram.request import HTTPXRequest
from telegram.error import Forbidden, BadRequest, TimedOut, NetworkError

from database import Database
from constants import (
    PROCEDURE_TYPES,
    TIME_SLOTS,
    generate_date_options,
    CREATE_EVENT_DATE,
    CREATE_EVENT_TIME,
    CREATE_EVENT_PROCEDURE,
    CREATE_EVENT_PHOTO_NEEDED,
    CREATE_EVENT_COMMENT,
    CREATE_EVENT_CONFIRM,
    APPLY_FULL_NAME,
    APPLY_PHONE,
    APPLY_PHOTOS,
    APPLY_CONFIRM,
    BLOCK_USER_ID
)

# Завантаження змінних середовища
load_dotenv()

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Ініціалізація бази даних
db = Database()

# Отримання конфігурації з .env
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
CHANNEL_ID = os.getenv('CHANNEL_ID', '')
GROUP_ID = os.getenv('GROUP_ID', '')


def is_admin(user_id: int) -> bool:
    """Перевірка чи користувач є адміністратором"""
    return user_id == ADMIN_ID


def format_date(date_str: str) -> str:
    """Форматування дати для відображення"""
    date = datetime.strptime(date_str, '%Y-%m-%d')
    return date.strftime('%d.%m.%Y')


def chunk_list(lst, n):
    """Розбиття списку на частини по n елементів"""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


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
            await update.effective_message.reply_text(
                "Вибачте, сталася помилка. Спробуйте ще раз або зверніться до адміністратора."
            )
    except Exception as e:
        logger.error(f"Could not send error message to user: {e}")


# ==================== КОМАНДИ ====================

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = False):
    """Відображення головного меню адміністратора"""
    keyboard = [
        [InlineKeyboardButton("Створити новий захід", callback_data="admin_create_event")],
        [InlineKeyboardButton("Переглянути заходи", callback_data="admin_manage_events")],
        [InlineKeyboardButton("Заблокувати користувача", callback_data="admin_block_user")]
    ]

    text = "Вітаю, адміністраторе!\n\nОберіть дію:"

    if edit_message and update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        message = update.callback_query.message if update.callback_query else update.message
        await message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка команди /start"""
    user_id = update.effective_user.id
    db.create_user(user_id)

    # Перевірка чи користувач заблокований
    user = db.get_user(user_id)
    if user and user.get('is_blocked'):
        await update.message.reply_text("Вибачте, ваш доступ до бота заблоковано.")
        return ConversationHandler.END

    # Перевірка deep link для подачі заявки
    if context.args and context.args[0].startswith('event_'):
        try:
            event_id = int(context.args[0].split('_')[1])
            # Перевірити чи існує захід
            event = db.get_event(event_id)
            if not event:
                await update.message.reply_text("Захід не знайдено або вже завершено.")
                return
            if event['status'] != 'published':
                await update.message.reply_text("Цей захід більше не приймає заявки.")
                return
            context.user_data['apply_event_id'] = event_id
            return await apply_event_start(update, context)
        except (ValueError, IndexError):
            await update.message.reply_text("Невірне посилання на захід.")
            return

    if is_admin(user_id):
        await show_admin_menu(update, context)
    else:
        await update.message.reply_text(
            "Вітаємо!\n\n"
            "Цей бот допоможе вам записатися на косметологічні процедури.\n\n"
            "Щоб подати заявку на участь, натисніть на повідомлення про захід в нашому каналі."
        )


async def admin_create_event_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка кнопки 'Створити захід'"""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text("Немає доступу")
        return ConversationHandler.END

    await query.edit_message_text("Створюємо новий захід...")

    # Викликаємо логіку створення заходу
    context.user_data.clear()
    context.user_data['event'] = {}

    date_options = generate_date_options()
    # Групуємо дати по 4 в рядок (4 стовпчики)
    date_buttons = [InlineKeyboardButton(opt['display'], callback_data=f"date_{opt['date']}")
                    for opt in date_options]
    keyboard = list(chunk_list(date_buttons, 4))
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel")])

    await query.message.reply_text(
        "Оберіть дату заходу:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_EVENT_DATE


async def admin_manage_events_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка кнопки 'Переглянути заходи'"""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text("Немає доступу")
        return

    events = db.get_active_events()

    if not events:
        keyboard = [[InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")]]
        await query.edit_message_text(
            "Немає активних заходів",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    message = "Активні заходи:\n\n"
    for event in events:
        message += f"ID {event['id']} | {event['procedure_type']}\n"
        message += f"Дата: {format_date(event['date'])} {event['time']}\n\n"

    keyboard = [[InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")]]
    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_block_user_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка кнопки 'Заблокувати користувача'"""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text("Немає доступу")
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
        await update.message.reply_text(
            f"✅ Користувача {user_id_to_block} заблоковано",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except ValueError:
        keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel_block")]]
        await update.message.reply_text(
            "❌ Невірний ID користувача. Спробуйте ще раз:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return BLOCK_USER_ID

    return ConversationHandler.END


async def cancel_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скасування блокування користувача"""
    query = update.callback_query
    await query.answer()

    await show_admin_menu(update, context, edit_message=True)

    return ConversationHandler.END


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повернення до головного меню"""
    query = update.callback_query
    await query.answer()

    await show_admin_menu(update, context, edit_message=True)


# ==================== СТВОРЕННЯ ЗАХОДУ (АДМІН) ====================

async def create_event_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Початок створення заходу"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Немає доступу")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data['event'] = {}

    date_options = generate_date_options()
    # Групуємо дати по 4 в рядок (4 стовпчики)
    date_buttons = [InlineKeyboardButton(opt['display'], callback_data=f"date_{opt['date']}")
                    for opt in date_options]
    keyboard = list(chunk_list(date_buttons, 4))
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel")])

    await update.message.reply_text(
        "Оберіть дату заходу:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_EVENT_DATE


async def show_date_selection(query, context: ContextTypes.DEFAULT_TYPE):
    """Показати вибір дати"""
    date_options = generate_date_options()
    # Групуємо дати по 4 в рядок (4 стовпчики)
    date_buttons = [InlineKeyboardButton(opt['display'], callback_data=f"date_{opt['date']}")
                    for opt in date_options]
    keyboard = list(chunk_list(date_buttons, 4))
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel")])

    if query:
        await query.edit_message_text(
            "Оберіть дату заходу:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    return CREATE_EVENT_DATE


async def create_event_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка вибору дати"""
    query = update.callback_query
    await query.answer()

    # Якщо це повернення назад, просто показуємо вибір дати
    if query.data == "back_to_date":
        return await show_date_selection(query, context)

    date = query.data.split('_', 1)[1]
    context.user_data['event']['date'] = date

    await query.edit_message_text("Дату обрано")

    # Показати часові слоти по 5 в ряд (5 стовпчиків)
    keyboard = list(chunk_list(
        [InlineKeyboardButton(time, callback_data=f"time_{time}") for time in TIME_SLOTS],
        5
    ))
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_date")])
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel")])

    await query.message.reply_text(
        "Оберіть час заходу:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_EVENT_TIME


async def show_time_selection(query, context: ContextTypes.DEFAULT_TYPE):
    """Показати вибір часу"""
    keyboard = list(chunk_list(
        [InlineKeyboardButton(time, callback_data=f"time_{time}") for time in TIME_SLOTS],
        5
    ))
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_date")])
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel")])

    await query.edit_message_text(
        "Оберіть час заходу:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_EVENT_TIME


async def create_event_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка вибору часу"""
    query = update.callback_query
    await query.answer()

    # Якщо це повернення назад з екрану процедур, просто показуємо вибір часу
    if query.data == "back_to_time":
        return await show_time_selection(query, context)

    time = query.data.split('_', 1)[1]
    context.user_data['event']['time'] = time

    await query.edit_message_text("Час обрано")

    # Показати типи процедур
    keyboard = [[InlineKeyboardButton(ptype, callback_data=f"proc_{i}")]
                for i, ptype in enumerate(PROCEDURE_TYPES)]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_time")])
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel")])

    await query.message.reply_text(
        "Оберіть тип процедури:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_EVENT_PROCEDURE


async def show_procedure_selection(query, context: ContextTypes.DEFAULT_TYPE):
    """Показати вибір процедури"""
    keyboard = [[InlineKeyboardButton(ptype, callback_data=f"proc_{i}")]
                for i, ptype in enumerate(PROCEDURE_TYPES)]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_time")])
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel")])

    await query.edit_message_text(
        "Оберіть тип процедури:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_EVENT_PROCEDURE


async def create_event_procedure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка вибору процедури"""
    query = update.callback_query
    await query.answer()

    # Якщо це повернення назад, просто показуємо вибір процедури
    if query.data == "back_to_procedure":
        return await show_procedure_selection(query, context)

    proc_index = int(query.data.split('_')[1])
    context.user_data['event']['procedure'] = PROCEDURE_TYPES[proc_index]

    await query.edit_message_text("Тип процедури обрано")

    keyboard = [
        [
            InlineKeyboardButton("✅ Так", callback_data="photo_yes"),
            InlineKeyboardButton("❌ Ні", callback_data="photo_no")
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_procedure")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
    ]

    await query.message.reply_text(
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
        [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
    ]

    await query.edit_message_text(
        "Чи потрібно кандидатам надавати фото зони?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_EVENT_PHOTO_NEEDED


async def create_event_photo_needed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка необхідності фото"""
    query = update.callback_query
    await query.answer()

    # Якщо це повернення назад, просто показуємо вибір фото
    if query.data == "back_to_photo":
        return await show_photo_needed_selection(query, context)

    needs_photo = query.data == "photo_yes"
    context.user_data['event']['needs_photo'] = needs_photo

    photo_text = "буде обов'язковим для кандидатів" if needs_photo else "не потрібне"
    await query.edit_message_text(f"Фото {photo_text}")

    keyboard = [
        [InlineKeyboardButton("⏭ Пропустити", callback_data="skip_comment")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_photo")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
    ]

    await query.message.reply_text(
        "Додайте коментар до заходу (необов'язково).\n\n"
        "Якщо коментар не потрібен, натисніть 'Пропустити'",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_EVENT_COMMENT


async def show_comment_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показати екран введення коментаря"""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("⏭ Пропустити", callback_data="skip_comment")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_photo")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
    ]

    await query.edit_message_text(
        "Додайте коментар до заходу (необов'язково).\n\n"
        "Якщо коментар не потрібен, натисніть 'Пропустити'",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CREATE_EVENT_COMMENT


async def create_event_comment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка текстового коментаря"""
    context.user_data['event']['comment'] = update.message.text
    await update.message.reply_text("Коментар додано")
    return await show_event_summary(update, context)


async def show_event_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показати підсумок заходу"""
    event = context.user_data['event']

    photo_required = "Обов'язкове" if event['needs_photo'] else "Не потрібне"

    summary = (
        f"Підсумок заходу:\n\n"
        f"Дата: {event['date']}\n"
        f"Час: {event['time']}\n"
        f"Процедура: {event['procedure']}\n"
        f"Фото від кандидатів: {photo_required}\n"
        f"Коментар: {event.get('comment', 'Відсутній')}"
    )

    keyboard = [
        [InlineKeyboardButton("✅ Підтвердити і опублікувати", callback_data="confirm_event")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_comment")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
    ]

    if update.callback_query:
        await update.callback_query.message.reply_text(
            summary,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            summary,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    return CREATE_EVENT_CONFIRM


async def confirm_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Підтвердження і збереження заходу"""
    query = update.callback_query
    await query.answer()

    event = context.user_data['event']

    try:
        # Зберегти захід
        event_id = db.create_event(
            date=event['date'],
            time=event['time'],
            procedure_type=event['procedure'],
            needs_photo=event['needs_photo'],
            comment=event.get('comment')
        )

        await query.edit_message_text("Захід створено!")

        # Опублікувати в канал
        await publish_event_to_channel(context, event_id)

        await query.message.reply_text(
            f"Захід \"{event['procedure']} {event['date']} на {event['time']}\" опубліковано в каналі!"
        )

        # Показати головне меню
        context.user_data.clear()
        await show_admin_menu(update, context, edit_message=False)

    except Exception as e:
        logger.error(f"Помилка створення заходу: {e}")
        await query.message.reply_text("Помилка при створенні заходу")

        # Показати головне меню навіть при помилці
        context.user_data.clear()
        await show_admin_menu(update, context, edit_message=False)

    return ConversationHandler.END


async def publish_event_to_channel(context: ContextTypes.DEFAULT_TYPE, event_id: int):
    """Публікація заходу в канал"""
    event = db.get_event(event_id)

    bot_username = (await context.bot.get_me()).username

    message_text = (
        f"БЕЗКОШТОВНО!\n\n"
        f"Процедура: {event['procedure_type']}\n"
        f"Дата: {format_date(event['date'])}\n"
        f"Час: {event['time']}\n"
    )

    if event['comment']:
        message_text += f"\n{event['comment']}\n"

    if event['needs_photo']:
        message_text += f"\nПотрібне фото зони!\n"

    message_text += "\nДля подачі заявки натисніть кнопку нижче:"

    keyboard = [[InlineKeyboardButton(
        "Подати заявку",
        url=f"https://t.me/{bot_username}?start=event_{event_id}"
    )]]

    message = await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=message_text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    db.update_event_message_id(event_id, message.message_id)
    db.update_event_status(event_id, 'published')


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скасування діалогу"""
    query = update.callback_query
    user_id = update.effective_user.id

    if query:
        await query.answer()
        await query.edit_message_text("Скасовано")
    else:
        await update.message.reply_text("Скасовано")

    context.user_data.clear()

    # Якщо це адмін, показуємо головне меню
    if is_admin(user_id):
        if query:
            await show_admin_menu(update, context, edit_message=False)
        else:
            # Якщо скасування прийшло через текстове повідомлення
            await update.message.reply_text("Використовуйте /start для повернення в меню")

    return ConversationHandler.END


# ==================== ПОДАЧА ЗАЯВКИ (МОДЕЛЬ) ====================

async def apply_event_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Початок подачі заявки"""
    event_id = context.user_data.get('apply_event_id')

    if not event_id:
        await update.message.reply_text("Захід не знайдено")
        return ConversationHandler.END

    user_id = update.effective_user.id

    # Перевірка блокування
    if db.is_user_blocked(user_id):
        await update.message.reply_text("Ви заблоковані і не можете подавати заявки.")
        return ConversationHandler.END

    # Перевірка існування заходу
    event = db.get_event(event_id)
    if not event or event['status'] != 'published':
        await update.message.reply_text("Захід не знайдено або він вже не активний")
        return ConversationHandler.END

    context.user_data['application'] = {'event_id': event_id, 'photos': []}

    # Перевірка чи є збережені дані користувача
    user = db.get_user(user_id)

    if user and user['full_name'] and user['phone']:
        keyboard = [
            [InlineKeyboardButton("✅ Так", callback_data="use_saved_data")],
            [InlineKeyboardButton("✏️ Ввести нові", callback_data="enter_new_data")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
        ]

        await update.message.reply_text(
            f"У нас є ваші дані:\n\n"
            f"ПІБ: {user['full_name']}\n"
            f"Телефон: {user['phone']}\n\n"
            f"Використати ці дані?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return APPLY_FULL_NAME
    else:
        await update.message.reply_text(
            "Введіть ваше повне ім'я (Прізвище Ім'я По батькові):"
        )
        return APPLY_FULL_NAME


async def apply_use_saved_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Використання збережених даних"""
    query = update.callback_query
    await query.answer()

    user = db.get_user(update.effective_user.id)
    context.user_data['application']['full_name'] = user['full_name']
    context.user_data['application']['phone'] = user['phone']

    await query.edit_message_text("Дані завантажено")

    event = db.get_event(context.user_data['application']['event_id'])

    if event['needs_photo']:
        keyboard = [
            [InlineKeyboardButton("✅ Готово", callback_data="photos_done")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
        ]
        await query.message.reply_text(
            "Надішліть фото зони процедури (до 3 фото).\n\n"
            "Після завантаження всіх фото натисніть кнопку 'Готово'",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return APPLY_PHOTOS
    else:
        return await show_consent(query.message, context)


async def apply_enter_new_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввести нові дані"""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("Введемо нові дані")
    await query.message.reply_text("Введіть ваше повне ім'я (Прізвище Ім'я По батькові):")
    return APPLY_FULL_NAME


async def apply_full_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка ПІБ"""
    context.user_data['application']['full_name'] = update.message.text
    await update.message.reply_text("ПІБ збережено")
    await update.message.reply_text("Введіть ваш номер телефону:")
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
    phone = update.message.text

    # Перевірка українського номера
    if not validate_ukrainian_phone(phone):
        await update.message.reply_text(
            "Невірний формат телефону.\n\n"
            "Приклади правильного формату:\n"
            "+380501234567\n"
            "0501234567\n"
            "050 123 45 67\n\n"
            "Введіть номер українського оператора:"
        )
        return APPLY_PHONE

    context.user_data['application']['phone'] = phone
    await update.message.reply_text("Телефон збережено")

    # Зберегти дані користувача
    db.update_user(
        update.effective_user.id,
        context.user_data['application']['full_name'],
        phone
    )

    event = db.get_event(context.user_data['application']['event_id'])

    if event['needs_photo']:
        keyboard = [
            [InlineKeyboardButton("✅ Готово", callback_data="photos_done")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
        ]
        await update.message.reply_text(
            "Надішліть фото зони процедури (до 3 фото).\n\n"
            "Після завантаження всіх фото натисніть кнопку 'Готово'",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return APPLY_PHOTOS
    else:
        return await show_consent(update.message, context)


async def apply_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка фото від моделі"""
    if 'application' not in context.user_data:
        await update.message.reply_text("Сесія застаріла. Будь ласка, почніть заново з посилання в каналі.")
        return ConversationHandler.END

    photos = context.user_data['application'].get('photos', [])

    if len(photos) >= 3:
        await update.message.reply_text("Можна додати не більше 3 фото")
        return APPLY_PHOTOS

    file_id = update.message.photo[-1].file_id
    photos.append(file_id)
    context.user_data['application']['photos'] = photos

    keyboard = [
        [InlineKeyboardButton("✅ Готово", callback_data="photos_done")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
    ]

    await update.message.reply_text(
        f"Фото додано ({len(photos)}/3)\n\n"
        "Надішліть ще фото або натисніть 'Готово' для завершення",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return APPLY_PHOTOS


async def apply_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершення додавання фото"""
    query = update.callback_query
    await query.answer()

    event = db.get_event(context.user_data['application']['event_id'])
    photos = context.user_data['application'].get('photos', [])

    if event['needs_photo'] and len(photos) == 0:
        await query.message.reply_text("Для цього заходу фото є обов'язковим. Додайте хоча б одне фото.")
        return APPLY_PHOTOS

    await query.edit_message_text("Фото прийнято")
    return await show_application_summary(query.message, context)


async def show_application_summary(message, context: ContextTypes.DEFAULT_TYPE):
    """Показати підсумок заявки зі згодою"""
    app = context.user_data['application']
    event = db.get_event(app['event_id'])

    summary = (
        f"Підсумок заявки:\n\n"
        f"Процедура: {event['procedure_type']}\n"
        f"Дата: {format_date(event['date'])}\n"
        f"Час: {event['time']}\n\n"
        f"ПІБ: {app['full_name']}\n"
        f"Телефон: {app['phone']}\n"
        f"Фото додано: {len(app.get('photos', []))}\n\n"
        f"Підтверджую, що:\n"
        f"• Мені виповнилося 18 років\n"
        f"• Я усвідомлюю характер процедури\n"
        f"• Я усвідомлюю можливі наслідки процедури"
    )

    keyboard = [
        [InlineKeyboardButton("✅ Підтвердити заявку", callback_data="submit_application")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
    ]

    await message.reply_text(
        summary,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return APPLY_CONFIRM


async def submit_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Відправити заявку"""
    query = update.callback_query
    await query.answer()

    app = context.user_data['application']

    try:
        # Зберегти заявку
        application_id = db.create_application(
            event_id=app['event_id'],
            user_id=update.effective_user.id,
            full_name=app['full_name'],
            phone=app['phone']
        )

        # Зберегти фото
        for file_id in app.get('photos', []):
            db.add_application_photo(application_id, file_id)

        await query.edit_message_text(
            "Вашу заявку успішно подано!\n\n"
            "Очікуйте на розгляд адміністратором."
        )

        # Опублікувати в групу
        await publish_application_to_group(context, application_id)

    except Exception as e:
        logger.error(f"Помилка подачі заявки: {e}")
        await query.message.reply_text("Помилка при подачі заявки")

    context.user_data.clear()
    return ConversationHandler.END


async def publish_application_to_group(context: ContextTypes.DEFAULT_TYPE, application_id: int):
    """Публікація заявки в групу"""
    app = db.get_application(application_id)
    event = db.get_event(app['event_id'])
    photos = db.get_application_photos(application_id)

    message_text = (
        f"Нова заявка №{application_id}\n\n"
        f"#захід_{event['id']} #кандидат_{app['user_id']}\n\n"
        f"Процедура: {event['procedure_type']}\n"
        f"Дата: {format_date(event['date'])} {event['time']}\n\n"
        f"ПІБ: {app['full_name']}\n"
        f"Телефон: {app['phone']}"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Прийняти", callback_data=f"approve_{application_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{application_id}")
        ],
        [InlineKeyboardButton("👤 Профіль кандидата", url=f"tg://user?id={app['user_id']}")]
    ]

    if photos:
        if len(photos) == 1:
            message = await context.bot.send_photo(
                chat_id=GROUP_ID,
                photo=photos[0],
                caption=message_text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            media = [InputMediaPhoto(media=photo_id, caption=message_text if i == 0 else '')
                    for i, photo_id in enumerate(photos)]
            messages = await context.bot.send_media_group(chat_id=GROUP_ID, media=media)
            message = messages[0]
            await context.bot.send_message(
                chat_id=GROUP_ID,
                text=f"Заявка №{application_id}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    else:
        message = await context.bot.send_message(
            chat_id=GROUP_ID,
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    db.update_application_group_message_id(application_id, message.message_id)


# ==================== УПРАВЛІННЯ ЗАЯВКАМИ ====================

async def approve_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прийняти заявку"""
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer("Немає доступу", show_alert=True)
        return

    await query.answer()

    application_id = int(query.data.split('_')[1])
    app = db.get_application(application_id)

    db.update_application_status(application_id, 'approved')

    keyboard = [
        [
            InlineKeyboardButton("⭐ Обрати основним", callback_data=f"primary_{application_id}"),
            InlineKeyboardButton("Заявки на захід", callback_data=f"view_apps_{app['event_id']}")
        ],
        [InlineKeyboardButton("👤 Профіль кандидата", url=f"tg://user?id={app['user_id']}")]
    ]

    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    # Повідомити користувача
    try:
        await context.bot.send_message(
            chat_id=app['user_id'],
            text="Вашу заявку схвалено!\n\nОчікуйте на додаткову інформацію."
        )
    except Forbidden:
        db.block_user(app['user_id'])
        logger.info(f"User {app['user_id']} blocked the bot - marked as blocked in DB")
    except Exception as e:
        logger.error(f"Не вдалося надіслати повідомлення користувачу: {e}")


async def reject_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Відхилити заявку"""
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer("Немає доступу", show_alert=True)
        return

    await query.answer()

    application_id = int(query.data.split('_')[1])
    app = db.get_application(application_id)

    db.update_application_status(application_id, 'rejected')

    await query.edit_message_reply_markup(reply_markup=None)

    # Повідомити користувача
    try:
        await context.bot.send_message(
            chat_id=app['user_id'],
            text="На жаль, вашу заявку відхилено."
        )
    except Exception as e:
        logger.error(f"Не вдалося надіслати повідомлення користувачу: {e}")


async def set_primary_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Встановити заявку як основну"""
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer("Немає доступу", show_alert=True)
        return

    await query.answer("Встановлено основним кандидатом")

    application_id = int(query.data.split('_')[1])
    app = db.get_application(application_id)
    event = db.get_event(app['event_id'])

    db.set_primary_application(application_id)

    # Надіслати інструкцію
    instruction = (
        f"Вітаємо! Ви обрані основним кандидатом!\n\n"
        f"Процедура: {event['procedure_type']}\n"
        f"Дата: {format_date(event['date'])}\n"
        f"Час: {event['time']}\n\n"
        f"Інструкції:\n"
        f"• Будь ласка, прийдіть за 10 хвилин до початку\n"
        f"• Майте при собі документ, що підтверджує особу\n"
        f"• У разі неможливості прийти, повідомте нас заздалегідь\n\n"
        f"До зустрічі! "
    )

    try:
        await context.bot.send_message(chat_id=app['user_id'], text=instruction)
        await query.message.reply_text("Інструкцію надіслано кандидату")
    except Exception as e:
        logger.error(f"Помилка відправки інструкції: {e}")
        await query.message.reply_text("Не вдалося надіслати інструкцію")


async def view_event_applications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переглянути заявки на захід"""
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer("Немає доступу", show_alert=True)
        return

    event_id = int(query.data.split('_')[2])
    applications = db.get_approved_applications(event_id)

    if not applications:
        await query.answer("Немає затверджених заявок", show_alert=True)
        return

    await query.answer()

    message = "Затверджені заявки на захід:\n\n"

    for i, app in enumerate(applications):
        status = "Основний" if app['is_primary'] else f"{i + 1}."
        message += f"{status} {app['full_name']} - {app['phone']}\n"

    await query.message.reply_text(message)


# ==================== ПОВІДОМЛЕННЯ КАНДИДАТУ ====================

async def forward_candidate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пересилання повідомлень від кандидатів в групу"""
    user_id = update.effective_user.id

    # Перевірка що це не адмін
    if is_admin(user_id):
        return

    # Ігнорувати якщо це приватний чат (conversation активний)
    # Тільки обробляємо повідомлення які НЕ в контексті conversation
    if context.user_data.get('message_user_id'):
        return

    # Перевірка що користувач є в базі (подавав заявку)
    user = db.get_user(user_id)
    if not user or not user['full_name']:
        return

    # Переслати повідомлення в групу
    try:
        message_text = (
            f"💬 Повідомлення від кандидата:\n\n"
            f"👤 {user['full_name']}\n"
            f"📱 {user['phone']}\n"
            f"🆔 User ID: {user_id}\n\n"
            f"Текст: {update.message.text}"
        )

        keyboard = [[InlineKeyboardButton("👤 Профіль кандидата", url=f"tg://user?id={user_id}")]]

        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Помилка пересилання повідомлення: {e}")


# ==================== MAIN ====================

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

    # Обробник створення заходу
    create_event_handler = ConversationHandler(
        entry_points=[
            CommandHandler('create_event', create_event_start),
            CommandHandler('new_event', create_event_start),
            CallbackQueryHandler(admin_create_event_button, pattern='^admin_create_event$')
        ],
        states={
            CREATE_EVENT_DATE: [
                CallbackQueryHandler(create_event_date, pattern='^date_'),
                CallbackQueryHandler(create_event_date, pattern='^back_to_date$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            CREATE_EVENT_TIME: [
                CallbackQueryHandler(create_event_time, pattern='^time_'),
                CallbackQueryHandler(create_event_date, pattern='^back_to_date$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            CREATE_EVENT_PROCEDURE: [
                CallbackQueryHandler(create_event_procedure, pattern='^proc_'),
                CallbackQueryHandler(create_event_time, pattern='^back_to_time$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            CREATE_EVENT_PHOTO_NEEDED: [
                CallbackQueryHandler(create_event_photo_needed, pattern='^photo_'),
                CallbackQueryHandler(create_event_procedure, pattern='^back_to_procedure$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            CREATE_EVENT_COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, create_event_comment_text),
                CallbackQueryHandler(show_event_summary, pattern='^skip_comment$'),
                CallbackQueryHandler(create_event_photo_needed, pattern='^back_to_photo$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            CREATE_EVENT_CONFIRM: [
                CallbackQueryHandler(confirm_event, pattern='^confirm_event$'),
                CallbackQueryHandler(show_comment_input, pattern='^back_to_comment$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel, pattern='^cancel$')],
        name="create_event_conversation",
        persistent=True
    )

    # Обробник подачі заявки
    apply_event_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            APPLY_FULL_NAME: [
                CallbackQueryHandler(apply_use_saved_data, pattern='^use_saved_data$'),
                CallbackQueryHandler(apply_enter_new_data, pattern='^enter_new_data$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, apply_full_name),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            APPLY_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, apply_phone),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            APPLY_PHOTOS: [
                MessageHandler(filters.PHOTO, apply_photo),
                CallbackQueryHandler(apply_photos_done, pattern='^photos_done$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            APPLY_CONFIRM: [
                CallbackQueryHandler(submit_application, pattern='^submit_application$'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel, pattern='^cancel$')],
        name="apply_event_conversation",
        persistent=True
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
        persistent=True
    )

    # Додати обробники (ConversationHandlers мають вищий пріоритет - group 0)
    application.add_handler(create_event_handler, group=0)
    application.add_handler(apply_event_handler, group=0)
    application.add_handler(block_user_handler, group=0)

    # Обробники кнопок адміністратора
    application.add_handler(CallbackQueryHandler(back_to_menu, pattern='^back_to_menu$'))
    application.add_handler(CallbackQueryHandler(admin_manage_events_button, pattern='^admin_manage_events$'))

    # Обробники callback для управління заявками
    application.add_handler(CallbackQueryHandler(approve_application, pattern='^approve_'))
    application.add_handler(CallbackQueryHandler(reject_application, pattern='^reject_'))
    application.add_handler(CallbackQueryHandler(set_primary_application, pattern='^primary_'))
    application.add_handler(CallbackQueryHandler(view_event_applications, pattern='^view_apps_'))

    # Обробник повідомлень від кандидатів (пересилання в групу) - нижчий пріоритет
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forward_candidate_message), group=1)

    # Глобальний обробник помилок
    application.add_error_handler(error_handler)

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
