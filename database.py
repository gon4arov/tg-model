import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any

# Часовий пояс України
UKRAINE_TZ = ZoneInfo("Europe/Kyiv")

class Database:
    def __init__(self, db_path: str = "bot.db"):
        self.db_path = db_path
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def init_db(self):
        """Ініціалізація таблиць бази даних"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Таблиця користувачів
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                phone TEXT,
                is_blocked INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Таблиця заходів
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                procedure_type TEXT NOT NULL,
                needs_photo INTEGER DEFAULT 0,
                comment TEXT,
                status TEXT DEFAULT 'draft',
                message_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Таблиця заявок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                consent INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                is_primary INTEGER DEFAULT 0,
                group_message_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (event_id) REFERENCES events(id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')

        # Таблиця фото заявок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS application_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                FOREIGN KEY (application_id) REFERENCES applications(id)
            )
        ''')

        # Таблиця типів процедур
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS procedure_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Таблиця повідомлень по днях
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS day_messages (
                date TEXT PRIMARY KEY,
                message_id INTEGER
            )
        ''')

        # Створення індексів для оптимізації запитів
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_applications_event_id ON applications(event_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_applications_user_id ON applications(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_applications_group_message_id ON applications(group_message_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_events_date ON events(date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_events_status ON events(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_application_photos_application_id ON application_photos(application_id)')

        conn.commit()
        conn.close()

        # Ініціалізувати типи процедур якщо таблиця порожня
        self._init_procedure_types()
        self._ensure_schema_upgrades()

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        """Додати колонку до таблиці, якщо вона відсутня"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table})")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if column not in existing_columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            conn.commit()
        conn.close()

    def _ensure_schema_upgrades(self) -> None:
        """Перевірити та застосувати зміни до схеми БД (зворотна сумісність)"""
        self._add_column_if_missing('applications', 'position', 'INTEGER DEFAULT 0')
        self._add_column_if_missing('events', 'applications_message_id', 'INTEGER')

    # Методи для роботи з користувачами
    def create_user(self, user_id: int) -> None:
        """Створити користувача"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
        conn.commit()
        conn.close()

    def get_user(self, user_id: int) -> Optional[Dict]:
        """Отримати користувача"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def update_user(self, user_id: int, full_name: str, phone: str) -> None:
        """Оновити дані користувача"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users SET full_name = ?, phone = ? WHERE user_id = ?
        ''', (full_name, phone, user_id))
        conn.commit()
        conn.close()

    def block_user(self, user_id: int) -> None:
        """Заблокувати користувача"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET is_blocked = 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()

    def is_user_blocked(self, user_id: int) -> bool:
        """Перевірити чи заблокований користувач"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT is_blocked FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result[0] == 1 if result else False

    # Методи для роботи з заходами
    def create_event(self, date: str, time: str, procedure_type: str,
                     needs_photo: bool, comment: Optional[str] = None) -> int:
        """Створити захід"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO events (date, time, procedure_type, needs_photo, comment, status)
            VALUES (?, ?, ?, ?, ?, 'confirmed')
        ''', (date, time, procedure_type, 1 if needs_photo else 0, comment))
        event_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return event_id

    def get_event(self, event_id: int) -> Optional[Dict]:
        """Отримати захід"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM events WHERE id = ?', (event_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def update_event_status(self, event_id: int, status: str) -> None:
        """Оновити статус заходу"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE events SET status = ? WHERE id = ?', (status, event_id))
        conn.commit()
        conn.close()

    def update_event_message_id(self, event_id: int, message_id: int) -> None:
        """Оновити ID повідомлення заходу"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE events SET message_id = ? WHERE id = ?', (message_id, event_id))
        conn.commit()
        conn.close()

    def update_event_applications_message_id(self, event_id: int, message_id: Optional[int]) -> None:
        """Зберегти або очистити ID групового повідомлення по заявках"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE events SET applications_message_id = ? WHERE id = ?',
            (message_id, event_id)
        )
        conn.commit()
        conn.close()

    def get_event_applications_message_id(self, event_id: int) -> Optional[int]:
        """Отримати ID групового повідомлення із заявками"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT applications_message_id FROM events WHERE id = ?', (event_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else None

    def get_active_events(self) -> List[Dict]:
        """Отримати активні заходи (від сьогодні)"""
        today = datetime.now(UKRAINE_TZ).strftime('%Y-%m-%d')

        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM events
            WHERE status = 'published' AND date >= ?
            ORDER BY date, time
        ''', (today,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_events_by_ids(self, event_ids: List[int]) -> List[Dict]:
        """Отримати перелік заходів за списком ID"""
        if not event_ids:
            return []

        placeholders = ','.join('?' for _ in event_ids)
        query = f'''
            SELECT *
            FROM events
            WHERE id IN ({placeholders})
        '''

        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query, tuple(event_ids))
        rows = cursor.fetchall()
        conn.close()

        events = [dict(row) for row in rows]
        # Сортуємо за датою, часом та ID для стабільного порядку
        events.sort(key=lambda item: (item['date'], item['time'], item['id']))
        return events

    def get_past_events(self) -> List[Dict]:
        """Отримати останні 10 минулих заходів (до сьогодні)"""
        today = datetime.now(UKRAINE_TZ).strftime('%Y-%m-%d')

        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM events
            WHERE status = 'published' AND date < ?
            ORDER BY date DESC, time DESC
            LIMIT 10
        ''', (today,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_events_by_date(self, date: str) -> List[Dict]:
        """Отримати всі заходи на конкретну дату"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT *
            FROM events
            WHERE date = ? AND status != 'cancelled'
            ORDER BY time
        ''', (date,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    # Методи для роботи з заявками
    def create_application(self, event_id: int, user_id: int,
                          full_name: str, phone: str) -> int:
        """Створити заявку"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO applications (event_id, user_id, full_name, phone, consent, status)
            VALUES (?, ?, ?, ?, 1, 'pending')
        ''', (event_id, user_id, full_name, phone))
        application_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return application_id

    def get_application(self, application_id: int) -> Optional[Dict]:
        """Отримати заявку"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM applications WHERE id = ?', (application_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def update_application_status(self, application_id: int, status: str) -> None:
        """Оновити статус заявки та прапорець основного кандидата"""
        conn = self.get_connection()
        cursor = conn.cursor()
        is_primary = 1 if status == 'primary' else 0
        cursor.execute(
            'UPDATE applications SET status = ?, is_primary = ? WHERE id = ?',
            (status, is_primary, application_id)
        )
        conn.commit()
        conn.close()

    def set_primary_application(self, application_id: int) -> None:
        """Позначити заявку основною"""
        self.update_application_status(application_id, 'primary')

    def get_approved_applications(self, event_id: int) -> List[Dict]:
        """Отримати затверджені заявки на захід"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM applications
            WHERE event_id = ? AND status = 'approved'
            ORDER BY is_primary DESC, created_at
        ''', (event_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_applications_by_event(self, event_id: int) -> List[Dict]:
        """Отримати всі заявки на захід (незалежно від статусу)"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT *
            FROM applications
            WHERE event_id = ?
            ORDER BY
                CASE status
                    WHEN 'primary' THEN 0
                    WHEN 'approved' THEN 1
                    WHEN 'pending' THEN 2
                    WHEN 'cancelled' THEN 3
                    WHEN 'rejected' THEN 4
                    ELSE 5
                END,
                CASE WHEN position > 0 THEN position ELSE 999 END,
                created_at
        ''', (event_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_user_applications(self, user_id: int) -> List[Dict]:
        """Отримати всі заявки користувача з інформацією про заходи"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT a.*, e.procedure_type, e.date, e.time, e.status as event_status
            FROM applications a
            JOIN events e ON a.event_id = e.id
            WHERE a.user_id = ?
            ORDER BY e.date DESC, e.time DESC
        ''', (user_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def user_has_application_for_event(self, user_id: int, event_id: int) -> bool:
        """Перевірити, чи існує заявка користувача на конкретний захід"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 1
            FROM applications
            WHERE user_id = ? AND event_id = ?
            LIMIT 1
        ''', (user_id, event_id))
        exists = cursor.fetchone() is not None
        conn.close()
        return exists

    def update_application_group_message_id(self, application_id: int, message_id: int) -> None:
        """Оновити ID повідомлення заявки в групі"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE applications SET group_message_id = ? WHERE id = ?
        ''', (message_id, application_id))
        conn.commit()
        conn.close()

    def set_application_status(self, application_id: int, status: str) -> None:
        """Оновити статус заявки та прапорець основного кандидата"""
        self.update_application_status(application_id, status)

    def update_application_position(self, application_id: int, position: int) -> None:
        """Змінити позицію заявки у черзі"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE applications
            SET position = ?
            WHERE id = ?
        ''', (position, application_id))
        conn.commit()
        conn.close()

    def recalculate_application_positions(self, event_id: int) -> None:
        """Перерахувати позиції заявок після змін статусів з захистом від race condition"""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            # Використовуємо BEGIN IMMEDIATE для блокування на рівні БД
            cursor.execute('BEGIN IMMEDIATE')

            # Отримуємо заявки в межах транзакції
            cursor.execute('''
                SELECT id, status
                FROM applications
                WHERE event_id = ?
                ORDER BY created_at ASC
            ''', (event_id,))
            applications = cursor.fetchall()

            primary_id = None
            reserve_ids: List[int] = []

            for app in applications:
                app_id = app[0]
                status = app[1]

                if status == 'primary':
                    if primary_id is None:
                        primary_id = app_id
                    else:
                        # Лишаємо лише одного основного кандидата
                        cursor.execute('''
                            UPDATE applications
                            SET status = 'approved', is_primary = 0
                            WHERE id = ?
                        ''', (app_id,))
                        reserve_ids.append(app_id)
                elif status == 'approved':
                    reserve_ids.append(app_id)

            position = 1
            if primary_id:
                cursor.execute('''
                    UPDATE applications
                    SET position = ?
                    WHERE id = ?
                ''', (position, primary_id))
                position += 1

            for reserve_id in reserve_ids:
                cursor.execute('''
                    UPDATE applications
                    SET position = ?
                    WHERE id = ?
                ''', (position, reserve_id))
                position += 1

            # Скинути позицію для інших статусів
            cursor.execute('''
                UPDATE applications
                SET position = 0
                WHERE event_id = ? AND status NOT IN ('primary', 'approved')
            ''', (event_id,))

            conn.commit()

        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def get_user_applications_for_date(self, user_id: int, date: str) -> List[Dict]:
        """Отримати всі заявки користувача на конкретну дату"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT a.*, e.procedure_type, e.time, e.status AS event_status
            FROM applications a
            JOIN events e ON a.event_id = e.id
            WHERE a.user_id = ? AND e.date = ?
        ''', (user_id, date))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def count_user_active_applications(self, user_id: int) -> int:
        """Підрахувати кількість активних заявок користувача (pending, approved, primary)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM applications
            WHERE user_id = ? AND status IN ('pending', 'approved', 'primary')
        ''', (user_id,))
        count = cursor.fetchone()[0]
        conn.close()
        return count

    # Методи для роботи з повідомленнями по днях
    def get_day_message_id(self, date: str) -> Optional[int]:
        """Отримати ID повідомлення з підсумком по дню"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT message_id FROM day_messages WHERE date = ?', (date,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else None

    def update_day_message_id(self, date: str, message_id: int) -> None:
        """Зберегти або оновити ID повідомлення по дню"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO day_messages (date, message_id)
            VALUES (?, ?)
            ON CONFLICT(date) DO UPDATE SET message_id = excluded.message_id
        ''', (date, message_id))
        conn.commit()
        conn.close()

    def delete_day_message(self, date: str) -> None:
        """Видалити запис про повідомлення по дню"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM day_messages WHERE date = ?', (date,))
        conn.commit()
        conn.close()

    def get_applications_by_group_message(self, group_message_id: int) -> List[Dict]:
        """Отримати всі заявки, прив'язані до одного повідомлення в групі"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT
                a.*,
                e.procedure_type,
                e.date,
                e.time,
                e.needs_photo,
                e.status AS event_status
            FROM applications a
            JOIN events e ON a.event_id = e.id
            WHERE a.group_message_id = ?
            ORDER BY e.date, e.time, a.id
        ''', (group_message_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_application_with_event(self, application_id: int) -> Optional[Dict]:
        """Отримати заявку разом із даними заходу"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT
                a.*,
                e.procedure_type,
                e.date,
                e.time,
                e.needs_photo,
                e.status AS event_status
            FROM applications a
            JOIN events e ON a.event_id = e.id
            WHERE a.id = ?
        ''', (application_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    # Методи для роботи з фото заявок
    def add_application_photo(self, application_id: int, file_id: str) -> None:
        """Додати фото до заявки"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO application_photos (application_id, file_id) VALUES (?, ?)
        ''', (application_id, file_id))
        conn.commit()
        conn.close()

    def get_application_photos(self, application_id: int) -> List[str]:
        """Отримати фото заявки"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT file_id FROM application_photos WHERE application_id = ?', (application_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

    # Методи для роботи з типами процедур
    def _init_procedure_types(self) -> None:
        """Ініціалізація типів процедур з constants.py"""
        from constants import PROCEDURE_TYPES

        conn = self.get_connection()
        cursor = conn.cursor()

        # Перевірити чи таблиця порожня
        cursor.execute('SELECT COUNT(*) FROM procedure_types')
        count = cursor.fetchone()[0]

        if count == 0:
            # Додати початкові типи процедур
            for procedure_type in PROCEDURE_TYPES:
                cursor.execute('''
                    INSERT INTO procedure_types (name) VALUES (?)
                ''', (procedure_type,))

        conn.commit()
        conn.close()

    def get_active_procedure_types(self) -> List[Dict]:
        """Отримати активні типи процедур"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM procedure_types
            WHERE is_active = 1
            ORDER BY name
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_all_procedure_types(self) -> List[Dict]:
        """Отримати всі типи процедур (включаючи неактивні)"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM procedure_types
            ORDER BY is_active DESC, name
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_procedure_type(self, type_id: int) -> Optional[Dict]:
        """Отримати тип процедури за ID"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM procedure_types WHERE id = ?', (type_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def create_procedure_type(self, name: str) -> int:
        """Створити новий тип процедури"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO procedure_types (name, is_active) VALUES (?, 1)
        ''', (name,))
        type_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return type_id

    def update_procedure_type(self, type_id: int, name: str) -> None:
        """Оновити назву типу процедури"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE procedure_types SET name = ? WHERE id = ?
        ''', (name, type_id))
        conn.commit()
        conn.close()

    def toggle_procedure_type(self, type_id: int) -> None:
        """Перемкнути активність типу процедури"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE procedure_types
            SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
            WHERE id = ?
        ''', (type_id,))
        conn.commit()
        conn.close()

    def delete_procedure_type(self, type_id: int) -> bool:
        """Видалити тип процедури (тільки якщо не використовується)"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Спочатку отримати назву типу
        cursor.execute('SELECT name FROM procedure_types WHERE id = ?', (type_id,))
        result = cursor.fetchone()

        if not result:
            conn.close()
            return False  # Тип не знайдено

        type_name = result[0]

        # Перевірити чи використовується цей тип в заходах
        cursor.execute('SELECT COUNT(*) FROM events WHERE procedure_type = ?', (type_name,))
        count = cursor.fetchone()[0]

        if count > 0:
            conn.close()
            return False  # Неможливо видалити, тип використовується

        # Видалити тип
        cursor.execute('DELETE FROM procedure_types WHERE id = ?', (type_id,))
        conn.commit()
        conn.close()
        return True

    def clear_all_data(self) -> None:
        """Повна очистка всіх даних в БД (структура таблиць зберігається)"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Видалити дані з усіх таблиць (зберігаючи структуру)
        cursor.execute('DELETE FROM application_photos')
        cursor.execute('DELETE FROM applications')
        cursor.execute('DELETE FROM events')
        cursor.execute('DELETE FROM users')
        cursor.execute('DELETE FROM procedure_types')

        # Скинути autoincrement лічильники
        cursor.execute('DELETE FROM sqlite_sequence')

        conn.commit()
        conn.close()

        # Повернути початкові типи процедур
        self._init_procedure_types()

    def archive_old_events(self, days_old: int = 180) -> int:
        """Архівувати старі заходи (старше вказаної кількості днів)

        Args:
            days_old: Кількість днів (за замовчуванням 180 = 6 місяців)

        Returns:
            Кількість архівованих заходів
        """
        from datetime import datetime, timedelta
        from constants import UKRAINE_TZ

        cutoff_date = (datetime.now(UKRAINE_TZ) - timedelta(days=days_old)).strftime('%Y-%m-%d')

        conn = self.get_connection()
        cursor = conn.cursor()

        # Отримати ID старих заходів
        cursor.execute('SELECT id FROM events WHERE date < ? AND status != "cancelled"', (cutoff_date,))
        old_event_ids = [row[0] for row in cursor.fetchall()]

        if not old_event_ids:
            conn.close()
            return 0

        # Змінити статус старих заходів на 'archived'
        placeholders = ','.join('?' * len(old_event_ids))
        cursor.execute(f'''
            UPDATE events
            SET status = 'archived'
            WHERE id IN ({placeholders})
        ''', old_event_ids)

        archived_count = cursor.rowcount
        conn.commit()
        conn.close()

        return archived_count
