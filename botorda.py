import os
import json
import logging
import asyncio
from logging.handlers import RotatingFileHandler
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv не установлен, используем системные переменные

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters
)

# ============== ТАРИФНЫЕ ПЛАНЫ ==============
SUBSCRIPTION_PLANS = {
    "month1": {
        "name": "1 месяц",
        "emoji": "📅",
        "stars": 50,
        "days": 30,
        "description": "Безлимитный proxy на 1 месяц"
    }
}

# ============== ПРОБНЫЙ ПЕРИОД ==============
TRIAL_CONFIG = {
    "days": 3,
    "traffic_limit_gb": 3,
    "traffic_limit_bytes": 3 * 1024 * 1024 * 1024,  # 3 GB
}

# Настройка логирования с записью в файл
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Форматтер для логов
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Консольный обработчик
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# Файловый обработчик с ротацией (макс 5 MB, 3 файла)
file_handler = RotatingFileHandler(
    LOG_DIR / "bot.log",
    maxBytes=5*1024*1024,
    backupCount=3,
    encoding='utf-8'
)
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

# Конфигурация из переменных окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ORDAFLOW_API_URL = os.getenv("ORDAFLOW_API_URL", "")
ADMIN_PANEL_URL = os.getenv("ADMIN_PANEL_URL", "")

# ID админа для уведомлений о платежах (ваш Telegram ID)
# Узнать можно через @userinfobot или @getmyid_bot
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")

# Проверка обязательных переменных
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не установлен! Создайте файл .env или установите переменную окружения.")
if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise ValueError("ADMIN_USERNAME и ADMIN_PASSWORD должны быть установлены!")
if not ORDAFLOW_API_URL:
    raise ValueError("ORDAFLOW_API_URL должен быть установлен!")

