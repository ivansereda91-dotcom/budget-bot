# Семейный бюджет — Telegram-бот

Расходы и доходы по категориям, месячные лимиты с предупреждениями (80 % и 100 %), отчёты с графиком, история с удалением, выгрузка в CSV. Общий бюджет на двоих, в евро.

## Как пользоваться

- `12,50 кофе` — расход, затем выбрать категорию кнопкой
- `+2000 зарплата` — доход
- Меню: ➖ Расход · ➕ Доход · 📊 Отчёт · 💰 Бюджет · 🧾 История · 🗂 Категории
- `/export` — все записи в CSV (открывается в Excel), `/cancel` — отменить ввод, `/id` — свой Telegram ID

## Переменные окружения

| Переменная | Что это |
|---|---|
| `BOT_TOKEN` | токен от @BotFather (обязательно) |
| `ALLOWED_IDS` | Telegram ID через запятую: `111111,222222`. Чужим бот отвечает «нет доступа» и показывает их ID |
| `DB_PATH` | путь к базе, по умолчанию `budget.db`. На хостинге укажи путь на постоянном диске, например `/data/budget.db` |
| `NOTIFY_PARTNER` | `1` — присылать второму человеку каждую новую запись (по умолчанию выключено) |

Как узнать ID жены: пусть она напишет боту что угодно, и он ответит её ID. Добавь его в `ALLOWED_IDS` и перезапусти бота.

## Запуск

```bash
pip install -r requirements.txt
export BOT_TOKEN=...  ALLOWED_IDS=...  DB_PATH=/data/budget.db
python bot.py
```

На своём сервере удобно держать бота как systemd-сервис:

```ini
# /etc/systemd/system/budget-bot.service
[Unit]
Description=Budget bot
After=network-online.target

[Service]
WorkingDirectory=/opt/budget-bot
EnvironmentFile=/opt/budget-bot/.env
ExecStart=/opt/budget-bot/venv/bin/python bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now budget-bot`

**Важно:** база хранится в одном файле SQLite. На хостингах без постоянного диска она стирается при каждом перезапуске, поэтому там нужен volume. Бэкап — периодически делать `/export` или копировать файл базы.
