# Repository Guidelines

## Project Structure & Module Organization
- `bot.py` orchestrates Telegram workflows, loads config, and routes callbacks; group new handlers next to related flows.
- `database.py` encapsulates SQLite access—extend its methods instead of issuing ad-hoc queries so schema upgrades stay consistent.
- `constants.py` holds conversation state IDs, date/time helpers, and limits; declare shared enums here and import into runtime modules.
- Runtime artifacts (`bot.db`, `bot-actions.log`, `bot_data.pickle`) live beside the code—keep them out of version control. Assets sit in `img/`, while secrets load from `.env`.

## Build, Test, and Development Commands
- Install dependencies in a virtualenv:  
  ```sh
  python -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  ```
- Start the bot with environment variables loaded (e.g., `source .env`):  
  ```sh
  python bot.py
  ```
- Inspect data without scripts:  
  ```sh
  sqlite3 bot.db 'SELECT id, date, time, status FROM events LIMIT 5;'
  ```

## Coding Style & Naming Conventions
- Follow PEP 8: 4-space indentation, snake_case functions, ALL_CAPS constants, and organized import blocks.
- Write async handlers that return Telegram conversation states; use descriptive names such as `handle_create_event` or `on_photo_upload`.
- When changing persistence, add `_add_column_if_missing` helpers rather than inline `ALTER TABLE` calls.
- Keep user-facing strings in Ukrainian and reuse existing emoji/status vocabulary for uniform responses.

## Testing Guidelines
- Automated tests are absent; rehearse admin and applicant journeys with a staging bot to confirm keyboards, statuses, and email hooks.
- Reset disposable data between runs with targeted `sqlite3` statements and verify `bot-actions.log` stays free of tracebacks.
- For new logic-heavy utilities, add `pytest` checks under `tests/` so future suites can run via `pytest -q`.

## Commit & Pull Request Guidelines
- Mirror the history: concise Ukrainian subject lines in the imperative mood (e.g., `Додати обробку помилок при розсилці`), capitalized and without trailing punctuation.
- Keep commits focused; note config or migration steps in the body when relevant.
- Pull requests must outline user impact, manual test evidence, touched environment variables, and visuals/logs for messaging changes.
- Reference issues or ticket IDs and flag required post-merge actions (schema updates, env tweaks).

## Security & Configuration Tips
- Keep `.env` values (tokens, SMTP creds, group IDs) out of version control and rotate them if logs expose sensitive data.
- Trial SMTP settings in a disposable environment before production and ensure all `EMAIL_*` variables are populated.
- Review `bot-actions.log`; override `BOT_LOG_FILE` per instance to preserve diagnostics.