class OrdaflowAPIManager:
    """Менеджер для работы с API Ordaflow"""
    
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.base_url = ORDAFLOW_API_URL
        self.access_token = None
        self.token_expiry = None
        logger.info(f"API Manager инициализирован для пользователя: {username}")
    
    def _make_request(self, endpoint: str, method: str = "GET", 
                     data: dict = None, headers: dict = None) -> dict:
        """Делает HTTP запрос к API"""
        try:
            url = f"{self.base_url}{endpoint}"
            
            # Базовые заголовки
            default_headers = {
                "Accept": "application/json"
            }
            
            if headers:
                default_headers.update(headers)
            
            # Подготавливаем данные
            data_bytes = None
            if data:
                if default_headers.get("Content-Type") == "application/x-www-form-urlencoded":
                    # Для формы
                    data_bytes = urllib.parse.urlencode(data).encode('utf-8')
                else:
                    # Для JSON
                    default_headers.setdefault("Content-Type", "application/json")
                    data_bytes = json.dumps(data, ensure_ascii=False).encode('utf-8')
            
            # Создаем запрос
            req = urllib.request.Request(
                url, 
                data=data_bytes, 
                headers=default_headers, 
                method=method
            )
            
            # Отправляем запрос
            with urllib.request.urlopen(req, timeout=30) as response:
                response_data = response.read().decode('utf-8')
                result = json.loads(response_data) if response_data else {}
                
                return {
                    "success": True,
                    "status_code": response.getcode(),
                    "data": result,
                    "message": "Запрос выполнен успешно"
                }
                
        except urllib.error.HTTPError as e:
            raw_error = e.read()
            error_data = raw_error.decode('utf-8') if raw_error else str(e)
            logger.error(f"HTTP Error {e.code} for {endpoint}: {error_data[:200]}")
            
            try:
                error_json = json.loads(error_data) if error_data else {}
                error_detail = error_json.get("detail", error_data)
                
                # Если это список ошибок валидации
                if isinstance(error_detail, list) and len(error_detail) > 0:
                    error_messages = []
                    for err in error_detail:
                        if isinstance(err, dict):
                            loc = err.get('loc', [])
                            msg = err.get('msg', '')
                            error_messages.append(f"{loc}: {msg}")
                    error_detail = "; ".join(error_messages)
                    
            except:
                error_detail = error_data
                
            return {
                "success": False,
                "status_code": e.code,
                "error": error_detail[:500],
                "message": f"HTTP ошибка {e.code}"
            }
        except Exception as e:
            logger.error(f"Request error for {endpoint}: {e}")
            return {
                "success": False,
                "error": str(e),
                "message": f"Ошибка запроса: {e}"
            }
    
    def get_access_token(self) -> dict:
        """Получает access token используя логин/пароль"""
        try:
            logger.info(f"Получение токена для пользователя: {self.username}")
            
            # Формируем данные для x-www-form-urlencoded
            data = {
                "username": self.username,
                "password": self.password
            }
            
            headers = {
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            result = self._make_request("/api/admin/token", "POST", data, headers)
            
            if result["success"] and "access_token" in result["data"]:
                self.access_token = result["data"]["access_token"]
                
                # Устанавливаем время истечения токена
                self.token_expiry = datetime.now() + timedelta(hours=23)
                
                logger.info(f"Токен получен успешно!")
                
                return {
                    "success": True,
                    "token": self.access_token[:30] + "..." if len(self.access_token) > 30 else self.access_token,
                    "expiry": self.token_expiry.isoformat(),
                    "message": "Токен получен успешно!"
                }
            else:
                error_msg = result.get("error", "Токен не найден в ответе")
                return {
                    "success": False,
                    "error": error_msg,
                    "message": result.get("message", f"API не вернул access token: {error_msg}")
                }
                    
        except Exception as e:
            logger.error(f"Token request error: {e}")
            return {
                "success": False,
                "error": str(e),
                "message": f"Ошибка получения токена: {e}"
            }
    
    def ensure_token_valid(self) -> bool:
        """Проверяет и обновляет токен если нужно"""
        if not self.access_token or not self.token_expiry:
            result = self.get_access_token()
            return result["success"]
        
        # Проверяем не истек ли токен
        if datetime.now() >= self.token_expiry:
            logger.info("Токен истек, получаем новый...")
            result = self.get_access_token()
            return result["success"]
        
        return True
    
    def test_connection(self) -> dict:
        """Тест подключения к API"""
        return self._make_request("/api/core", "GET")
    
    def create_vpn_user(self, username: str, telegram_username: str = None,
                         data_limit_bytes: int = None, expire_days: int = 30) -> dict:
        """Создает нового VPN пользователя"""
        if not self.ensure_token_valid():
            return {
                "success": False,
                "message": "Не удалось получить действительный токен"
            }
        
        # Вычисляем дату истечения
        # Marzban принимает expire в секундах (Unix timestamp)
        expire_timestamp = int((datetime.now() + timedelta(days=expire_days)).timestamp())
        
        # Лимит трафика (0 = безлимит, None = по умолчанию 150 GB)
        if data_limit_bytes is None:
            data_limit_bytes = 5 * 30 * 1024 * 1024 * 1024  # 150 GB по умолчанию
        
        # Примечание с логином Telegram
        note = f"Telegram: @{telegram_username}" if telegram_username else f"Telegram ID: {username}"
        
        # Данные для создания пользователя с привязкой к inbound'ам
        user_data = {
            "username": username,
            "proxies": {
                "vmess": {},
                "vless": {},
                "trojan": {},
                "shadowsocks": {}
            },
            "inbounds": {
                "vmess": ["VMess TCP"],
                "vless": ["VLESS TCP REALITY"],
                "trojan": ["Trojan TLS", "Trojan Websocket TLS"],
                "shadowsocks": ["Shadowsocks TCP"]
            },
            "status": "active",
            "data_limit": data_limit_bytes,
            "expire": expire_timestamp,
            "note": note
        }
        
        limit_text = f"{data_limit_bytes / (1024**3):.0f} GB" if data_limit_bytes > 0 else "безлимит"
        logger.info(f"Создаю пользователя: {username}, лимит: {limit_text}, срок: {expire_days} дней, примечание: {note}")
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        
        result = self._make_request("/api/user", "POST", user_data, headers)
        
        if result["success"]:
            data = result.get("data", {})
            logger.info(f"Ответ API при создании пользователя: {data}")
            
            # Marzban возвращает subscription_url в ответе
            if isinstance(data, dict):
                # Проверяем разные возможные поля с ссылкой
                subscription_url = (
                    data.get("subscription_url") or 
                    data.get("subscription") or 
                    data.get("sub_url") or
                    data.get("link")
                )
                
                if subscription_url:
                    result["subscription_url"] = subscription_url
                    logger.info(f"Ссылка на подписку из ответа: {subscription_url}")
                else:
                    # Пробуем получить через отдельный запрос
                    subscription_result = self.get_subscription_url(username)
                    if subscription_result["success"]:
                        result["subscription_url"] = subscription_result.get("subscription_url")
                    else:
                        logger.warning(f"Не удалось получить ссылку для {username}")
                        result["subscription_url"] = None
                
                # Сохраняем также прямые ссылки на конфиги
                links = data.get("links", [])
                if links:
                    result["links"] = links
        
        return result
    
    def get_subscription_url(self, username: str) -> dict:
        """Получает ссылку на подписку для пользователя"""
        if not self.ensure_token_valid():
            return {
                "success": False,
                "message": "Не удалось получить действительный токен"
            }
        
        headers = {
            "Authorization": f"Bearer {self.access_token}"
        }
        
        # Сначала получаем информацию о пользователе - там должна быть ссылка
        user_result = self._make_request(f"/api/user/{username}", "GET", headers=headers)
        
        if user_result["success"]:
            data = user_result.get("data", {})
            logger.info(f"Данные пользователя {username}: {data}")
            
            if isinstance(data, dict):
                # Проверяем разные возможные поля
                subscription_url = (
                    data.get("subscription_url") or 
                    data.get("subscription") or 
                    data.get("sub_url") or
                    data.get("link")
                )
                
                if subscription_url:
                    return {
                        "success": True,
                        "subscription_url": subscription_url
                    }
                
                # Если есть links - список конфигов
                links = data.get("links", [])
                if links and len(links) > 0:
                    return {
                        "success": True,
                        "subscription_url": links[0],  # Первая ссылка
                        "all_links": links
                    }
        
        # Если не нашли ссылку в данных пользователя
        return {
            "success": False,
            "message": "Ссылка на подписку не найдена в данных пользователя"
        }
    
    def get_user_info(self, username: str = None) -> dict:
        """Получает информацию о пользователях"""
        if not self.ensure_token_valid():
            return {
                "success": False,
                "message": "Не удалось получить действительный токен"
            }
        
        headers = {
            "Authorization": f"Bearer {self.access_token}"
        }
        
        endpoint = "/api/users"
        if username:
            endpoint = f"/api/user/{username}"
        
        return self._make_request(endpoint, "GET", headers=headers)
    
    def reset_user_traffic(self, username: str) -> dict:
        """Сбрасывает трафик пользователя"""
        if not self.ensure_token_valid():
            return {
                "success": False,
                "message": "Не удалось получить действительный токен"
            }
        
        headers = {
            "Authorization": f"Bearer {self.access_token}"
        }
        
        return self._make_request(f"/api/user/{username}/reset", "POST", headers=headers)
    
    def set_user_status(self, username: str, status: str) -> dict:
        """Устанавливает статус пользователя (active/disabled/limited)"""
        if not self.ensure_token_valid():
            return {
                "success": False,
                "message": "Не удалось получить действительный токен"
            }
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        
        data = {"status": status}
        
        return self._make_request(f"/api/user/{username}", "PUT", data, headers)

# Инициализация менеджера API
api_manager = OrdaflowAPIManager(ADMIN_USERNAME, ADMIN_PASSWORD)


class DailyTrafficManager:
    """Менеджер дневного лимита трафика"""
    
    def __init__(self):
        self.data_file = Path(__file__).parent / "daily_traffic.json"
        self.data = self._load_data()
    
    def _load_data(self) -> dict:
        """Загружает данные из файла"""
        if self.data_file.exists():
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка загрузки daily_traffic.json: {e}")
        return {"date": datetime.now().strftime("%Y-%m-%d"), "users": {}}
    
    def _save_data(self):
        """Сохраняет данные в файл"""
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения daily_traffic.json: {e}")
    
    def _check_new_day(self):
        """Проверяет новый день и сбрасывает счётчики"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.data.get("date") != today:
            logger.info(f"Новый день! Сброс дневных счётчиков трафика ({self.data.get('date')} -> {today})")
            self.data = {"date": today, "users": {}}
            self._save_data()
    
    def get_user_daily_traffic(self, username: str) -> dict:
        """Получает дневной трафик пользователя"""
        self._check_new_day()
        
        user_data = self.data["users"].get(username, {
            "start_traffic": 0,
            "last_traffic": 0,
            "daily_used": 0,
            "is_blocked": False
        })
        
        return user_data
    
    def update_user_traffic(self, username: str, current_total_traffic: int, daily_limit_bytes: int = None) -> dict:
        """
        Обновляет информацию о дневном трафике пользователя
        
        Args:
            username: имя пользователя
            current_total_traffic: текущий общий трафик из API (used_traffic)
            daily_limit_bytes: дневной лимит пользователя (0 = безлимит, None = DAILY_TRAFFIC_LIMIT)
        
        Returns:
            dict с информацией о дневном лимите
        """
        self._check_new_day()
        
        if daily_limit_bytes is None:
            daily_limit_bytes = DAILY_TRAFFIC_LIMIT
        
        if username not in self.data["users"]:
            # Первый запрос за день - запоминаем начальное значение
            self.data["users"][username] = {
                "start_traffic": current_total_traffic,
                "last_traffic": current_total_traffic,
                "daily_used": 0,
                "is_blocked": False
            }
        else:
            # Вычисляем использованный трафик за день
            start_traffic = self.data["users"][username]["start_traffic"]
            daily_used = current_total_traffic - start_traffic
            
            # Если трафик сбросился на сервере (reset), корректируем
            if daily_used < 0:
                self.data["users"][username]["start_traffic"] = current_total_traffic
                daily_used = 0
            
            self.data["users"][username]["daily_used"] = daily_used
            self.data["users"][username]["last_traffic"] = current_total_traffic
        
        self._save_data()
        
        user_data = self.data["users"][username]
        daily_used = user_data["daily_used"]
        
        # 0 = безлимит
        if daily_limit_bytes == 0:
            return {
                "daily_used": daily_used,
                "daily_limit": 0,
                "remaining": 0,
                "is_exceeded": False,
                "is_unlimited": True,
                "used_percent": 0
            }
        
        remaining = max(0, daily_limit_bytes - daily_used)
        is_exceeded = daily_used >= daily_limit_bytes
        
        return {
            "daily_used": daily_used,
            "daily_limit": daily_limit_bytes,
            "remaining": remaining,
            "is_exceeded": is_exceeded,
            "is_unlimited": False,
            "used_percent": min(100, (daily_used / daily_limit_bytes) * 100)
        }
    
    def set_user_blocked(self, username: str, blocked: bool):
        """Отмечает пользователя как заблокированного за превышение лимита"""
        self._check_new_day()
        if username in self.data["users"]:
            self.data["users"][username]["is_blocked"] = blocked
            self._save_data()
    
    def is_user_blocked(self, username: str) -> bool:
        """Проверяет, заблокирован ли пользователь за превышение дневного лимита"""
        self._check_new_day()
        return self.data["users"].get(username, {}).get("is_blocked", False)
    
    def reset_all_daily(self):
        """Полный сброс всех дневных счётчиков (вызывается в полночь)"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Разблокируем всех заблокированных пользователей
        blocked_users = [u for u, d in self.data.get("users", {}).items() if d.get("is_blocked")]
        
        self.data = {"date": today, "users": {}}
        self._save_data()
        
        logger.info(f"Дневные счётчики сброшены. Разблокировано пользователей: {len(blocked_users)}")
        return blocked_users


# Инициализация менеджеров
daily_traffic_manager = DailyTrafficManager()


class SubscriptionManager:
    """Менеджер подписок пользователей"""
    
    def __init__(self):
        self.data_file = Path(__file__).parent / "subscriptions.json"
        self.data = self._load_data()
    
    def _load_data(self) -> dict:
        """Загружает данные подписок"""
        if self.data_file.exists():
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка загрузки subscriptions.json: {e}")
        return {"users": {}}
    
    def _save_data(self):
        """Сохраняет данные подписок"""
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения subscriptions.json: {e}")
    
    def get_subscription(self, user_id: int) -> dict:
        """Получает информацию о подписке пользователя"""
        user_key = str(user_id)
        if user_key not in self.data["users"]:
            return {
                "active": False,
                "plan": None,
                "expires": None
            }
        
        sub = self.data["users"][user_key]
        expires = datetime.fromisoformat(sub["expires"]) if sub.get("expires") else None
        
        # Проверяем истекла ли подписка
        if expires and datetime.now() > expires:
            return {
                "active": False,
                "plan": sub.get("plan"),
                "expires": expires,
                "expired": True
            }
        
        return {
            "active": True,
            "plan": sub.get("plan"),
            "plan_name": sub.get("plan_name"),
            "expires": expires,
            "payment_id": sub.get("payment_id"),
            "vpn_username": sub.get("vpn_username")
        }
    
    def activate_subscription(self, user_id: int, plan_id: str, payment_id: str, vpn_username: str) -> dict:
        """Активирует подписку для пользователя"""
        if plan_id not in SUBSCRIPTION_PLANS:
            return {"success": False, "error": "Неизвестный тарифный план"}
        
        plan = SUBSCRIPTION_PLANS[plan_id]
        user_key = str(user_id)
        
        # Вычисляем дату окончания
        expires = datetime.now() + timedelta(days=plan["days"])
        
        self.data["users"][user_key] = {
            "plan": plan_id,
            "plan_name": plan["name"],
            "expires": expires.isoformat(),
            "payment_id": payment_id,
            "vpn_username": vpn_username,
            "activated_at": datetime.now().isoformat(),
            "stars_paid": plan["stars"]
        }
        
        self._save_data()
        
        logger.info(f"Подписка активирована: user={user_id}, plan={plan_id}, expires={expires}")
        
        return {
            "success": True,
            "plan": plan,
            "expires": expires,
            "vpn_username": vpn_username
        }
    
    def get_daily_limit_bytes(self, user_id: int) -> int:
        """Возвращает дневной лимит в байтах для пользователя"""
        sub = self.get_subscription(user_id)
        if not sub["active"]:
            return 0  # Нет подписки - нет доступа
        
        return 0  # Все тарифы безлимитные
    
    def save_payment(self, user_id: int, payment_id: str, plan_id: str, stars: int):
        """Сохраняет информацию о платеже"""
        payments_file = Path(__file__).parent / "payments.json"
        
        try:
            if payments_file.exists():
                with open(payments_file, 'r', encoding='utf-8') as f:
                    payments = json.load(f)
            else:
                payments = {"payments": []}
            
            payments["payments"].append({
                "user_id": user_id,
                "payment_id": payment_id,
                "plan_id": plan_id,
                "stars": stars,
                "timestamp": datetime.now().isoformat()
            })
            
            with open(payments_file, 'w', encoding='utf-8') as f:
                json.dump(payments, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            logger.error(f"Ошибка сохранения платежа: {e}")

    # ============ МЕТОДЫ ПРОБНОГО ПЕРИОДА ============

    def has_used_trial(self, user_id: int) -> bool:
        """Проверяет, использовал ли пользователь пробный период"""
        user_key = str(user_id)
        user_data = self.data["users"].get(user_key, {})
        return user_data.get("trial_used", False)

    def get_trial_status(self, user_id: int) -> dict:
        """Получает статус пробного периода"""
        user_key = str(user_id)
        user_data = self.data["users"].get(user_key, {})

        if not user_data.get("trial_active", False):
            return {"active": False, "used": user_data.get("trial_used", False)}

        expires = datetime.fromisoformat(user_data["trial_expires"])
        now = datetime.now()

        if now > expires:
            return {
                "active": False,
                "used": True,
                "expired": True,
                "reason": "time",
                "vpn_username": user_data.get("vpn_username")
            }

        return {
            "active": True,
            "used": True,
            "expires": expires,
            "days_left": (expires - now).days,
            "hours_left": int((expires - now).total_seconds() / 3600),
            "vpn_username": user_data.get("vpn_username"),
            "traffic_limit_bytes": TRIAL_CONFIG["traffic_limit_bytes"]
        }

    def activate_trial(self, user_id: int, vpn_username: str) -> dict:
        """Активирует пробный период для пользователя"""
        user_key = str(user_id)

        if self.has_used_trial(user_id):
            return {"success": False, "error": "Пробный период уже был использован"}

        expires = datetime.now() + timedelta(days=TRIAL_CONFIG["days"])

        self.data["users"][user_key] = {
            **self.data["users"].get(user_key, {}),
            "trial_active": True,
            "trial_used": True,
            "trial_expires": expires.isoformat(),
            "trial_activated_at": datetime.now().isoformat(),
            "vpn_username": vpn_username
        }

        self._save_data()
        logger.info(f"Trial активирован: user={user_id}, expires={expires}")

        return {
            "success": True,
            "expires": expires,
            "vpn_username": vpn_username
        }

    def deactivate_trial(self, user_id: int, reason: str = "expired"):
        """Деактивирует пробный период"""
        user_key = str(user_id)
        if user_key in self.data["users"]:
            self.data["users"][user_key]["trial_active"] = False
            self.data["users"][user_key]["trial_deactivated_at"] = datetime.now().isoformat()
            self.data["users"][user_key]["trial_deactivate_reason"] = reason
            self._save_data()
            logger.info(f"Trial деактивирован: user={user_id}, reason={reason}")

    def get_active_trials(self) -> list:
        """Возвращает список пользователей с активным trial"""
        trials = []
        for user_key, user_data in self.data.get("users", {}).items():
            if user_data.get("trial_active", False):
                trials.append({
                    "user_id": int(user_key),
                    "vpn_username": user_data.get("vpn_username"),
                    "expires": user_data.get("trial_expires")
                })
        return trials


# Инициализация менеджера подписок
subscription_manager = SubscriptionManager()


# ============ ФУНКЦИИ РАБОТЫ С ПОДПИСКАМИ И ПЛАТЕЖАМИ ============

async def show_subscription_plans(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = False):
    """Показывает доступные тарифные планы"""
    user = update.effective_user
    
    # Проверяем текущую подписку
    current_sub = subscription_manager.get_subscription(user.id)
    
    if current_sub["active"]:
        expires = current_sub["expires"]
        days_left = (expires - datetime.now()).days if expires else 0
        plan_name = current_sub.get("plan_name", "Неизвестный")
        
        status_text = (
            f"📊 **Ваша текущая подписка:**\n"
            f"• Тариф: {plan_name}\n"
            f"• Осталось: {days_left} дней\n"
            f"• До: {expires.strftime('%d.%m.%Y') if expires else 'Бессрочно'}\n\n"
        )
    else:
        status_text = "⚠️ **У вас нет активной подписки**\n\n"
    
    plans_text = "🌟 **Выберите тарифный план:**\n\n"
    
    for plan_id, plan in SUBSCRIPTION_PLANS.items():
        plans_text += (
            f"**{plan['emoji']} {plan['name']}** - {plan['stars']}⭐\n"
            f"   • Срок: {plan['days']} дней\n"
            f"   • Трафик: Безлимит\n\n"
        )
    
    message_text = (
        f"🌐 **Ordaflow Proxy Service**\n\n"
        f"{status_text}"
        f"{plans_text}"
        f"💫 Оплата через Telegram Stars\n"
        f"✅ Мгновенная активация\n"
        f"♾️ Безлимитный трафик"
    )
    
    keyboard = [
        [InlineKeyboardButton(f"📅 1 месяц - {SUBSCRIPTION_PLANS['month1']['stars']}⭐", callback_data="buy_month1")],
    ]
    
    # Если есть активная подписка, добавляем кнопку статуса
    if current_sub["active"]:
        keyboard.append([InlineKeyboardButton("📊 Мой статус", callback_data="my_status")])
    
    keyboard.append([InlineKeyboardButton("❓ Помощь", callback_data="help_subscription")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if edit_message and update.callback_query:
        await update.callback_query.edit_message_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )


async def send_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: str):
    """Отправляет счет для оплаты звездами Telegram"""
    query = update.callback_query
    await query.answer()
    
    if plan_id not in SUBSCRIPTION_PLANS:
        await query.edit_message_text("❌ Неизвестный тарифный план")
        return
    
    plan = SUBSCRIPTION_PLANS[plan_id]
    user = update.effective_user
    
    # Формируем payload (данные о заказе)
    payload = f"{plan_id}_{user.id}_{int(datetime.now().timestamp())}"
    
    title = f"Proxy {plan['name']}"
    description = f"{plan['description']}\n• Срок: {plan['days']} дней\n• Безлимитный трафик"
    
    # Цена в Stars (для Telegram Stars provider_token пустой, currency=XTR)
    prices = [LabeledPrice(label=f"Proxy {plan['name']}", amount=plan['stars'])]
    
    try:
        # Отправляем счет
        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title=title,
            description=description,
            payload=payload,
            provider_token="",  # Пустой для Telegram Stars
            currency="XTR",     # Валюта - Telegram Stars
            prices=prices,
            start_parameter=f"vpn_{plan_id}",
            photo_url="https://i.imgur.com/vpn_logo.png",  # Можно заменить на свой логотип
            photo_width=512,
            photo_height=512,
            need_name=False,
            need_phone_number=False,
            need_email=False,
            need_shipping_address=False,
            is_flexible=False
        )
        
        # Удаляем предыдущее сообщение с тарифами
        try:
            await query.message.delete()
        except:
            pass
            
    except Exception as e:
        logger.error(f"Ошибка отправки счета: {e}")
        await query.edit_message_text(
            f"❌ **Ошибка создания счета**\n\n"
            f"Попробуйте позже или обратитесь в поддержку.\n"
            f"Ошибка: {str(e)}",
            parse_mode="Markdown"
        )


async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик PreCheckoutQuery - подтверждение платежа"""
    query = update.pre_checkout_query
    
    # Проверяем payload
    payload = query.invoice_payload
    parts = payload.split("_")
    
    if len(parts) < 3:
        await query.answer(ok=False, error_message="Некорректные данные платежа")
        return
    
    plan_id = parts[0]
    
    if plan_id not in SUBSCRIPTION_PLANS:
        await query.answer(ok=False, error_message="Неизвестный тарифный план")
        return
    
    # Подтверждаем платеж (ВАЖНО: ответить нужно в течение 10 секунд!)
    await query.answer(ok=True)
    logger.info(f"PreCheckout подтвержден: user={query.from_user.id}, plan={plan_id}")


async def notify_admin_payment(context: ContextTypes.DEFAULT_TYPE, user, plan: dict, stars: int, payment_id: str):
    """Отправляет уведомление админу о новом платеже"""
    if not ADMIN_CHAT_ID:
        logger.warning("ADMIN_CHAT_ID не установлен - уведомления отключены")
        return
    
    try:
        admin_id = int(ADMIN_CHAT_ID)
        
        # Формируем информацию о пользователе
        user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
        user_name = user.full_name or user.first_name or "Неизвестно"
        
        message = (
            f"💰 **Новый платеж!**\n\n"
            f"👤 **Пользователь:** {user_name}\n"
            f"📱 **Контакт:** {user_info}\n"
            f"🆔 **User ID:** `{user.id}`\n\n"
            f"📦 **Тариф:** {plan['emoji']} {plan['name']}\n"
            f"⭐ **Оплачено:** {stars} звезд\n"
            f"📅 **Срок:** {plan['days']} дней\n\n"
            f"🧾 **Payment ID:**\n`{payment_id}`\n\n"
            f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
        )
        
        await context.bot.send_message(
            chat_id=admin_id,
            text=message,
            parse_mode="Markdown"
        )
        
        logger.info(f"Уведомление о платеже отправлено админу {admin_id}")
        
    except ValueError:
        logger.error(f"Неверный формат ADMIN_CHAT_ID: {ADMIN_CHAT_ID}")
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления админу: {e}")


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик успешного платежа - создаем VPN аккаунт"""
    payment = update.message.successful_payment
    user = update.effective_user
    
    # Парсим payload
    payload = payment.invoice_payload
    parts = payload.split("_")
    plan_id = parts[0] if parts else "month1"
    
    plan = SUBSCRIPTION_PLANS.get(plan_id, SUBSCRIPTION_PLANS["month1"])
    
    # ID платежа от Telegram
    payment_id = payment.telegram_payment_charge_id
    stars_paid = payment.total_amount
    
    logger.info(f"Успешный платеж: user={user.id}, plan={plan_id}, stars={stars_paid}, payment_id={payment_id}")
    
    # Сохраняем информацию о платеже
    subscription_manager.save_payment(user.id, payment_id, plan_id, stars_paid)
    
    # Уведомляем админа о платеже
    await notify_admin_payment(context, user, plan, stars_paid, payment_id)
    
    # Отправляем сообщение о процессе
    msg = await update.message.reply_text(
        f"✅ **Оплата получена!**\n\n"
        f"💫 Звезд оплачено: {stars_paid}⭐\n"
        f"📦 Тариф: {plan['name']}\n\n"
        f"🔄 Создаю ваш аккаунт...",
        parse_mode="Markdown"
    )
    
    # Генерируем имя пользователя
    vpn_username = f"tg_{user.id}"

    # Деактивируем trial если был активен
    trial_status = subscription_manager.get_trial_status(user.id)
    if trial_status.get("active"):
        subscription_manager.deactivate_trial(user.id, "upgraded_to_paid")
        logger.info(f"Trial деактивирован после оплаты: user={user.id}")
    
    # Получаем токен API
    token_result = api_manager.get_access_token()
    
    if not token_result["success"]:
        await msg.edit_text(
            f"✅ **Оплата получена!**\n\n"
            f"⚠️ Временные проблемы с сервером.\n"
            f"Ваша подписка сохранена.\n\n"
            f"Попробуйте позже: /myvpn",
            parse_mode="Markdown"
        )
        # Все равно активируем подписку
        subscription_manager.activate_subscription(user.id, plan_id, payment_id, vpn_username)
        return
    
    # Проверяем, есть ли уже аккаунт
    user_info = api_manager.get_user_info(vpn_username)
    
    if user_info["success"]:
        # Пользователь существует - продлеваем/обновляем
        # Обновляем на безлимит и новый срок (важно при переходе с trial)
        expire_timestamp = int((datetime.now() + timedelta(days=plan["days"])).timestamp())
        update_data = {
            "data_limit": 0,  # безлимит
            "expire": expire_timestamp,
            "status": "active"
        }
        headers = {
            "Authorization": f"Bearer {api_manager.access_token}",
            "Content-Type": "application/json"
        }
        api_manager._make_request(f"/api/user/{vpn_username}", "PUT", update_data, headers)

        subscription_url_result = api_manager.get_subscription_url(vpn_username)
        subscription_url = subscription_url_result.get("subscription_url", f"{ORDAFLOW_API_URL}/sub/{vpn_username}")
        
        # Активируем подписку
        sub_result = subscription_manager.activate_subscription(user.id, plan_id, payment_id, vpn_username)
        
        expires = sub_result.get("expires")
        expire_text = expires.strftime('%d.%m.%Y') if expires else "Неизвестно"
        
        await msg.edit_text(
            f"🎉 **Подписка активирована!**\n\n"
            f"📦 **Тариф:** {plan['name']}\n"
            f"⏳ **Срок:** {plan['days']} дней (до {expire_text})\n"
            f"♾️ **Трафик:** Безлимит\n\n"
            f"👤 **Ваш аккаунт:** `{vpn_username}`\n\n"
            f"🔗 **Ссылка на подписку:**\n"
            f"`{subscription_url}`\n\n"
            f"📱 **Как подключиться:**\n"
            f"1️⃣ Скопируйте ссылку\n"
            f"2️⃣ Откройте proxy-клиент\n"
            f"3️⃣ Добавьте подписку\n"
            f"4️⃣ Подключайтесь!\n\n"
            f"💾 Сохраните это сообщение!",
            parse_mode="Markdown"
        )
        
    else:
        # Создаем нового пользователя с безлимитным трафиком
        result = api_manager.create_vpn_user(vpn_username, user.username,
                                              data_limit_bytes=0,
                                              expire_days=plan["days"])
        
        if result["success"]:
            subscription_url = result.get("subscription_url", f"{ORDAFLOW_API_URL}/sub/{vpn_username}")
            
            # Активируем подписку
            sub_result = subscription_manager.activate_subscription(user.id, plan_id, payment_id, vpn_username)
            
            expires = sub_result.get("expires")
            expire_text = expires.strftime('%d.%m.%Y') if expires else "Неизвестно"
            
            await msg.edit_text(
                f"🎉 **Аккаунт создан и активирован!**\n\n"
                f"📦 **Тариф:** {plan['name']}\n"
                f"⏳ **Срок:** {plan['days']} дней (до {expire_text})\n"
                f"♾️ **Трафик:** Безлимит\n\n"
                f"👤 **Ваш аккаунт:** `{vpn_username}`\n\n"
                f"🔗 **Ваша ссылка на подписку:**\n"
                f"`{subscription_url}`\n\n"
                f"📱 **Как подключиться:**\n"
                f"1️⃣ Скопируйте ссылку выше\n"
                f"2️⃣ Откройте proxy-клиент (V2rayN, Nekoray, Shadowrocket)\n"
                f"3️⃣ Добавьте подписку по ссылке\n"
                f"4️⃣ Подключитесь!\n\n"
                f"💾 Сохраните это сообщение!",
                parse_mode="Markdown"
            )
        else:
            # Ошибка создания, но подписку все равно активируем
            subscription_manager.activate_subscription(user.id, plan_id, payment_id, vpn_username)
            
            await msg.edit_text(
                f"✅ **Оплата получена!**\n\n"
                f"⚠️ Не удалось автоматически создать аккаунт.\n"
                f"Ваша подписка активирована.\n\n"
                f"Используйте /myvpn для повторной попытки\n"
                f"или обратитесь в /paysupport",
                parse_mode="Markdown"
            )
    
    # Отправляем отдельное сообщение со ссылкой
    if 'subscription_url' in locals() and subscription_url:
        await update.message.reply_text(
            f"📋 **Ссылка для копирования:**\n\n{subscription_url}",
            parse_mode="Markdown"
        )


async def buy_plan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок покупки тарифа"""
    query = update.callback_query
    data = query.data
    
    if data == "buy_month1":
        await send_invoice(update, context, "month1")


async def activate_trial_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Попробовать бесплатно'"""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    # Проверяем, использовал ли уже trial
    if subscription_manager.has_used_trial(user.id):
        await query.edit_message_text(
            f"⚠️ **Пробный период уже использован**\n\n"
            f"Вы уже воспользовались бесплатным пробным периодом.\n"
            f"Оформите подписку, чтобы продолжить пользоваться сервисом!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Оформить подписку (50⭐)", callback_data="buy_month1")],
            ])
        )
        return

    # Проверяем, есть ли активная подписка
    sub = subscription_manager.get_subscription(user.id)
    if sub["active"]:
        await query.edit_message_text(
            f"✅ У вас уже есть активная подписка!\n"
            f"Используйте /myvpn для просмотра статуса.",
            parse_mode="Markdown"
        )
        return

    await query.edit_message_text(
        f"🔄 **Активирую пробный период...**\n\n"
        f"⏳ Создаю ваш аккаунт...",
        parse_mode="Markdown"
    )

    vpn_username = f"tg_{user.id}"

    # Получаем токен API
    token_result = api_manager.get_access_token()
    if not token_result["success"]:
        await query.edit_message_text(
            f"⚠️ **Временные проблемы с сервером**\n\n"
            f"Попробуйте позже через /start",
            parse_mode="Markdown"
        )
        return

    # Проверяем, есть ли уже аккаунт
    user_info = api_manager.get_user_info(vpn_username)

    if user_info["success"]:
        # Аккаунт уже есть — обновляем его для trial (3 дня, 3 GB)
        # Деактивируем и создадим заново для чистоты
        subscription_url_result = api_manager.get_subscription_url(vpn_username)
        subscription_url = subscription_url_result.get("subscription_url", f"{ORDAFLOW_API_URL}/sub/{vpn_username}")
    else:
        # Создаём новый аккаунт с ограничениями trial
        result = api_manager.create_vpn_user(
            vpn_username,
            user.username,
            data_limit_bytes=TRIAL_CONFIG["traffic_limit_bytes"],
            expire_days=TRIAL_CONFIG["days"]
        )

        if not result["success"]:
            await query.edit_message_text(
                f"❌ **Не удалось создать аккаунт**\n\n"
                f"Попробуйте позже через /start\n"
                f"или обратитесь в /paysupport",
                parse_mode="Markdown"
            )
            return

        subscription_url = result.get("subscription_url", f"{ORDAFLOW_API_URL}/sub/{vpn_username}")

    # Активируем trial в системе
    trial_result = subscription_manager.activate_trial(user.id, vpn_username)

    if not trial_result["success"]:
        await query.edit_message_text(
            f"⚠️ {trial_result.get('error', 'Ошибка')}",
            parse_mode="Markdown"
        )
        return

    expires = trial_result["expires"]
    expire_text = expires.strftime('%d.%m.%Y %H:%M')

    await query.edit_message_text(
        f"🎉 **Пробный период активирован!**\n\n"
        f"🆓 **Бесплатно на {TRIAL_CONFIG['days']} дня**\n"
        f"📊 **Лимит трафика:** {TRIAL_CONFIG['traffic_limit_gb']} GB\n"
        f"⏳ **Действует до:** {expire_text}\n\n"
        f"👤 **Ваш аккаунт:** `{vpn_username}`\n\n"
        f"🔗 **Ссылка на подписку:**\n"
        f"`{subscription_url}`\n\n"
        f"📱 **Как подключиться:**\n"
        f"1️⃣ Скопируйте ссылку\n"
        f"2️⃣ Откройте proxy-клиент (V2rayN, Nekoray, Shadowrocket)\n"
        f"3️⃣ Добавьте подписку по ссылке\n"
        f"4️⃣ Подключитесь!\n\n"
        f"💡 _После окончания пробного периода вы сможете оформить подписку за 50⭐/мес_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Открыть ссылку", url=subscription_url)],
            [InlineKeyboardButton("📊 Мой статус", callback_data="my_status")],
        ])
    )

    # Отправляем ссылку отдельным сообщением для копирования
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"📋 **Ссылка для копирования:**\n\n{subscription_url}",
        parse_mode="Markdown"
    )

    # Уведомляем админа
    if ADMIN_CHAT_ID:
        try:
            admin_text = (
                f"🆓 **Новый trial-пользователь!**\n\n"
                f"👤 {user.first_name} (@{user.username or 'нет'})\n"
                f"🆔 ID: `{user.id}`\n"
                f"⏳ До: {expire_text}\n"
                f"📊 Лимит: {TRIAL_CONFIG['traffic_limit_gb']} GB"
            )
            await context.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=admin_text,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления админа о trial: {e}")


