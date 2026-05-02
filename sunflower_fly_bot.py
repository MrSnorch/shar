#!/usr/bin/env python3
"""
Sunflower Land — Floating Island Flight Schedule Bot
Запускается одиночно (GitHub Actions + cron-job.org).
Состояние хранится в fly_bot_state.json (коммитится обратно в репо).

GitHub Secrets:
  TELEGRAM_TOKEN        — токен бота от @BotFather
  TELEGRAM_CHANNEL_ID   — @channel или -1001234567890
  SUNFLOWER_BEARER      — JWT из заголовка Authorization в DevTools
  CRONJOB_API_KEY       — API-ключ с cron-job.org
  CRONJOB_JOB_ID        — числовой ID задания на cron-job.org
"""

import base64
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests as _requests                     # для Telegram и cron-job.org
from curl_cffi import requests as cffi_requests  # Chrome TLS fingerprint для Sunflower API

# ─── Настройки ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
SUNFLOWER_BEARER    = os.getenv("SUNFLOWER_BEARER", "")

CRONJOB_API_KEY     = os.getenv("CRONJOB_API_KEY", "")
CRONJOB_JOB_ID      = os.getenv("CRONJOB_JOB_ID", "")

DISPLAY_UTC_OFFSET   = 3
DISPLAY_TZ_NAME      = "Kyiv"
STATE_FILE           = "fly_bot_state.json"
ARRIVAL_WINDOW_MIN   = 12   # окно «свежего» прилёта в минутах
ARRIVAL_DELETE_MIN   = 30   # через сколько минут удалять уведомление о прилёте
EARLY_START_MIN      = 2    # запускаемся за N минут до рейса
FALLBACK_CHECK_HOURS = 6    # резервный интервал если рейсов нет
TOKEN_WARN_HOURS     = 24   # за сколько часов предупреждать об истечении токена
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DISPLAY_TZ = timezone(timedelta(hours=DISPLAY_UTC_OFFSET))
WEEKDAYS   = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ─── Проверка Bearer токена ───────────────────────────────────────────────────

