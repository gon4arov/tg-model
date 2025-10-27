import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Any

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

        conn.commit()
        conn.close()

        # Ініціалізувати типи процедур якщо таблиця порожня
        self._init_procedure_types()

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

    def get_active_events(self) -> List[Dict]:
        """Отримати активні заходи (від сьогодні)"""
        from datetime import datetime
        today = datetime.now().strftime('%Y-%m-%d')

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
        """Отримати минулі заходи (до сьогодні)"""
        from datetime import datetime
        today = datetime.now().strftime('%Y-%m-%d')

        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM events
            WHERE status = 'published' AND date < ?
            ORDER BY date DESC, time DESC
        ''', (today,))
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
        """Оновити статус заявки"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE applications SET status = ? WHERE id = ?', (status, application_id))
        conn.commit()
        conn.close()

    def set_primary_application(self, application_id: int) -> None:
        """Встановити заявку як основну"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE applications SET is_primary = 1 WHERE id = ?', (application_id,))
        conn.commit()
        conn.close()

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
            SELECT * FROM applications
            WHERE event_id = ?
            ORDER BY created_at
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

    def update_application_group_message_id(self, application_id: int, message_id: int) -> None:
        """Оновити ID повідомлення заявки в групі"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE applications SET group_message_id = ? WHERE id = ?
        ''', (message_id, application_id))
        conn.commit()
        conn.close()

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