async def check_trials_job(app: Application):
    """Фоновая задача проверки истечения пробных периодов"""
    while True:
        try:
            await asyncio.sleep(300)  # Проверяем каждые 5 минут
            
            active_trials = subscription_manager.get_active_trials()
            now = datetime.now()

            for trial in active_trials:
                user_id = trial["user_id"]
                vpn_username = trial["vpn_username"]
                expires_str = trial.get("expires")

                if not expires_str:
                    continue

                expires = datetime.fromisoformat(expires_str)
                time_expired = now > expires

                # Проверяем трафик через API
                traffic_expired = False
                try:
                    user_info = api_manager.get_user_info(vpn_username)
                    if user_info["success"]:
                        used_traffic = user_info.get("data", {}).get("used_traffic", 0)
                        if used_traffic >= TRIAL_CONFIG["traffic_limit_bytes"]:
                            traffic_expired = True
                except Exception as e:
                    logger.error(f"Ошибка проверки трафика trial {vpn_username}: {e}")

                if time_expired or traffic_expired:
                    reason = "traffic" if traffic_expired else "time"
                    reason_text = "исчерпан лимит трафика" if traffic_expired else "истёк срок"

                    # Деактивируем trial
                    subscription_manager.deactivate_trial(user_id, reason)

                    # Отключаем аккаунт на сервере
                    try:
                        api_manager.set_user_status(vpn_username, "disabled")
                    except Exception as e:
                        logger.error(f"Ошибка отключения trial аккаунта {vpn_username}: {e}")

                    # Отправляем уведомление пользователю
                    try:
                        notification_text = (
                            f"⏰ **Пробный период завершён!**\n\n"
                            f"Причина: {reason_text}\n\n"
                            f"Вам понравился наш сервис? 🐎\n"
                            f"Оформите подписку и продолжайте пользоваться:\n\n"
                            f"📅 **1 месяц — 50⭐**\n"
                            f"♾️ Безлимитный трафик\n"
                            f"🔒 Полная защита данных\n\n"
                            f"_Сёрфи свободно и безопасно. OrdaFlow._"
                        )
                        await app.bot.send_message(
                            chat_id=user_id,
                            text=notification_text,
                            parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("💳 Оформить подписку (50⭐)", callback_data="buy_month1")],
                                [InlineKeyboardButton("📋 Тарифы", callback_data="back_to_plans")],
                            ])
                        )
                        logger.info(f"Trial уведомление отправлено: user={user_id}, reason={reason}")
                    except Exception as e:
                        logger.error(f"Ошибка отправки trial уведомления user={user_id}: {e}")

        except Exception as e:
            logger.error(f"Ошибка в check_trials_job: {e}")


