from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Типи процедур
PROCEDURE_TYPES = [
    'Лазерна епіляція',
    'Видалення тату',
    'Видалення судин',
    'Видалення новоутворень',
    'Карбоновий пілінг обличчя',
    'Видалення ПМ губ',
    'Видалення ПМ брів',
    'Видалення стрілки'
]

# Дні тижня українською (понеділок = 0, неділя = 6, відповідно до weekday())
DAYS_OF_WEEK = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Нд']

# Часовий пояс України
UKRAINE_TZ = ZoneInfo("Europe/Kyiv")

# Стани для ConversationHandler
(CREATE_EVENT_DATE, CREATE_EVENT_TIME, CREATE_EVENT_PROCEDURE,
 CREATE_EVENT_PHOTO_NEEDED, CREATE_EVENT_COMMENT,
 CREATE_EVENT_CONFIRM, CREATE_EVENT_REVIEW) = range(7)

(APPLY_SELECT_EVENTS, APPLY_FULL_NAME, APPLY_PHONE, APPLY_PHOTOS, APPLY_CONFIRM) = range(100, 105)

MESSAGE_TO_CANDIDATE = 200
BLOCK_USER_ID = 201

# Стани для керування типами процедур
(ADD_PROCEDURE_TYPE_NAME, EDIT_PROCEDURE_TYPE_NAME) = range(300, 302)

# Стан для очистки БД
CLEAR_DB_PASSWORD = 400

def generate_time_slots():
    """Генерація часових слотів від 9:00 до 17:00 з інтервалом 10 хвилин"""
    slots = []
    for hour in range(9, 18):
        for minute in range(0, 60, 10):
            if hour == 17 and minute > 0:
                break
            slots.append(f"{hour:02d}:{minute:02d}")
    return slots

def generate_date_options():
    """Генерація дат на найближчі 7 днів"""
    options = []
    today = datetime.now(UKRAINE_TZ)

    for i in range(7):
        date = today + timedelta(days=i)
        date_str = date.strftime('%Y-%m-%d')
        day_name = DAYS_OF_WEEK[date.weekday()]
        day_num = date.day
        month = date.month

        if i == 0:
            display = "Сьогодні"
        else:
            display = f"{day_name}, {day_num}.{month:02d}"

        options.append({'date': date_str, 'display': display})

    return options

TIME_SLOTS = generate_time_slots()
