# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Огляд проекту

Телеграм бот на Python для автоматизації запису моделей на безкоштовні косметологічні процедури. Бот використовує python-telegram-bot v20.7 та SQLite для зберігання даних.

**Мова інтерфейсу:** Українська (весь текст боту має бути українською)

## Команди для роботи

### Запуск бота
```bash
python bot.py
# або
python3 bot.py
```

### Встановлення залежностей
```bash
pip install -r requirements.txt
```

### Налаштування середовища
1. Скопіювати `.env.example` в `.env`
2. Заповнити BOT_TOKEN, ADMIN_ID, CHANNEL_ID, GROUP_ID
3. ID каналу та групи мають починатися з мінуса!

## Архітектура

### Три основні компоненти:

1. **bot.py** - Головний файл з логікою бота
   - ConversationHandler для створення заходу (адмін)
   - ConversationHandler для подачі заявки (модель)
   - Callback handlers для управління заявками в групі

2. **database.py** - Робота з SQLite
   - Методи роботи з users, events, applications, application_photos
   - База створюється автоматично при першому запуску

3. **constants.py** - Константи та утиліти
   - PROCEDURE_TYPES - 7 типів процедур
   - generate_date_options() - генерує дати на 7 днів вперед
   - generate_time_slots() - генерує часові слоти 9:00-17:00 з інтервалом 10 хв

### Потік даних:

```
Адмін створює захід → Публікація в канал → Модель подає заявку →
→ Заявка в групу з хештегами → Адмін приймає/відхиляє →
→ Вибір основного кандидата → Автоматична відправка інструкцій
```

### ConversationHandler стани:

**Створення заходу (6 станів):**
- CREATE_EVENT_DATE → CREATE_EVENT_TIME → CREATE_EVENT_PROCEDURE →
→ CREATE_EVENT_PHOTO_NEEDED → CREATE_EVENT_COMMENT → CREATE_EVENT_CONFIRM

**Подача заявки (5 станів):**
- APPLY_FULL_NAME → APPLY_PHONE → APPLY_PHOTOS → APPLY_CONSENT → APPLY_CONFIRM

## Важливі деталі реалізації

### Фото від кандидатів
- **Адміністратор НЕ додає фото** при створенні заходу
- Адміністратор лише вибирає: "Чи потрібні фото від кандидатів?"
- Якщо `needs_photo=True`, кандидат **обов'язково** повинен додати мінімум 1 фото (максимум 3)
- Перевірка в `apply_photos_done()`: якщо фото обов'язкове, але не додано → помилка
- Кнопка "✅ Готово" завершує додавання фото

### Хештеги в групі
Заявки публікуються з двома хештегами для фільтрації:
- `#захід_{event_id}` - всі заявки на конкретний захід
- `#кандидат_{user_id}` - всі заявки від конкретного користувача

### Deep linking
Кнопка "Подати заявку" в каналі використовує URL:
```
https://t.me/{bot_username}?start=event_{event_id}
```
Обробляється в `start()` через `context.args`

### Callback data patterns
- `date_{date}` - вибір дати
- `time_{time}` - вибір часу
- `proc_{index}` - вибір процедури
- `approve_{application_id}` - прийняти заявку
- `reject_{application_id}` - відхилити заявку
- `primary_{application_id}` - встановити основним
- `view_apps_{event_id}` - переглянути заявки

### Збереження даних користувачів
Дані (ПІБ, телефон) зберігаються в таблиці `users` після першої заявки.
При наступних заявках пропонується використати збережені дані.

## Типові помилки та виправлення

### F-string з апострофами
❌ НЕПРАВИЛЬНО:
```python
f"✅ Фото {'обов\'язкове' if x else 'ні'}"
```

✅ ПРАВИЛЬНО:
```python
text = "обов'язкове" if x else "ні"
f"✅ Фото {text}"
```

### ID каналу/групи
Завжди з мінусом: `-1001234567890`

### Права бота
- В каналі: право "Публікувати повідомлення"
- В групі: базові права адміністратора

## База даних

SQLite з таблицями:
- `users` - user_id (PK), full_name, phone, is_blocked
- `events` - id (PK), date, time, procedure_type, needs_photo, comment, status, message_id
- `applications` - id (PK), event_id, user_id, full_name, phone, consent, status, is_primary
- `application_photos` - id (PK), application_id, file_id

**Важливо:** Таблиці `event_photos` немає! Фото додають лише кандидати.

## Додавання нових типів процедур

Редагувати список `PROCEDURE_TYPES` в `constants.py`