async def myvpn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /myvpn - показывает информацию о VPN и подписке"""
    user = update.effective_user
    vpn_username = f"tg_{user.id}"
    
    # Проверяем подписку
    sub = subscription_manager.get_subscription(user.id)
    
    if not sub["active"]:
        # Проверяем, может есть активный trial
        trial_status = subscription_manager.get_trial_status(user.id)
        if trial_status.get("active"):
            hours_left = trial_status.get("hours_left", 0)
            expires = trial_status.get("expires")
            expire_text = expires.strftime('%d.%m.%Y %H:%M') if expires else "Неизвестно"

            traffic_text = "..."
            user_info = api_manager.get_user_info(vpn_username)
            if user_info["success"]:
                used = user_info.get("data", {}).get("used_traffic", 0)
                used_gb = used / (1024**3)
                traffic_text = f"{used_gb:.2f} / {TRIAL_CONFIG['traffic_limit_gb']} GB"

            subscription_result = api_manager.get_subscription_url(vpn_username)
            subscription_url = subscription_result.get("subscription_url", f"{ORDAFLOW_API_URL}/sub/{vpn_username}")

            await update.message.reply_text(
                f"🆓 **Пробный период**\n\n"
                f"👤 Аккаунт: `{vpn_username}`\n"
                f"📊 Трафик: {traffic_text}\n"
                f"⏳ Осталось: {hours_left} ч. (до {expire_text})\n\n"
                f"🔗 **Ваша ссылка:**\n`{subscription_url}`\n\n"
                f"💡 Хотите безлимит? /subscribe",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📥 Открыть ссылку", url=subscription_url)],
                    [InlineKeyboardButton("💳 Оформить подписку (50⭐)", callback_data="buy_month1")],
                ])
            )
            return

        if sub.get("expired"):
            await update.message.reply_text(
                f"⚠️ **Ваша подписка истекла**\n\n"
                f"Тариф: {sub.get('plan_name', 'Неизвестный')}\n"
                f"Истекла: {sub.get('expires').strftime('%d.%m.%Y') if sub.get('expires') else 'Неизвестно'}\n\n"
                f"Используйте /subscribe для продления",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"❌ **У вас нет активной подписки**\n\n"
                f"Используйте /subscribe для оформления подписки",
                parse_mode="Markdown"
            )
        return
    
    # Получаем информацию о VPN аккаунте
    user_info = api_manager.get_user_info(vpn_username)
    
    if user_info["success"]:
        user_data = user_info.get("data", {})
        status = user_data.get("status", "unknown")
        used_traffic = user_data.get("used_traffic", 0)
        
        # Форматируем статус
        status_emoji = "🟢" if status == "active" else "🔴"
        
        # Ссылка на подписку
        subscription_result = api_manager.get_subscription_url(vpn_username)
        subscription_url = subscription_result.get("subscription_url", f"{ORDAFLOW_API_URL}/sub/{vpn_username}")
        
        expires = sub.get("expires")
        days_left = (expires - datetime.now()).days if expires else 0
        
        # Общий трафик
        used_gb = used_traffic / (1024**3)
        
        await update.message.reply_text(
            f"📊 **Ваш статус**\n\n"
            f"**Подписка:**\n"
            f"• Тариф: {sub.get('plan_name', 'Неизвестный')}\n"
            f"• Осталось: {days_left} дней\n"
            f"• До: {expires.strftime('%d.%m.%Y') if expires else 'Неизвестно'}\n\n"
            f"**Аккаунт:**\n"
            f"• Статус: {status_emoji} {status}\n"
            f"• Трафик: {used_gb:.2f} GB (безлимит)\n"
            f"• Аккаунт: `{vpn_username}`\n\n"
            f"🔗 **Ссылка:**\n`{subscription_url}`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"⚠️ **Аккаунт не найден**\n\n"
            f"Подписка активна, но аккаунт отсутствует.\n"
            f"Попробуйте /subscribe для создания.",
            parse_mode="Markdown"
        )


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /subscribe - показывает тарифы"""
    await show_subscription_plans(update, context)


