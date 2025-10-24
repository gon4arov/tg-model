from datetime import datetime, timedelta

# Типи процедур
PROCEDURE_TYPES = [
    'Лазерна епіляція',
    'Видалення тату',
    'Видалення судин',
    'Видалення новоутворень',
    'Карбоновий пілінг обличчя',
    'Видалення ПМ губ',
    'Видалення стрілки'
]

# Дні тижня українською
DAYS_OF_WEEK = ['Нд', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб']

# Стани для ConversationHandler
(CREATE_EVENT_DATE, CREATE_EVENT_TIME, CREATE_EVENT_PROCEDURE,
 CREATE_EVENT_PHOTO_NEEDED, CREATE_EVENT_COMMENT,
 CREATE_EVENT_CONFIRM) = range(6)

(APPLY_FULL_NAME, APPLY_PHONE, APPLY_PHOTOS, APPLY_CONSENT, APPLY_CONFIRM) = range(100, 105)

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
    today = datetime.now()

    for i in range(7):
        date = today + timedelta(days=i)
        date_str = date.strftime('%Y-%m-%d')
        day_name = DAYS_OF_WEEK[date.weekday()]
        day_num = date.day
        month = date.month

        if i == 0:
            display = f"Сьогодні ({day_num}.{month:02d})"
        else:
            display = f"{day_name}, {day_num}.{month:02d}"

        options.append({'date': date_str, 'display': display})

    return options

TIME_SLOTS = generate_time_slots()
