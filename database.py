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

        conn.commit()
        conn.close()

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
        """Отримати активні заходи"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM events WHERE status = 'published'
            ORDER BY date, time
        ''')
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