async def paysupport_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /paysupport - поддержка по платежам (требуется Telegram)"""
    await update.message.reply_text(
        "💬 **Поддержка по платежам**\n\n"
        "Если у вас возникли проблемы с оплатой или подпиской:\n\n"
        "1️⃣ Проверьте баланс звезд в Telegram\n"
        "2️⃣ Убедитесь, что платеж прошел успешно\n"
        "3️⃣ Попробуйте команду /myvpn\n\n"
        "📧 Для связи с администратором:\n"
        f"• Напишите в поддержку бота\n\n"
        "🔄 Возврат средств возможен в течение 24 часов\n"
        "после покупки если услуга не была использована.",
        parse_mode="Markdown"
    )


async def help_subscription_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Справка по подписке"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "❓ **Помощь по подпискам**\n\n"
        "**Как оплатить:**\n"
        "1. Выберите тарифный план\n"
        "2. Нажмите кнопку 'Оплатить'\n"
        "3. Подтвердите оплату звездами\n"
        "4. Proxy создастся автоматически!\n\n"
        "**Что такое Telegram Stars:**\n"
        "⭐ Это внутренняя валюта Telegram\n"
        "Купить можно в настройках Telegram\n\n"
        "**Команды:**\n"
        "/subscribe - Тарифы и оплата\n"
        "/myvpn - Статус вашего аккаунта\n"
        "/paysupport - Поддержка\n\n"
        "**Тариф:**\n"
        "📅 1 месяц - 50⭐ (безлимитный трафик)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад к тарифам", callback_data="back_to_plans")]
        ])
    )


async def back_to_plans_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к меню тарифов"""
    query = update.callback_query
    await query.answer()
    await show_subscription_plans(update, context, edit_message=True)


def get_user_daily_limit(user_id: int) -> int:
    """Получает дневной лимит пользователя в байтах (0 = безлимит)"""
    return subscription_manager.get_daily_limit_bytes(user_id)


async def check_and_enforce_daily_limit(username: str, user_id: int = None) -> dict:
    """
    Проверяет дневной лимит пользователя и блокирует при превышении
    
    Returns:
        dict с информацией о статусе лимита
    """
    # Определяем user_id из username если не передан
    if user_id is None:
        try:
            user_id = int(username.replace("tg_", ""))
        except (ValueError, AttributeError):
            user_id = None
    
    # Получаем дневной лимит из подписки
    if user_id:
        user_daily_limit = subscription_manager.get_daily_limit_bytes(user_id)
    else:
        user_daily_limit = DAILY_TRAFFIC_LIMIT
    
    # Получаем текущий трафик из API
    user_info = api_manager.get_user_info(username)
    
    if not user_info["success"]:
        return {"success": False, "message": "Не удалось получить данные пользователя"}
    
    user_data = user_info.get("data", {})
    current_traffic = user_data.get("used_traffic", 0)
    current_status = user_data.get("status", "active")
    
    # Обновляем дневной трафик с учётом лимита пользователя
    daily_info = daily_traffic_manager.update_user_traffic(username, current_traffic, user_daily_limit)
    
    # Для безлимитных тарифов не блокируем
    if daily_info.get("is_unlimited"):
        daily_info["just_blocked"] = False
        daily_info["success"] = True
        return daily_info
    
    # Проверяем превышение лимита
    if daily_info["is_exceeded"] and current_status == "active":
        # Блокируем пользователя
        logger.warning(f"Пользователь {username} превысил дневной лимит! Использовано: {daily_info['daily_used'] / (1024**3):.2f} GB")
        
        result = api_manager.set_user_status(username, "disabled")
        if result["success"]:
            daily_traffic_manager.set_user_blocked(username, True)
            logger.info(f"Пользователь {username} заблокирован до конца дня")
        
        daily_info["just_blocked"] = True
    else:
        daily_info["just_blocked"] = False
    
    daily_info["success"] = True
    return daily_info


async def unblock_daily_limited_users(app: Application):
    """Разблокирует пользователей, заблокированных за дневной лимит"""
    blocked_users = daily_traffic_manager.reset_all_daily()
    
    for username in blocked_users:
        try:
            result = api_manager.set_user_status(username, "active")
            if result["success"]:
                logger.info(f"Пользователь {username} разблокирован (новый день)")
            else:
                logger.error(f"Не удалось разблокировать {username}: {result.get('error')}")
        except Exception as e:
            logger.error(f"Ошибка разблокировки {username}: {e}")