def check_bearer_expiry() -> None:
    """Проверяет срок действия SUNFLOWER_BEARER и уведомляет в канал если истёк или истекает."""
    if not SUNFLOWER_BEARER:
        log.warning("SUNFLOWER_BEARER не задан — пропускаю проверку токена")
        return

    try:
        # JWT состоит из header.payload.signature — декодируем payload (часть [1])
        parts = SUNFLOWER_BEARER.split(".")
        if len(parts) != 3:
            log.warning("SUNFLOWER_BEARER не похож на JWT (частей: %d)", len(parts))
            return

        payload_b64 = parts[1]
        # Дополняем до кратного 4 (base64 padding)
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))

        exp_ts = payload.get("exp")
        if not exp_ts:
            log.warning("В SUNFLOWER_BEARER нет поля 'exp' — проверка срока невозможна")
            return

        exp_dt = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
        now    = datetime.now(timezone.utc)
        diff   = exp_dt - now

        log.info(
            "Bearer токен истекает: %s UTC (через %s)",
            exp_dt.strftime("%d.%m.%Y %H:%M"),
            diff,
        )

        if now >= exp_dt:
            # Токен уже истёк
            expired_ago = now - exp_dt
            hours_ago   = int(expired_ago.total_seconds() // 3600)
            send_message(
                "⚠️ <b>Bearer токен истёк!</b>\n\n"
                f"Срок действия закончился <b>{exp_dt.astimezone(DISPLAY_TZ).strftime('%d.%m.%Y %H:%M')} ({DISPLAY_TZ_NAME})</b>"
                f" — {hours_ago} ч. назад.\n\n"
                "🔑 Обнови токен в GitHub Secrets → <code>SUNFLOWER_BEARER</code>.\n"
                "Инструкция: DevTools → Network → любой запрос к api.sunflower-land.com → заголовок <code>Authorization</code>."
            )
        elif diff <= timedelta(hours=TOKEN_WARN_HOURS):
            # Токен истекает в ближайшие TOKEN_WARN_HOURS часов
            hours_left = int(diff.total_seconds() // 3600)
            mins_left  = int((diff.total_seconds() % 3600) // 60)
            send_message(
                f"⏰ <b>Bearer токен скоро истечёт!</b>\n\n"
                f"Осталось: <b>{hours_left} ч. {mins_left} мин.</b>\n"
                f"Истекает: <b>{exp_dt.astimezone(DISPLAY_TZ).strftime('%d.%m.%Y %H:%M')} ({DISPLAY_TZ_NAME})</b>.\n\n"
                "🔑 Обнови токен в GitHub Secrets → <code>SUNFLOWER_BEARER</code>.\n"
                "Инструкция: DevTools → Network → любой запрос к api.sunflower-land.com → заголовок <code>Authorization</code>."
            )

    except Exception as e:
        log.error("Не удалось проверить срок Bearer токена: %s", e)


# ─── Sunflower Land API ───────────────────────────────────────────────────────

def fetch_schedule() -> Optional[list[dict]]:
    """Запрашивает floatingIsland.schedule с API Sunflower Land."""
    client_version = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    url     = "https://api.sunflower-land.com/session"
    payload = {"clientVersion": client_version, "timezone": "Europe/Kiev", "language": "en"}
    headers = {
        "Content-Type":     "application/json;charset=UTF-8",
        "Accept":           "application/json",
        "Origin":           "https://sunflower-land.com",
        "Referer":          "https://sunflower-land.com/",
        "x-transaction-id": "undefined",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36 OPR/130.0.0.0"
        ),
    }
    if SUNFLOWER_BEARER:
        headers["Authorization"] = f"Bearer {SUNFLOWER_BEARER}"

    try:
        resp = cffi_requests.post(
            url, json=payload, headers=headers, timeout=15,
            impersonate="chrome110",
        )
        resp.raise_for_status()
        data     = resp.json()
        schedule = _extract_schedule(data)
        if schedule is None:
            log.warning("floatingIsland.schedule не найден в ответе")
            log.debug("Ключи верхнего уровня: %s", list(data.keys())[:20])
        return schedule
    except Exception as e:
        log.error("Ошибка запроса к Sunflower API: %s", e)
        return None


def _extract_schedule(data: dict) -> Optional[list]:
    for obj in [data, data.get("state", {}), data.get("gameState", {}), data.get("farm", {})]:
        if isinstance(obj, dict):
            fi = obj.get("floatingIsland", {})
            if isinstance(fi, dict) and "schedule" in fi:
                return fi["schedule"]
    return None


# ─── Форматирование ───────────────────────────────────────────────────────────

def _slot_parts(slot: dict, now: datetime):
    start_dt = datetime.fromtimestamp(slot["startAt"] / 1000, tz=timezone.utc)
    end_dt   = datetime.fromtimestamp(slot["endAt"]   / 1000, tz=timezone.utc)
    start_l  = start_dt.astimezone(DISPLAY_TZ)
    end_l    = end_dt.astimezone(DISPLAY_TZ)
    label    = f"{start_l.strftime('%H:%M')} – {end_l.strftime('%H:%M')}"
    return start_dt, end_dt, label, start_dt <= now <= end_dt


def format_schedule_message(schedule: list[dict]) -> str:
    now = datetime.now(timezone.utc)

    # Сначала ищем активный рейс
    for slot in sorted(schedule, key=lambda s: s["startAt"]):
        start_dt, end_dt, label, active = _slot_parts(slot, now)
        if active:
            return f"❤️ <b>{label} — прилетел!</b>"

    # Иначе — ближайший предстоящий
    upcoming = sorted(
        [s for s in schedule if datetime.fromtimestamp(s["startAt"] / 1000, tz=timezone.utc) > now],
        key=lambda s: s["startAt"],
    )
    if upcoming:
        _, _, label, _ = _slot_parts(upcoming[0], now)
        return label

    return "—"


def format_arrival_message(slot: dict) -> str:
    end_dt = datetime.fromtimestamp(slot["endAt"] / 1000, tz=timezone.utc)
    end_l  = end_dt.astimezone(DISPLAY_TZ)
    return f"❤️ <b>Шар прилетел! Успей слетать до {end_l.strftime('%H:%M')}!</b>"


def schedule_key(schedule: list[dict]) -> str:
    return json.dumps(
        sorted([{"s": s["startAt"], "e": s["endAt"]} for s in schedule], key=lambda x: x["s"])
    )


# ─── Telegram API ─────────────────────────────────────────────────────────────

def tg(method: str, **kwargs) -> Optional[dict]:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    try:
        resp   = _requests.post(url, json=kwargs, timeout=10)
        result = resp.json()
        if not result.get("ok"):
            log.error("Telegram [%s]: %s", method, result.get("description"))
            return None
        return result
    except Exception as e:
        log.error("Telegram request failed [%s]: %s", method, e)
        return None


def send_message(text: str) -> Optional[int]:
    result = tg("sendMessage", chat_id=TELEGRAM_CHANNEL_ID, text=text, parse_mode="HTML")
    if result:
        msg_id = result["result"]["message_id"]
        log.info("Отправлено message_id=%d", msg_id)
        return msg_id
    return None


def delete_message(message_id: int) -> bool:
    result = tg("deleteMessage", chat_id=TELEGRAM_CHANNEL_ID, message_id=message_id)
    if result:
        log.info("Удалено message_id=%d", message_id)
        return True
    return False


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


# ─── Уведомления о прилёте ────────────────────────────────────────────────────

def _wait_and_notify(schedule: list[dict], state: dict) -> None:
    """Отправляет уведомление о прилёте.
    Если до рейса > (EARLY_START_MIN+1) мин — выходим, cron-job.org разбудит точно в нужный момент.
    Если мы уже в окне запуска (≤ EARLY_START_MIN+1 мин) — досыпаем оставшиеся секунды и уведомляем.
    """
    import time as _time

    now      = datetime.now(timezone.utc)
    notified = set(state.get("notified_arrivals", []))

    for slot in sorted(schedule, key=lambda s: s["startAt"]):
        start_dt = datetime.fromtimestamp(slot["startAt"] / 1000, tz=timezone.utc)
        end_dt   = datetime.fromtimestamp(slot["endAt"]   / 1000, tz=timezone.utc)
        slot_id  = str(slot["startAt"])

        if slot_id in notified:
            continue
        if end_dt < now:
            continue

        if start_dt > now:
            wait_sec = (start_dt - now).total_seconds()
            max_wait = (EARLY_START_MIN + 1) * 60  # окно: N+1 минута

            if wait_sec > max_wait:
                # До рейса далеко — cron-job.org запустит скрипт вовремя, выходим
                log.info(
                    "Рейс через %.0f сек (%s UTC) — выхожу, cron-job.org разбудит за %d мин до старта",
                    wait_sec, start_dt.strftime("%H:%M"), EARLY_START_MIN,
                )
                break

            # Мы в окне запуска (≤ EARLY_START_MIN+1 мин) — досыпаем и уведомляем
            log.info(
                "В окне запуска (%.0f сек до рейса %s UTC) — жду и отправлю уведомление...",
                wait_sec, start_dt.strftime("%H:%M"),
            )
            _time.sleep(wait_sec)

        # Рейс активен прямо сейчас (или только что наступил после ожидания)
        if start_dt <= datetime.now(timezone.utc) <= end_dt:
            # Шаг 3 (как в тест-скрипте): редактируем закреп на «❤️ прилетел!»
            pinned_id = state.get("message_id")
            if pinned_id:
                active_text = format_schedule_message(schedule)  # вернёт ❤️ т.к. рейс активен
                log.info("Редактирую закреплённое сообщение на активный рейс...")
                edit_message(pinned_id, active_text)

            log.info("Шар прилетел! Отправляю уведомление...")
            msg_id = send_message(format_arrival_message(slot))
            notified.add(slot_id)
            if msg_id:
                delete_after = datetime.now(timezone.utc) + timedelta(minutes=ARRIVAL_DELETE_MIN)
                state["arrival_msg_id"]    = msg_id
                state["arrival_delete_ts"] = delete_after.timestamp()
                log.info("Удаление запланировано на %s UTC", delete_after.strftime("%H:%M"))
            break  # уведомляем только об одном рейсе за запуск

    current_ids = {str(s["startAt"]) for s in schedule}
    state["notified_arrivals"] = list(notified & current_ids)


def _maybe_delete_arrival(state: dict, schedule: Optional[list] = None) -> None:
    """Удаляет уведомление о прилёте если наступило время, сохранённое в стейте."""
    msg_id    = state.get("arrival_msg_id")
    delete_ts = state.get("arrival_delete_ts")
    if not msg_id or not delete_ts:
        return

    if datetime.now(timezone.utc).timestamp() >= delete_ts:
        log.info("Удаляю уведомление о прилёте (message_id=%d)...", msg_id)
        delete_message(msg_id)
        state["arrival_msg_id"]    = None
        state["arrival_delete_ts"] = None
        # Сбрасываем schedule_key: run() сам обновит закреп на следующий рейс,
        # без двойного редактирования и ошибки "message is not modified"
        state["schedule_key"] = None
    else:
        remaining = delete_ts - datetime.now(timezone.utc).timestamp()
        log.info("Уведомление о прилёте будет удалено через %.0f сек", remaining)


# ─── cron-job.org перепланирование ───────────────────────────────────────────

def reschedule_cronjob(schedule: list[dict], state: dict) -> None:
    """Обновляет расписание задания на cron-job.org.
    Учитывает запланированное удаление уведомления о прилёте — выбирает ближайшее из событий.
    """
    if not CRONJOB_API_KEY or not CRONJOB_JOB_ID:
        log.debug("CRONJOB_API_KEY / CRONJOB_JOB_ID не заданы — пропускаю")
        return

    now         = datetime.now(timezone.utc)
    fallback_dt = now + timedelta(hours=FALLBACK_CHECK_HOURS)

    upcoming = sorted([
        datetime.fromtimestamp(s["startAt"] / 1000, tz=timezone.utc)
        for s in schedule
        if datetime.fromtimestamp(s["startAt"] / 1000, tz=timezone.utc) > now
    ])

    candidates = []

    if upcoming:
        flight_dt = upcoming[0] - timedelta(minutes=EARLY_START_MIN)
        candidates.append((flight_dt, f"следующий рейс в {upcoming[0].strftime('%H:%M UTC')} (запуск в {flight_dt.strftime('%H:%M UTC')})"))

    delete_ts = state.get("arrival_delete_ts")
    if delete_ts:
        delete_dt = datetime.fromtimestamp(delete_ts, tz=timezone.utc)
        if delete_dt > now:
            candidates.append((delete_dt, f"удаление уведомления в {delete_dt.strftime('%H:%M UTC')}"))

    if candidates:
        next_dt, reason = min(candidates, key=lambda x: x[0])
    else:
        next_dt = fallback_dt
        reason  = f"нет рейсов → резервная проверка через {FALLBACK_CHECK_HOURS}ч"

    # Гарантируем что next_dt всегда в будущем — иначе cron-job.org не сработает
    min_future = now + timedelta(minutes=1)
    if next_dt <= now:
        log.warning(
            "Расчётное время запуска %s UTC уже в прошлом — сдвигаю на следующую минуту (%s UTC)",
            next_dt.strftime("%H:%M"), min_future.strftime("%H:%M"),
        )
        next_dt = min_future

    try:
        resp = _requests.patch(
            f"https://api.cron-job.org/jobs/{CRONJOB_JOB_ID}",
            json={"job": {"schedule": {
                "timezone": "UTC",
                "minutes":  [next_dt.minute],
                "hours":    [next_dt.hour],
                "mdays":    [next_dt.day],
                "months":   [next_dt.month],
                "wdays":    [-1],
            }}},
            headers={
                "Authorization": f"Bearer {CRONJOB_API_KEY}",
                "Content-Type":  "application/json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info("cron-job.org перепланирован: %s UTC (%s)",
                 next_dt.strftime("%d.%m %H:%M"), reason)
    except Exception as e:
        log.error("Ошибка обновления cron-job.org: %s", e)


# ─── Состояние ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "message_id": None,
            "schedule_key": None,
            "notified_arrivals": [],
            "arrival_msg_id": None,
            "arrival_delete_ts": None,
        }


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log.info("Состояние сохранено")


# ─── Главная функция ──────────────────────────────────────────────────────────

def run() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHANNEL_ID:
        log.error("TELEGRAM_TOKEN и TELEGRAM_CHANNEL_ID должны быть заданы!")
        sys.exit(1)

    # Проверяем срок действия Bearer токена
    check_bearer_expiry()

    log.info("Запрашиваю расписание...")
    schedule = fetch_schedule()
    if schedule is None:
        log.warning("Не удалось получить расписание, пропускаю итерацию")
        sys.exit(0)

    log.info("Получено %d слотов", len(schedule))

    state   = load_state()
    new_key = schedule_key(schedule)
    text    = format_schedule_message(schedule)

    # 1) Удаляем уведомление о прилёте если пришло время
    _maybe_delete_arrival(state, schedule)

    # 2) Закреплённое сообщение с расписанием — отправляем сразу
    if state["message_id"] is None:
        log.info("Первый запуск — отправляю сообщение...")
        msg_id = send_message(text)
        if msg_id:
            state["message_id"]   = msg_id
            state["schedule_key"] = new_key

    elif new_key != state["schedule_key"]:
        log.info("Расписание изменилось — обновляю сообщение...")
        if edit_message(state["message_id"], text):
            state["schedule_key"] = new_key
        else:
            log.warning("Не смог отредактировать — отправляю новое...")
            msg_id = send_message(text)
            if msg_id:
                state["message_id"]   = msg_id
                state["schedule_key"] = new_key

    else:
        log.info("Расписание не изменилось — пропускаю редактирование")

    # 3) Перепланируем cron-job.org на ближайшее событие (рейс или удаление)
    reschedule_cronjob(schedule, state)

    # 4) Сохраняем стейт — message_id и время следующего запуска зафиксированы.
    #    Это важно сделать ДО ожидания: если джоб прервётся, данные не потеряются.
    save_state(state)

    # 5) Если до рейса ≤ EARLY_START_MIN+1 мин — досыпаем и уведомляем.
    #    Если дольше — _wait_and_notify сразу вернётся (cron разбудит вовремя).
    _wait_and_notify(schedule, state)

    # 6) Если отправили уведомление — arrival_delete_ts теперь заполнен.
    #    Перепланируем крон ещё раз, чтобы он запустился именно для удаления.
    reschedule_cronjob(schedule, state)

    # 7) Сохраняем стейт после уведомления (arrival_msg_id, notified_arrivals)
    save_state(state)


if __name__ == "__main__":
    run()
