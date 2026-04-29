#!/usr/bin/env python3
"""
Sunflower Land — Floating Island Flight Schedule Bot
Запускается одиночно (через GitHub Actions + cron-job.org).
Состояние хранится в fly_bot_state.json (коммитится обратно в репо).

Env-переменные (задаются в GitHub Secrets):
  TELEGRAM_TOKEN        — токен бота от @BotFather
  TELEGRAM_CHANNEL_ID   — @channel или -1001234567890
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

# ─── Настройки ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")

# Таймзона для отображения (UTC+3 = Kyiv EEST)
DISPLAY_UTC_OFFSET  = 3
DISPLAY_TZ_NAME     = "Kyiv"

# Файл состояния (коммитится в репо)
STATE_FILE          = "fly_bot_state.json"

# Окно уведомления о прилёте: считаем «шар прилетел», если старт был
# не раньше чем ARRIVAL_WINDOW_MIN минут назад (и рейс ещё идёт)
ARRIVAL_WINDOW_MIN  = 12   # чуть больше интервала запуска (10 мин)
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DISPLAY_TZ = timezone(timedelta(hours=DISPLAY_UTC_OFFSET))


# ─── Получение расписания ─────────────────────────────────────────────────────

def fetch_schedule() -> Optional[list[dict]]:
    """Запрашивает floatingIsland.schedule с API Sunflower Land."""
    client_version = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    url = "https://api.sunflower-land.com/session"
    payload = {
        "clientVersion": client_version,
        "timezone": "Europe/Kiev",
        "language": "en",
    }
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json",
        "Origin": "https://sunflower-land.com",
        "Referer": "https://sunflower-land.com/",
        # Этот заголовок обязателен — без него API возвращает 401
        "x-transaction-id": "undefined",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36 OPR/130.0.0.0"
        ),
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        schedule = _extract_schedule(data)
        if schedule is None:
            log.warning("floatingIsland.schedule не найден в ответе")
            log.debug("Ключи верхнего уровня: %s", list(data.keys())[:20])
        return schedule
    except requests.RequestException as e:
        log.error("Ошибка запроса к API: %s", e)
        return None
    except (KeyError, ValueError) as e:
        log.error("Ошибка парсинга: %s", e)
        return None


def _extract_schedule(data: dict) -> Optional[list]:
    candidates = [
        data,
        data.get("state", {}),
        data.get("gameState", {}),
        data.get("farm", {}),
    ]
    for obj in candidates:
        if isinstance(obj, dict):
            fi = obj.get("floatingIsland", {})
            if isinstance(fi, dict) and "schedule" in fi:
                return fi["schedule"]
    return None


# ─── Форматирование расписания ────────────────────────────────────────────────

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _slot_label(slot: dict, now: datetime) -> tuple[datetime, str, bool]:
    """Возвращает (start_dt, метка, активен_прямо_сейчас)."""
    start_dt = datetime.fromtimestamp(slot["startAt"] / 1000, tz=timezone.utc)
    end_dt   = datetime.fromtimestamp(slot["endAt"]   / 1000, tz=timezone.utc)
    start_l  = start_dt.astimezone(DISPLAY_TZ)
    end_l    = end_dt.astimezone(DISPLAY_TZ)
    day      = WEEKDAYS[start_l.weekday()]
    label    = f"{day} {start_l.strftime('%d.%m')} 🕐 {start_l.strftime('%H:%M')} – {end_l.strftime('%H:%M')}"
    active   = start_dt <= now <= end_dt
    return start_dt, label, active, end_dt


def format_schedule_message(schedule: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    lines = ["✈️ <b>Floating Island — расписание полётов</b>", ""]

    upcoming, past = [], []
    for slot in schedule:
        start_dt, label, active, end_dt = _slot_label(slot, now)
        if active:
            upcoming.insert(0, (start_dt, f"🟡 <b>{label}  ← СЕЙЧАС В ВОЗДУХЕ</b>"))
        elif end_dt < now:
            past.append((start_dt, label))
        else:
            upcoming.append((start_dt, label))

    if upcoming:
        lines.append("🟢 <b>Предстоящие / текущие:</b>")
        for _, lbl in upcoming:
            prefix = "" if lbl.startswith("🟡") else "  🛫 "
            lines.append(f"{prefix}{lbl}")
    else:
        lines.append("🔴 <i>Предстоящих полётов пока нет</i>")

    if past:
        lines.append("")
        lines.append("✅ <b>Прошедшие:</b>")
        for _, lbl in past:
            lines.append(f"  <s>{lbl}</s>")

    lines.append("")
    upd = datetime.now(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M")
    lines.append(f"<i>🔄 Обновлено: {upd} ({DISPLAY_TZ_NAME} UTC+{DISPLAY_UTC_OFFSET})</i>")
    return "\n".join(lines)


def format_arrival_message(slot: dict) -> str:
    start_dt = datetime.fromtimestamp(slot["startAt"] / 1000, tz=timezone.utc)
    end_dt   = datetime.fromtimestamp(slot["endAt"]   / 1000, tz=timezone.utc)
    start_l  = start_dt.astimezone(DISPLAY_TZ)
    end_l    = end_dt.astimezone(DISPLAY_TZ)
    until    = end_l.strftime("%H:%M")
    return (
        "🎈 <b>Шар прилетел!</b>\n\n"
        f"Floating Island сейчас <b>в воздухе</b> до <b>{until} ({DISPLAY_TZ_NAME})</b>.\n"
        f"Начало: {start_l.strftime('%H:%M')} – Конец: {until}\n\n"
        "⚡ Успей слетать!"
    )


def schedule_key(schedule: list[dict]) -> str:
    return json.dumps(sorted(
        [{"s": s["startAt"], "e": s["endAt"]} for s in schedule],
        key=lambda x: x["s"],
    ))


# ─── Telegram API ─────────────────────────────────────────────────────────────

def tg(method: str, **kwargs) -> Optional[dict]:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=kwargs, timeout=10)
        result = resp.json()
        if not result.get("ok"):
            log.error("Telegram [%s]: %s", method, result.get("description"))
            return None
        return result
    except requests.RequestException as e:
        log.error("Telegram request failed [%s]: %s", method, e)
        return None


def send_message(text: str, pin: bool = False) -> Optional[int]:
    result = tg("sendMessage", chat_id=TELEGRAM_CHANNEL_ID, text=text, parse_mode="HTML")
    if result:
        msg_id = result["result"]["message_id"]
        log.info("Отправлено message_id=%d", msg_id)
        if pin:
            tg("pinChatMessage", chat_id=TELEGRAM_CHANNEL_ID,
               message_id=msg_id, disable_notification=True)
        return msg_id
    return None


def edit_message(message_id: int, text: str) -> bool:
    result = tg(
        "editMessageText",
        chat_id=TELEGRAM_CHANNEL_ID,
        message_id=message_id,
        text=text,
        parse_mode="HTML",
    )
    if result:
        log.info("Обновлено message_id=%d", message_id)
        return True
    return False


# ─── Состояние ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"message_id": None, "schedule_key": None, "notified_arrivals": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log.info("Состояние сохранено")


# ─── Логика уведомлений о прилёте ────────────────────────────────────────────

def check_and_notify_arrivals(schedule: list[dict], state: dict) -> None:
    """Если шар сейчас в воздухе и мы ещё не уведомляли — шлём алерт."""
    now = datetime.now(timezone.utc)
    window = timedelta(minutes=ARRIVAL_WINDOW_MIN)
    notified: set[str] = set(state.get("notified_arrivals", []))

    for slot in schedule:
        start_dt = datetime.fromtimestamp(slot["startAt"] / 1000, tz=timezone.utc)
        end_dt   = datetime.fromtimestamp(slot["endAt"]   / 1000, tz=timezone.utc)
        slot_id  = str(slot["startAt"])

        # Рейс активен прямо сейчас + старт был не раньше ARRIVAL_WINDOW_MIN назад
        if start_dt <= now <= end_dt and (now - start_dt) <= window:
            if slot_id not in notified:
                log.info("Шар в воздухе! Отправляю уведомление...")
                send_message(format_arrival_message(slot))
                notified.add(slot_id)

    # Чистим старые записи (рейсы, которых уже нет в расписании)
    current_ids = {str(s["startAt"]) for s in schedule}
    state["notified_arrivals"] = list(notified & current_ids)


# ─── Главная функция (одиночный запуск) ──────────────────────────────────────

def run() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHANNEL_ID:
        log.error("TELEGRAM_TOKEN и TELEGRAM_CHANNEL_ID должны быть заданы!")
        sys.exit(1)

    log.info("Запрашиваю расписание...")
    schedule = fetch_schedule()

    if schedule is None:
        log.warning("Не удалось получить расписание")
        sys.exit(0)   # не падаем — просто пропускаем итерацию

    log.info("Получено %d слотов", len(schedule))

    state   = load_state()
    new_key = schedule_key(schedule)
    text    = format_schedule_message(schedule)

    # 1) Уведомление о прилёте (независимо от изменения расписания)
    check_and_notify_arrivals(schedule, state)

    # 2) Обновление/создание закреплённого сообщения с расписанием
    if state["message_id"] is None:
        log.info("Первый запуск — отправляю сообщение с расписанием...")
        msg_id = send_message(text, pin=True)
        if msg_id:
            state["message_id"]   = msg_id
            state["schedule_key"] = new_key

    elif new_key != state["schedule_key"]:
        log.info("Расписание изменилось — обновляю сообщение...")
        ok = edit_message(state["message_id"], text)
        if ok:
            state["schedule_key"] = new_key
        else:
            log.warning("Не смог отредактировать, отправляю новое...")
            msg_id = send_message(text, pin=True)
            if msg_id:
                state["message_id"]   = msg_id
                state["schedule_key"] = new_key

    else:
        log.info("Расписание не изменилось")
        # Всё равно обновляем время в сообщении
        edit_message(state["message_id"], text)

    save_state(state)


if __name__ == "__main__":
    run()