async def daily_reset_job(app: Application):
    """Фоновая задача для ежедневного сброса лимитов в полночь"""
    while True:
        now = datetime.now()
        # Вычисляем время до полуночи
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        seconds_until_midnight = (tomorrow - now).total_seconds()
        
        logger.info(f"Следующий сброс дневных лимитов через {seconds_until_midnight/3600:.1f} часов")
        
        await asyncio.sleep(seconds_until_midnight)
        
        # Выполняем сброс
        logger.info("🔄 Выполняю ежедневный сброс лимитов...")
        await unblock_daily_limited_users(app)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start - проверка подписки и показ меню тарифов"""
    user = update.effective_user
    vpn_username = f"tg_{user.id}"
    
    # Проверяем, есть ли активная подписка
    sub = subscription_manager.get_subscription(user.id)
    
    if sub["active"]:
        # У пользователя есть активная подписка - показываем статус VPN
        msg = await update.message.reply_text(
            f"👋 Привет, {user.first_name}!\n\n"
            f"🔄 Загружаю данные...",
            parse_mode="Markdown"
        )
        
        # Получаем информацию о VPN
        user_info = api_manager.get_user_info(vpn_username)
        
        if user_info["success"]:
            user_data = user_info.get("data", {})
            status = user_data.get("status", "active")
            
            # Ссылка на подписку
            subscription_result = api_manager.get_subscription_url(vpn_username)
            subscription_url = subscription_result.get("subscription_url", f"{ORDAFLOW_API_URL}/sub/{vpn_username}")
            
            # Информация о подписке
            expires = sub.get("expires")
            days_left = (expires - datetime.now()).days if expires else 0
            plan_name = sub.get("plan_name", "Неизвестный")
            
            status_emoji = "🟢" if status == "active" else "🔴"
            
            await msg.edit_text(
                f"✅ **Ваш proxy активен!**\n\n"
                f"👤 **Аккаунт:** `{vpn_username}`\n"
                f"📊 **Статус:** {status_emoji} {status}\n"
                f"♾️ **Трафик:** Безлимит\n\n"
                f"**Подписка:**\n"
                f"• Тариф: {plan_name}\n"
                f"• Осталось: {days_left} дней\n"
                f"• До: {expires.strftime('%d.%m.%Y') if expires else 'Неизвестно'}\n\n"
                f"🔗 **Ваша ссылка:**\n"
                f"`{subscription_url}`\n\n"
                f"📱 Скопируйте и вставьте в proxy-клиент",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📥 Открыть ссылку", url=subscription_url)],
                    [InlineKeyboardButton("🔄 Обновить", callback_data="refresh_subscription")],
                    [InlineKeyboardButton("📊 Подробный статус", callback_data="my_status")],
                    [InlineKeyboardButton("💳 Продлить подписку", callback_data="back_to_plans")]
                ])
            )
            
            # Отдельное сообщение для копирования
            await update.message.reply_text(
                f"📋 **Ссылка для копирования:**\n\n{subscription_url}",
                parse_mode="Markdown"
            )
        else:
            # Подписка есть, но VPN аккаунт не найден - создаем
            await msg.edit_text(
                f"🔄 Создаю аккаунт...",
                parse_mode="Markdown"
            )
            
            # Параметры из подписки
            sub_plan_id = sub.get("plan")
            sub_plan = SUBSCRIPTION_PLANS.get(sub_plan_id, {})
            sub_days = sub_plan.get("days", 30)
            
            result = api_manager.create_vpn_user(vpn_username, user.username,
                                                  data_limit_bytes=0,
                                                  expire_days=sub_days)
            
            if result["success"]:
                subscription_url = result.get("subscription_url", f"{ORDAFLOW_API_URL}/sub/{vpn_username}")
                
                expires = sub.get("expires")
                days_left = (expires - datetime.now()).days if expires else 0
                plan_name = sub.get("plan_name", "Неизвестный")
                
                await msg.edit_text(
                    f"🎉 **Аккаунт создан!**\n\n"
                    f"👤 **Аккаунт:** `{vpn_username}`\n"
                    f"📊 **Статус:** 🟢 active\n"
                    f"♾️ **Трафик:** Безлимит\n\n"
                    f"**Подписка:**\n"
                    f"• Тариф: {plan_name}\n"
                    f"• Осталось: {days_left} дней\n\n"
                    f"🔗 **Ваша ссылка:**\n"
                    f"`{subscription_url}`\n\n"
                    f"📱 Скопируйте и вставьте в proxy-клиент",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📥 Открыть ссылку", url=subscription_url)],
                        [InlineKeyboardButton("📊 Статус", callback_data="my_status")]
                    ])
                )
                
                await update.message.reply_text(
                    f"📋 **Ссылка для копирования:**\n\n{subscription_url}",
                    parse_mode="Markdown"
                )
            else:
                await msg.edit_text(
                    f"⚠️ **Не удалось создать аккаунт**\n\n"
                    f"Ваша подписка активна.\n"
                    f"Попробуйте позже: /myvpn\n\n"
                    f"Или обратитесь в /paysupport",
                    parse_mode="Markdown"
                )
    else:
        # Нет активной подписки — проверяем trial
        trial_status = subscription_manager.get_trial_status(user.id)

        if trial_status.get("active"):
            # Активный trial — показываем статус
            trial_vpn = trial_status["vpn_username"]
            hours_left = trial_status.get("hours_left", 0)
            expires = trial_status.get("expires")
            expire_text = expires.strftime('%d.%m.%Y %H:%M') if expires else "Неизвестно"

            # Получаем трафик
            traffic_text = "..."
            user_info = api_manager.get_user_info(trial_vpn)
            if user_info["success"]:
                used = user_info.get("data", {}).get("used_traffic", 0)
                used_gb = used / (1024**3)
                limit_gb = TRIAL_CONFIG["traffic_limit_gb"]
                traffic_text = f"{used_gb:.2f} / {limit_gb} GB"

            subscription_result = api_manager.get_subscription_url(trial_vpn)
            subscription_url = subscription_result.get("subscription_url", f"{ORDAFLOW_API_URL}/sub/{trial_vpn}")

            await update.message.reply_text(
                f"👋 Привет, {user.first_name}!\n\n"
                f"🆓 **Пробный период активен**\n\n"
                f"👤 Аккаунт: `{trial_vpn}`\n"
                f"📊 Трафик: {traffic_text}\n"
                f"⏳ Осталось: {hours_left} ч. (до {expire_text})\n\n"
                f"🔗 **Ваша ссылка:**\n"
                f"`{subscription_url}`\n\n"
                f"💡 _Хотите безлимит? Оформите подписку!_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📥 Открыть ссылку", url=subscription_url)],
                    [InlineKeyboardButton("📊 Статус", callback_data="my_status")],
                    [InlineKeyboardButton("💳 Оформить подписку (50⭐)", callback_data="buy_month1")],
                ])
            )
            return

        # Нет активной подписки и нет trial
        if sub.get("expired"):
            # Была подписка, но истекла
            await update.message.reply_text(
                f"👋 Привет, {user.first_name}!\n\n"
                f"⚠️ **Ваша подписка истекла**\n"
                f"Тариф: {sub.get('plan_name', 'Неизвестный')}\n\n"
                f"Продлите подписку для продолжения использования сервиса:",
                parse_mode="Markdown"
            )
        elif trial_status.get("used"):
            # Trial уже использован, подписки нет
            await update.message.reply_text(
                f"👋 Привет, {user.first_name}!\n\n"
                f"🌐 **Ordaflow Proxy Service**\n\n"
                f"Ваш пробный период завершён.\n"
                f"Оформите подписку для продолжения:\n\n"
                f"📅 **1 месяц — 50⭐**\n"
                f"♾️ Безлимитный трафик\n"
                f"🔒 Полная защита данных\n\n"
                f"_Сёрфи свободно и безопасно. OrdaFlow._ 🐎",
                parse_mode="Markdown"
            )
        else:
            # Совсем новый пользователь — предлагаем trial
            await update.message.reply_text(
                f"👋 Добро пожаловать, {user.first_name}!\n\n"
                f"🌐 **Ordaflow Proxy Service**\n\n"
                f"Быстрый и надёжный proxy для тех, кто ценит свободу в сети.\n\n"
                f"♾️ Безлимитный трафик — без ограничений\n"
                f"🔒 Ваша безопасность — наш приоритет\n"
                f"⭐ Удобная оплата через Telegram Stars\n\n"
                f"🎁 **Попробуйте бесплатно!**\n"
                f"📅 {TRIAL_CONFIG['days']} дня • 📊 {TRIAL_CONFIG['traffic_limit_gb']} GB трафика\n\n"
                f"🌾 _Ordaflow — как ветер в степи: быстрый, свободный, неудержимый._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎁 Попробовать бесплатно", callback_data="activate_trial")],
                    [InlineKeyboardButton("💳 Сразу оформить подписку (50⭐)", callback_data="buy_month1")],
                ])
            )
            return

        # Показываем меню тарифов
        await show_subscription_plans(update, context)

async def create_vpn_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание VPN аккаунта"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "👤 **Создание аккаунта**\n\n"
        "Введите имя пользователя:\n"
        "(только латинские буквы, цифры и подчеркивание)\n\n"
        "Пример: `vpn_user123` или `user_telegram`",
        parse_mode="Markdown"
    )
    
    context.user_data['state'] = 'awaiting_username'

async def get_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение ссылки на подписку"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "🔗 **Получение ссылки на подписку**\n\n"
        "Введите имя пользователя аккаунта:",
        parse_mode="Markdown"
    )
    
    context.user_data['state'] = 'awaiting_subscription_username'

async def check_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка аккаунта"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "📊 **Проверка аккаунта**\n\n"
        "Введите имя пользователя для проверки:",
        parse_mode="Markdown"
    )
    
    context.user_data['state'] = 'awaiting_check_username'

async def refresh_token_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновление токена"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text("🔄 Обновляю токен доступа...")
    
    result = api_manager.get_access_token()
    
    if result["success"]:
        message = f"✅ **Токен обновлен!**\n\nСрок действия: {result['expiry']}"
    else:
        message = f"❌ **Ошибка:** {result.get('message', 'Неизвестная ошибка')}"
    
    keyboard = [[InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]]
    
    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений"""
    user_input = update.message.text.strip()
    state = context.user_data.get('state')
    
    if not state:
        await update.message.reply_text(
            "Используйте кнопки в меню для выбора действия.\n"
            "Нажмите /start для открытия меню."
        )
        return
    
    if state == 'awaiting_username':
        await process_username(update, context, user_input)
    elif state == 'awaiting_subscription_username':
        await process_subscription_request(update, context, user_input)
    elif state == 'awaiting_check_username':
        await process_check_account(update, context, user_input)

