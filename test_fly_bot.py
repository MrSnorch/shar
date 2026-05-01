#!/usr/bin/env python3
"""
Тестовый скрипт: симуляция полного цикла
  1. Закреплённое сообщение «ближайший шар» (ожидание)
  2. Уведомление о прилёте
  3. Удаление уведомления

Запуск:
  TELEGRAM_TOKEN=... TELEGRAM_CHANNEL_ID=... python test_fly_bot.py

Опциональные переменные:
  WAIT_SEC   — через сколько секунд «прилетит» шар    (default: 10)
  FLIGHT_SEC — продолжительность рейса в секундах     (default: 20)
  DELETE_SEC — через сколько секунд удалить прилёт    (default: 15)
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

# ─── Настройки ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")

WAIT_SEC   = int(os.getenv("WAIT_SEC",   "10"))
FLIGHT_SEC = int(os.getenv("FLIGHT_SEC", "20"))
DELETE_SEC = int(os.getenv("DELETE_SEC", "15"))

DISPLAY_UTC_OFFSET = 3
DISPLAY_TZ         = timezone(timedelta(hours=DISPLAY_UTC_OFFSET))
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Telegram ─────────────────────────────────────────────────────────────────

def tg(method: str, **kwargs) -> Optional[dict]:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    try:
        resp   = requests.post(url, json=kwargs, timeout=10)
        result = resp.json()
        if not result.get("ok"):
            log.error("Telegram [%s]: %s", method, result.get("description"))
            return None
        return result
    except Exception as e:
        log.error("Telegram error [%s]: %s", method, e)
        return None


def send_message(text: str) -> Optional[int]:
    result = tg("sendMessage", chat_id=TELEGRAM_CHANNEL_ID, text=text, parse_mode="HTML")
    if result:
        msg_id = result["result"]["message_id"]
        log.info("Отправлено message_id=%d", msg_id)
        return msg_id
    return None


def edit_message(message_id: int, text: str) -> bool:
    result = tg("editMessageText",
                chat_id=TELEGRAM_CHANNEL_ID,
                message_id=message_id,
                text=text,
                parse_mode="HTML")
    if result:
        log.info("Отредактировано message_id=%d", msg_id := message_id)
        return True
    return False


def delete_message(message_id: int) -> bool:
    result = tg("deleteMessage", chat_id=TELEGRAM_CHANNEL_ID, message_id=message_id)
    if result:
        log.info("Удалено message_id=%d", message_id)
        return True
    return False


# ─── Форматирование ───────────────────────────────────────────────────────────

def fmt_slot(start_dt: datetime, end_dt: datetime) -> str:
    s = start_dt.astimezone(DISPLAY_TZ).strftime("%H:%M")
    e = end_dt.astimezone(DISPLAY_TZ).strftime("%H:%M")
    return f"{s} – {e}"


def fmt_upcoming(start_dt: datetime, end_dt: datetime) -> str:
    return fmt_slot(start_dt, end_dt)


def fmt_active(start_dt: datetime, end_dt: datetime) -> str:
    label = fmt_slot(start_dt, end_dt)
    return f"🟢 <b>{label} — прилетел!</b>"


def fmt_arrival(end_dt: datetime) -> str:
    end_l = end_dt.astimezone(DISPLAY_TZ).strftime("%H:%M")
    return f"🎈 <b>Шар прилетел! Успей слетать до {end_l}!</b>"


# ─── Симуляция цикла ──────────────────────────────────────────────────────────

def run_test() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHANNEL_ID:
        log.error("Задай TELEGRAM_TOKEN и TELEGRAM_CHANNEL_ID!")
        return

    now      = datetime.now(timezone.utc)
    start_dt = now + timedelta(seconds=WAIT_SEC)
    end_dt   = start_dt + timedelta(seconds=FLIGHT_SEC)

    # Следующий слот — через 8 часов (как в реальном расписании)
    next_start_dt = now + timedelta(hours=8)
    next_end_dt   = next_start_dt + timedelta(minutes=30)

    log.info("=" * 50)
    log.info("ТЕСТ: полный цикл шара")
    log.info("  Прилёт через:    %d сек (%s)", WAIT_SEC,   start_dt.astimezone(DISPLAY_TZ).strftime("%H:%M:%S"))
    log.info("  Длительность:    %d сек", FLIGHT_SEC)
    log.info("  Удаление через:  %d сек после прилёта", DELETE_SEC)
    log.info("  След. рейс:      %s", fmt_upcoming(next_start_dt, next_end_dt))
    log.info("=" * 50)

    # ── Шаг 1: закреплённое сообщение «ожидание» ──────────────────────────────
    log.info("[1/5] Отправляю сообщение с ближайшим рейсом (ID запоминаю для редактирования)...")
    pinned_msg_id = send_message(fmt_upcoming(start_dt, end_dt))
    if not pinned_msg_id:
        log.error("Не удалось отправить закреплённое сообщение, выхожу")
        return

    # ── Шаг 2: ждём прилёта ───────────────────────────────────────────────────
    log.info("[2/5] Жду %d сек до прилёта...", WAIT_SEC)
    for remaining in range(WAIT_SEC, 0, -1):
        print(f"\r  Осталось: {remaining} сек  ", end="", flush=True)
        time.sleep(1)
    print()

    # ── Шаг 3: редактируем закреп + отправляем уведомление о прилёте ──────────
    log.info("[3/5] Шар прилетел!")
    log.info("      Редактирую закреплённое сообщение...")
    edit_message(pinned_msg_id, fmt_active(start_dt, end_dt))
    log.info("      Отправляю уведомление о прилёте...")
    arrival_msg_id = send_message(fmt_arrival(end_dt))

    # ── Шаг 4: ждём и удаляем уведомление ─────────────────────────────────────
    log.info("[4/5] Жду %d сек, потом удаляю уведомление...", DELETE_SEC)
    for remaining in range(DELETE_SEC, 0, -1):
        print(f"\r  Осталось: {remaining} сек  ", end="", flush=True)
        time.sleep(1)
    print()

    if arrival_msg_id:
        delete_message(arrival_msg_id)

    # ── Шаг 5: обновляем закреп на следующий рейс ─────────────────────────────
    log.info("[5/5] Обновляю закреплённое сообщение на следующий рейс...")
    edit_message(pinned_msg_id, fmt_upcoming(next_start_dt, next_end_dt))

    log.info("=" * 50)
    log.info("ТЕСТ ЗАВЕРШЁН ✓")
    log.info("Сообщение (id=%d) показывает следующий рейс.", pinned_msg_id)
    log.info("=" * 50)


if __name__ == "__main__":
    run_test()
