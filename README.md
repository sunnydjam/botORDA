# 🐎 OrdaFlow Proxy Bot

Telegram бот для продажи Proxy-подписок с оплатой через Telegram Stars ⭐

## ✨ Возможности

- 💳 **Оплата Telegram Stars** — встроенные платежи без внешних платёжных систем
- 📅 **Подписка на 1 месяц** — 50⭐
- ♾️ **Безлимитный трафик** — без ограничений
- 🔄 **Автоматическое создание proxy-аккаунта** — сразу после оплаты
- 🔔 **Уведомления админу** — о каждом платеже

## 📋 Тариф

| Тариф | Цена | Срок | Трафик |
|-------|------|------|--------|
| 📅 1 месяц | 50⭐ | 30 дней | Безлимит |

## 🚀 Установка

### 1. Клонируйте репозиторий

```bash
git clone https://github.com/your-username/botORDA.git
cd botORDA
```

### 2. Установите зависимости

```bash
pip install python-telegram-bot python-dotenv
```

### 3. Настройте переменные окружения

```bash
cp .env.example .env
```

Отредактируйте `.env`:

```env
TELEGRAM_TOKEN=your_bot_token_from_botfather
ADMIN_USERNAME=your_admin_login
ADMIN_PASSWORD=your_admin_password
ORDAFLOW_API_URL=https://your-api-url.com
ADMIN_CHAT_ID=your_telegram_id
```

### 4. Запустите бота

```bash
python botorda.py
```

## 📱 Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Главное меню / статус proxy |
| `/subscribe` | Оформление подписки |
| `/myvpn` | Информация о подписке |
| `/paysupport` | Помощь по платежам |
| `/help` | Справка |

## ⚙️ Переменные окружения

| Переменная | Описание | Обязательно |
|------------|----------|-------------|
| `TELEGRAM_TOKEN` | Токен бота от @BotFather | ✅ |
| `ADMIN_USERNAME` | Логин админа панели | ✅ |
| `ADMIN_PASSWORD` | Пароль админа панели | ✅ |
| `ORDAFLOW_API_URL` | URL API сервера | ✅ |
| `ADMIN_PANEL_URL` | URL админ-панели | ❌ |
| `ADMIN_CHAT_ID` | Telegram ID для уведомлений | ❌ |

## 🔒 Безопасность

⚠️ **Никогда не коммитьте файл `.env`!**

Файл `.gitignore` уже настроен для игнорирования:
- `.env` - секреты
- `*.log` - логи
- `subscriptions.json` - данные подписок
- `payments.json` - история платежей

## 📂 Структура проекта

```
botORDA/
├── botorda.py          # Основной код бота
├── .env.example        # Пример конфигурации
├── .gitignore          # Игнорируемые файлы
├── README.md           # Документация
└── logs/               # Логи (создаётся автоматически)
```

## 🛠 Требования

- Python 3.10+
- python-telegram-bot >= 20.0
- python-dotenv
- Совместимый Proxy/VPN API сервер

## 📝 Лицензия

MIT License

## 👤 Автор

Telegram: @sunnydja