async def process_username(update: Update, context: ContextTypes.DEFAULT_TYPE, username: str):
    """Обработка ввода имени пользователя"""
    if not username:
        await update.message.reply_text("❌ Имя пользователя не может быть пустым.")
        return
    
    if not username.replace('_', '').isalnum():
        await update.message.reply_text(
            "❌ Используйте только латинские буквы, цифры и подчеркивание.\n"
            "Попробуйте еще раз:"
        )
        return
    
    # Очищаем состояние
    context.user_data.pop('state', None)
    
    # Создаем пользователя
    msg = await update.message.reply_text(f"🔄 Создаю аккаунт `{username}`...")
    
    # Получаем telegram username если доступен
    tg_username = update.effective_user.username if update.effective_user else None
    result = api_manager.create_vpn_user(username, tg_username)
    
    if result["success"]:
        subscription_url = result.get("subscription_url", "")
        expire_date = (datetime.now() + timedelta(days=30)).strftime('%d.%m.%Y')
        
        response_text = (
            f"✅ **Аккаунт создан!**\n\n"
            f"**Имя пользователя:** `{username}`\n"
            f"**Статус:** Активный\n"
            f"**Трафик:** 5 GB/день\n"
            f"**Срок:** 30 дней (до {expire_date})\n\n"
        )
        
        if subscription_url:
            response_text += f"**🔗 Ссылка на подписку:**\n`{subscription_url}`\n\n"
            
            keyboard = [
                [InlineKeyboardButton("📥 Открыть ссылку", url=subscription_url)],
                [InlineKeyboardButton("✨ Создать еще", callback_data="create_vpn")],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]
            ]
        else:
            response_text += "⚠️ Ссылка на подписку не сгенерирована.\n"
            response_text += "Проверьте аккаунт в админ панели.\n\n"
            
            keyboard = [
                [InlineKeyboardButton("✨ Создать еще", callback_data="create_vpn")],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]
            ]
        
        response_text += "**Как использовать:**\n"
        response_text += "1. Скопируйте ссылку\n"
        response_text += "2. Вставьте в proxy-клиент\n"
        response_text += "3. Подключитесь!\n\n"
        response_text += "💡 Сохраните это сообщение!"
        
    else:
        error_detail = result.get("error", "Неизвестная ошибка")
        
        response_text = (
            f"❌ **Ошибка создания аккаунта**\n\n"
            f"**Код ошибки:** {result.get('status_code', 'N/A')}\n"
            f"**Детали:** {error_detail}\n\n"
            f"**Возможные причины:**\n"
            f"1. Имя пользователя уже существует\n"
            f"2. Неверный формат данных\n"
            f"3. Проблемы с API\n\n"
            f"Попробуйте другое имя:"
        )
        
        keyboard = [
            [InlineKeyboardButton("🔄 Попробовать снова", callback_data="create_vpn")],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]
        ]
    
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=response_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
            disable_web_page_preview=False
        )
    except Exception as e:
        logger.error(f"Error editing message: {e}")
        await update.message.reply_text(
            response_text,
            reply_markup=InlineKeyboardMarkup(keyboard) if 'keyboard' in locals() else None,
            parse_mode="Markdown"
        )
    
    # Отправляем отдельное сообщение со ссылкой если есть
    if result["success"] and subscription_url:
        await update.message.reply_text(
            f"🔗 **Прямая ссылка для копирования:**\n`{subscription_url}`",
            parse_mode="Markdown"
        )

async def process_subscription_request(update: Update, context: ContextTypes.DEFAULT_TYPE, username: str):
    """Обработка запроса ссылки на подписку"""
    if not username:
        await update.message.reply_text("❌ Имя пользователя не может быть пустым.")
        return
    
    context.user_data.pop('state', None)
    
    msg = await update.message.reply_text(f"🔍 Ищу аккаунт `{username}`...")
    
    # Сначала проверяем существует ли пользователь
    user_info = api_manager.get_user_info(username)
    
    if user_info["success"]:
        user_data = user_info.get("data", {})
        
        # Получаем ссылку на подписку
        subscription_result = api_manager.get_subscription_url(username)
        
        response_text = f"✅ **Аккаунт найден:** `{username}`\n\n"
        response_text += f"**Статус:** {user_data.get('status', 'unknown')}\n"
        
        if subscription_result["success"] and subscription_result.get("subscription_url"):
            subscription_url = subscription_result["subscription_url"]
            response_text += f"\n**🔗 Ссылка на подписку:**\n`{subscription_url}`\n"
            
            keyboard = [
                [InlineKeyboardButton("📥 Открыть ссылку", url=subscription_url)],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]
            ]
        else:
            # Если ссылка не найдена, показываем общую ссылку
            general_url = f"{ORDAFLOW_API_URL}/sub/{username}"
            response_text += f"\n**🔗 Общая ссылка на подписку:**\n`{general_url}`\n"
            response_text += "⚠️ Эта ссылка может не работать, проверьте в админ панели.\n"
            
            keyboard = [
                [InlineKeyboardButton("📥 Попробовать ссылку", url=general_url)],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]
            ]
        
    else:
        response_text = (
            f"❌ Аккаунт `{username}` не найден.\n\n"
            f"**Ошибка:** {user_info.get('message', 'Неизвестная ошибка')}\n\n"
            f"Проверьте правильность имени пользователя."
        )
        keyboard = [[InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]]
    
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=response_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error editing message: {e}")
        await update.message.reply_text(
            response_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

async def process_check_account(update: Update, context: ContextTypes.DEFAULT_TYPE, username: str):
    """Проверка информации об аккаунте"""
    if not username:
        await update.message.reply_text("❌ Имя пользователя не может быть пустым.")
        return
    
    context.user_data.pop('state', None)
    
    msg = await update.message.reply_text(f"📊 Проверяю аккаунт `{username}`...")
    
    user_info = api_manager.get_user_info(username)
    
    if user_info["success"]:
        user_data = user_info.get("data", {})
        
        response_text = f"📊 **Информация об аккаунте:** `{username}`\n\n"
        
        # Основная информация
        response_text += f"**Статус:** {user_data.get('status', 'unknown')}\n"
        
        # Трафик
        used = user_data.get('used_traffic', 0)
        limit = user_data.get('data_limit', 0)
        
        if limit > 0:
            used_gb = used / 1073741824
            limit_gb = limit / 1073741824
            percent = (used_gb / limit_gb * 100) if limit_gb > 0 else 0
            response_text += f"**Трафик:** {used_gb:.2f} GB / {limit_gb:.2f} GB ({percent:.1f}%)\n"
        else:
            response_text += f"**Трафик:** {used / 1073741824:.2f} GB (безлимит)\n"
        
        # Срок действия
        expire = user_data.get('expire', 0)
        if expire > 0:
            expire_date = datetime.fromtimestamp(expire)
            days_left = (expire_date - datetime.now()).days
            response_text += f"**Срок:** {expire_date.strftime('%Y-%m-%d')} ({days_left} дней)\n"
        else:
            response_text += "**Срок:** Бессрочно\n"
        
        # Примечание
        note = user_data.get('note', '')
        if note:
            response_text += f"**Примечание:** {note}\n"
        
        keyboard = [[InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]]
        
    else:
        response_text = f"❌ Аккаунт `{username}` не найден."
        keyboard = [[InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]]
    
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=response_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error editing message: {e}")
        await update.message.reply_text(
            response_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат в меню"""
    query = update.callback_query
    await query.answer()
    
    context.user_data.clear()
    
    user = query.from_user
    vpn_username = f"tg_{user.id}"
    
    # Проверяем статус аккаунта
    user_info = api_manager.get_user_info(vpn_username)
    
    if user_info["success"]:
        subscription_result = api_manager.get_subscription_url(vpn_username)
        if subscription_result["success"] and subscription_result.get("subscription_url"):
            subscription_url = subscription_result["subscription_url"]
        else:
            subscription_url = f"{ORDAFLOW_API_URL}/sub/{vpn_username}"
        
        menu_text = (
            f"🏠 **Главное меню**\n\n"
            f"Привет, {user.first_name}!\n"
            f"Ваш аккаунт: `{vpn_username}`\n\n"
            f"🔗 **Ваша ссылка:**\n`{subscription_url}`"
        )
        
        keyboard = [
            [InlineKeyboardButton("📥 Открыть ссылку", url=subscription_url)],
            [InlineKeyboardButton("🔄 Обновить ссылку", callback_data="refresh_subscription")],
            [InlineKeyboardButton("📊 Статус аккаунта", callback_data="my_status")]
        ]
    else:
        menu_text = (
            f"🏠 **Главное меню**\n\n"
            f"Привет, {user.first_name}!\n"
            f"У вас ещё нет аккаунта."
        )
        
        keyboard = [
            [InlineKeyboardButton("✨ Создать аккаунт", callback_data="retry_create")]
        ]
    
    await query.edit_message_text(
        menu_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def refresh_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновить ссылку на подписку"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    vpn_username = f"tg_{user.id}"
    
    await query.edit_message_text("🔄 Получаю актуальную ссылку...")
    
    subscription_result = api_manager.get_subscription_url(vpn_username)
    
    if subscription_result["success"] and subscription_result.get("subscription_url"):
        subscription_url = subscription_result["subscription_url"]
    else:
        subscription_url = f"{ORDAFLOW_API_URL}/sub/{vpn_username}"
    
    response_text = (
        f"✅ **Ваша ссылка на подписку:**\n\n"
        f"`{subscription_url}`\n\n"
        f"💡 Скопируйте и вставьте в proxy-клиент"
    )
    
    keyboard = [
        [InlineKeyboardButton("📥 Открыть ссылку", url=subscription_url)],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]
    ]
    
    await query.edit_message_text(
        response_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    
    # Отправляем отдельное сообщение для удобного копирования
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"📋 **Ссылка для копирования:**\n\n{subscription_url}",
        parse_mode="Markdown"
    )

async def my_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статус моего аккаунта"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    vpn_username = f"tg_{user.id}"
    
    await query.edit_message_text(f"📊 Проверяю статус аккаунта...")
    
    user_info = api_manager.get_user_info(vpn_username)
    
    if user_info["success"]:
        user_data = user_info.get("data", {})
        
        status = user_data.get("status", "unknown")
        
        if status == "active":
            status_emoji = "✅"
            status_text = "Активный"
        elif status == "disabled":
            status_emoji = "🚫"
            status_text = "Заблокирован"
        else:
            status_emoji = "⚠️"
            status_text = status
        
        # Общий трафик
        used = user_data.get('used_traffic', 0)
        used_gb = used / 1073741824
        total_traffic_text = f"{used_gb:.2f} GB (безлимит)"
        
        # Срок действия
        expire = user_data.get('expire', 0)
        if expire > 0:
            expire_date = datetime.fromtimestamp(expire)
            days_left = (expire_date - datetime.now()).days
            expire_text = f"{expire_date.strftime('%d.%m.%Y')} ({days_left} дн.)"
        else:
            expire_text = "♾️ Бессрочно"
        
        response_text = (
            f"📊 **Статус вашего аккаунта**\n\n"
            f"👤 **Аккаунт:** `{vpn_username}`\n"
            f"{status_emoji} **Статус:** {status_text}\n\n"
            f"♾️ **Трафик:** {total_traffic_text}\n"
            f"⏳ **Срок действия:** {expire_text}\n"
        )
        
        keyboard = [
            [InlineKeyboardButton("🔄 Обновить статус", callback_data="my_status")],
            [InlineKeyboardButton("🔗 Моя ссылка", callback_data="refresh_subscription")],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]
        ]
    else:
        response_text = (
            f"❌ **Аккаунт не найден**\n\n"
            f"Похоже, у вас ещё нет аккаунта.\n"
            f"Нажмите кнопку ниже, чтобы создать."
        )
        
        keyboard = [
            [InlineKeyboardButton("✨ Создать аккаунт", callback_data="retry_create")],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]
        ]
    
    await query.edit_message_text(
        response_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def retry_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повторная попытка создания аккаунта"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    vpn_username = f"tg_{user.id}"
    
    await query.edit_message_text("🔄 Создаю аккаунт...")
    
    # Получаем токен
    token_result = api_manager.get_access_token()
    
    if not token_result["success"]:
        await query.edit_message_text(
            f"❌ **Ошибка подключения**\n\n"
            f"Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data="retry_create")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    # Проверяем существует ли пользователь
    user_info = api_manager.get_user_info(vpn_username)
    
    if user_info["success"]:
        # Уже существует
        subscription_result = api_manager.get_subscription_url(vpn_username)
        if subscription_result["success"] and subscription_result.get("subscription_url"):
            subscription_url = subscription_result["subscription_url"]
        else:
            subscription_url = f"{ORDAFLOW_API_URL}/sub/{vpn_username}"
        
        response_text = (
            f"✅ **Ваш аккаунт уже готов!**\n\n"
            f"👤 **Аккаунт:** `{vpn_username}`\n\n"
            f"🔗 **Ссылка на подписку:**\n`{subscription_url}`"
        )
        
        keyboard = [
            [InlineKeyboardButton("📥 Открыть ссылку", url=subscription_url)],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]
        ]
    else:
        # Создаем нового с безлимитным трафиком
        tg_username = user.username
        sub = subscription_manager.get_subscription(user.id)
        if sub["active"]:
            sub_plan_id = sub.get("plan")
            sub_plan = SUBSCRIPTION_PLANS.get(sub_plan_id, {})
            sub_days = sub_plan.get("days", 30)
        else:
            sub_days = 30
        
        result = api_manager.create_vpn_user(vpn_username, tg_username,
                                              data_limit_bytes=0,
                                              expire_days=sub_days)
        
        if result["success"]:
            subscription_url = result.get("subscription_url", f"{ORDAFLOW_API_URL}/sub/{vpn_username}")
            expire_date = (datetime.now() + timedelta(days=sub_days)).strftime('%d.%m.%Y')
            
            response_text = (
                f"🎉 **Аккаунт создан!**\n\n"
                f"👤 **Аккаунт:** `{vpn_username}`\n"
                f"♾️ **Трафик:** Безлимит\n"
                f"⏳ **Срок:** {sub_days} дней (до {expire_date})\n\n"
                f"🔗 **Ссылка на подписку:**\n`{subscription_url}`"
            )
            
            keyboard = [
                [InlineKeyboardButton("📥 Открыть ссылку", url=subscription_url)],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]
            ]
        else:
            response_text = (
                f"❌ **Ошибка создания**\n\n"
                f"{result.get('error', 'Неизвестная ошибка')}"
            )
            
            keyboard = [
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data="retry_create")],
                [InlineKeyboardButton("📞 Поддержка", callback_data="support")]
            ]
    
    await query.edit_message_text(
        response_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    
    # Отправляем ссылку отдельным сообщением
    if 'subscription_url' in locals() and subscription_url:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"📋 **Ссылка для копирования:**\n\n{subscription_url}",
            parse_mode="Markdown"
        )

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поддержка"""
    query = update.callback_query
    await query.answer()
    
    response_text = (
        f"📞 **Поддержка**\n\n"
        f"Если у вас возникли проблемы:\n\n"
        f"1️⃣ Попробуйте нажать /start еще раз\n"
        f"2️⃣ Проверьте интернет соединение\n"
        f"3️⃣ Убедитесь что proxy-клиент обновлен\n\n"
        f"Если проблема не решена, обратитесь к администратору."
    )
    
    keyboard = [
        [InlineKeyboardButton("🔄 Попробовать снова", callback_data="retry_create")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]
    ]
    
    await query.edit_message_text(
        response_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда помощи"""
    help_text = (
        "🤖 **Ordaflow Proxy — Помощь**\n\n"
        "**Основные команды:**\n"
        "/start - Главное меню и статус\n"
        "/subscribe - Тарифы и оплата\n"
        "/myvpn - Статус вашего аккаунта\n"
        "/paysupport - Помощь с оплатой\n"
        "/help - Эта справка\n"
        "/admin - Админ панель\n\n"
        "**Как подключиться:**\n"
        "1. Выберите тарифный план\n"
        "2. Оплатите звездами Telegram ⭐\n"
        "3. Proxy создастся автоматически!\n"
        "4. Скопируйте ссылку в proxy-клиент\n\n"
        "**Тариф:**\n"
        "📅 1 месяц (50⭐) — безлимитный трафик\n\n"
        "**Проблемы?**\n"
        "• /paysupport - помощь с оплатой\n"
        "• Проверьте баланс звезд в Telegram"
    )
    
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда админ-панели"""
    if ADMIN_PANEL_URL:
        message = (
            "🔧 **Администрирование**\n\n"
            f"• **Админ панель:** [открыть]({ADMIN_PANEL_URL})\n"
            f"• **Текущий админ:** `{ADMIN_USERNAME}`\n\n"
            f"**В админ панели вы можете:**\n"
            f"• Управлять пользователями\n"
            f"• Смотреть статистику\n"
            f"• Настраивать сервера\n"
            f"• Генерировать ссылки"
        )
    else:
        message = (
            "🔧 **Администрирование**\n\n"
            f"• **Текущий админ:** `{ADMIN_USERNAME}`\n"
            f"• **API URL:** `{ORDAFLOW_API_URL}`\n\n"
            f"⚠️ ADMIN_PANEL_URL не настроен в .env файле"
        )
    
    await update.message.reply_text(
        message,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

def main():
    """Запуск бота"""
    print("=" * 60)
    print("Ordaflow Proxy Service")
    print("=" * 60)
    print(f"Telegram Token: {TELEGRAM_TOKEN[:10]}...")
    print(f"Admin Username: {ADMIN_USERNAME}")
    print(f"API URL: {ORDAFLOW_API_URL}")
    print("Bot started! Use /start in Telegram")
    print("  Plans: 1 month (50 stars)")
    print("  Trial: 3 days, 3 GB")
    print("  Unlimited traffic")
    print("=" * 60)
    
    try:
        application = Application.builder().token(TELEGRAM_TOKEN).build()
        
        # ========== ОБРАБОТЧИКИ ПЛАТЕЖЕЙ (должны быть первыми!) ==========
        # PreCheckout - КРИТИЧЕСКИ ВАЖНО отвечать быстро!
        application.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
        
        # Успешный платеж
        application.add_handler(MessageHandler(
            filters.SUCCESSFUL_PAYMENT,
            successful_payment_handler
        ))
        
        # ========== КОМАНДЫ ==========
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("admin", admin_command))
        application.add_handler(CommandHandler("subscribe", subscribe_command))
        application.add_handler(CommandHandler("myvpn", myvpn_command))
        application.add_handler(CommandHandler("paysupport", paysupport_command))
        
        # ========== ОБРАБОТЧИКИ КНОПОК ПОДПИСКИ ==========
        application.add_handler(CallbackQueryHandler(buy_plan_handler, pattern="^buy_"))
        application.add_handler(CallbackQueryHandler(activate_trial_handler, pattern="^activate_trial$"))
        application.add_handler(CallbackQueryHandler(help_subscription_handler, pattern="^help_subscription$"))
        application.add_handler(CallbackQueryHandler(back_to_plans_handler, pattern="^back_to_plans$"))
        
        # ========== ОБРАБОТЧИКИ КНОПОК VPN ==========
        application.add_handler(CallbackQueryHandler(refresh_subscription, pattern="^refresh_subscription$"))
        application.add_handler(CallbackQueryHandler(my_status, pattern="^my_status$"))
        application.add_handler(CallbackQueryHandler(retry_create, pattern="^retry_create$"))
        application.add_handler(CallbackQueryHandler(support, pattern="^support$"))
        
        # Обработчики кнопок - старые (для совместимости)
        application.add_handler(CallbackQueryHandler(create_vpn_account, pattern="^create_vpn$"))
        application.add_handler(CallbackQueryHandler(get_subscription, pattern="^get_subscription$"))
        application.add_handler(CallbackQueryHandler(check_account, pattern="^check_account$"))
        application.add_handler(CallbackQueryHandler(refresh_token_cmd, pattern="^refresh_token$"))
        application.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
        
        # ========== ТЕКСТОВЫЕ СООБЩЕНИЯ ==========
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, 
            handle_message
        ))
        
        # Запускаем фоновую задачу для сброса дневных лимитов
        async def post_init(app: Application):
            await app.bot.set_my_commands([
                BotCommand("start", "Главное меню"),
                BotCommand("subscribe", "Тарифы и оплата"),
                BotCommand("myvpn", "Мой аккаунт"),
                BotCommand("help", "Помощь"),
                BotCommand("paysupport", "Поддержка"),
            ])
            asyncio.create_task(daily_reset_job(app))
            asyncio.create_task(check_trials_job(app))
            logger.info("Команды бота установлены, фоновые задачи запущены")
        
        application.post_init = post_init
        
        # Запускаем бота
        logger.info("Бот запускается...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"Ошибка запуска: {e}")
        print(f"\n❌ Ошибка: {e}")
        input("\nНажмите Enter для выхода...")

if __name__ == "__main__":
    main()