#!/usr/bin/env python3
"""Receive Zvonok lead webhooks and batch them into a Telegram group."""

from __future__ import annotations

import asyncio
import html
import io
import json
import os
import random
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "leads.sqlite3"))
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
PORT = int(os.environ.get("PORT", "8080"))
DELIVERY_INTERVAL_SECONDS = int(os.environ.get("DELIVERY_INTERVAL_SECONDS", "900"))
OUTREACH_FIRST_DELAY_RANGE = (60, 180)
OUTREACH_BETWEEN_DELAY_RANGE = (1500, 2100)
OUTREACH_RETRY_DELAY_SECONDS = 1800
OUTREACH_POLL_SECONDS = 15
SESSION_PATH = os.environ.get("TELEGRAM_SESSION_PATH", "/data/outreach.session")
USER_API_ID = os.environ.get("TELEGRAM_API_ID")
USER_API_HASH = os.environ.get("TELEGRAM_API_HASH")
USER_PHONE = os.environ.get("TELEGRAM_USER_PHONE")


@dataclass(frozen=True)
class Lead:
    event_id: str
    phone: str
    audio_url: str | None
    campaign_id: str | None
    completed_at: str | None


def init_database() -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                event_id TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                audio_url TEXT,
                campaign_id TEXT,
                completed_at TEXT,
                delivered_at TEXT
            )
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(leads)")}
        if "outreach_due_at" not in columns:
            connection.execute("ALTER TABLE leads ADD COLUMN outreach_due_at REAL")
        if "outreach_status" not in columns:
            connection.execute("ALTER TABLE leads ADD COLUMN outreach_status TEXT DEFAULT 'not_scheduled'")
        if "outreach_sent_at" not in columns:
            connection.execute("ALTER TABLE leads ADD COLUMN outreach_sent_at TEXT")
        if "report_status" not in columns:
            connection.execute("ALTER TABLE leads ADD COLUMN report_status TEXT")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                   key TEXT PRIMARY KEY, value TEXT NOT NULL)"""
        )


def get_setting(key: str) -> str | None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(key: str, value: str) -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def first_value(payload: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return str(value)
    return None


def parse_lead(payload: dict[str, Any]) -> Lead:
    """Normalise the fields shown in Zvonok lead notifications."""
    phone = first_value(payload, "phone", "dst_phone", "client_phone", "ct_phone")
    if not phone:
        raise ValueError("Zvonok webhook has no phone number")

    call_id = first_value(payload, "call_id", "ats_call_id", "call_uuid", "ct_call_id")
    completed_at = first_value(payload, "completed_date", "completed_at", "call_start", "ct_completed")
    event_id = call_id or f"{phone}:{completed_at or json.dumps(payload, sort_keys=True)}"

    audio_url = first_value(payload, "recorded_audio_url", "audio_url", "ct_record_url")
    if audio_url and audio_url.startswith("/"):
        audio_url = f"https://zvonok.com{audio_url}"

    return Lead(
        event_id=event_id,
        phone=phone,
        audio_url=audio_url,
        campaign_id=first_value(payload, "campaign_id", "ats_campaign_id", "ct_campaign_id"),
        completed_at=completed_at,
    )


def save_lead(lead: Lead) -> bool:
    with sqlite3.connect(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO leads
                (event_id, phone, audio_url, campaign_id, completed_at, outreach_status)
            VALUES (?, ?, ?, ?, ?, 'waiting_lead_chat')
            """,
            (lead.event_id, lead.phone, lead.audio_url, lead.campaign_id, lead.completed_at),
        )
    return cursor.rowcount == 1


def telegram_request(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # getUpdates is long-polling for 20 seconds, so the HTTP client needs a
    # slightly longer window than the Bot API request itself.
    with urllib.request.urlopen(request, timeout=35) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected {method}: {result}")
    return result


def format_lead(phone: str, number: int) -> str:
    # Keep the phone as plain text: Telegram then turns a registered number
    # into a native profile/contact link in the client.
    return f"Телефон: {phone}\nЛид №{number}"


def deliver_pending() -> int:
    with sqlite3.connect(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT event_id, phone, audio_url FROM leads
            WHERE delivered_at IS NULL
            ORDER BY rowid
            """
        ).fetchall()

        for event_id, phone, audio_url in rows:
            lead_number = connection.execute(
                """
                SELECT COUNT(*) FROM leads
                WHERE event_id NOT LIKE 'relay-%'
                  AND rowid <= (SELECT rowid FROM leads WHERE event_id = ?)
                """,
                (event_id,),
            ).fetchone()[0]
            message: dict[str, Any] = {
                "chat_id": CHAT_ID,
                "text": format_lead(phone, lead_number),
                "parse_mode": "HTML",
            }
            if audio_url:
                message["reply_markup"] = {
                    "inline_keyboard": [[{"text": "▶️ Запись звонка", "url": audio_url}]]
                }
            telegram_request("sendMessage", message)
            connection.execute(
                """UPDATE leads SET delivered_at = ?, outreach_due_at = ?,
                   outreach_status = CASE WHEN outreach_status = 'waiting_lead_chat'
                                          THEN 'pending' ELSE outreach_status END
                   WHERE event_id = ?""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    time.time() + random.randint(*OUTREACH_FIRST_DELAY_RANGE),
                    event_id,
                ),
            )
        return len(rows)


def delivery_loop() -> None:
    while True:
        try:
            deliver_pending()
        except Exception as error:  # Keep queued leads for the next retry.
            print(f"delivery failed: {error}", flush=True)
        time.sleep(DELIVERY_INTERVAL_SECONDS)


def utf16_index(text: str, offset: int) -> int:
    """Convert Telegram Bot API's UTF-16 offsets to Python string offsets."""
    units = 0
    for index, character in enumerate(text):
        if units >= offset:
            return index
        units += 2 if ord(character) > 0xFFFF else 1
    return len(text)


def message_to_html(message: dict[str, Any]) -> str | None:
    # Telegram uses ``caption`` and ``caption_entities`` for a photo's text.
    text = message.get("text") or message.get("caption")
    if not isinstance(text, str) or not text.strip():
        return None
    tags = {
        "bold": ("<b>", "</b>"), "italic": ("<i>", "</i>"),
        "underline": ("<u>", "</u>"), "strikethrough": ("<s>", "</s>"),
        "code": ("<code>", "</code>"), "pre": ("<pre>", "</pre>"),
    }
    replacements: list[tuple[int, str]] = []
    entities = message.get("entities") or message.get("caption_entities") or []
    for entity in entities:
        entity_type = entity.get("type")
        if entity_type not in tags:
            continue
        start = utf16_index(text, int(entity["offset"]))
        end = utf16_index(text, int(entity["offset"]) + int(entity["length"]))
        opening, closing = tags[entity_type]
        replacements.extend([(start, opening), (end, closing)])
    boundaries: dict[int, list[str]] = {}
    for position, tag in replacements:
        boundaries.setdefault(position, []).append(tag)
    chunks: list[str] = []
    for index, character in enumerate(text):
        chunks.extend(boundaries.get(index, []))
        chunks.append(html.escape(character))
    chunks.extend(boundaries.get(len(text), []))
    return "".join(chunks)


def photo_file_id(message: dict[str, Any]) -> str | None:
    """Return the largest image attached to a Bot API message."""
    photos = message.get("photo")
    if not isinstance(photos, list) or not photos:
        return None
    largest = photos[-1]
    return largest.get("file_id") if isinstance(largest, dict) else None


def configure_from_bot_update(message: dict[str, Any]) -> None:
    chat = message.get("chat", {})
    chat_type = chat.get("type")
    chat_id = str(chat.get("id", ""))
    text = message.get("text", "")
    if "ОТЧЕТ" in str(chat.get("title", "")).upper() and "ЛИД" in str(chat.get("title", "")).upper():
        if get_setting("report_chat_id") != chat_id:
            set_setting("report_chat_id", chat_id)
            telegram_request("sendMessage", {
                "chat_id": chat_id,
                "text": "Журнал рассылки подключён.",
            })
        return
    if chat_type == "private":
        if text.startswith("/start"):
            telegram_request("sendMessage", {
                "chat_id": chat_id,
                "text": "Пришлите первое сообщение клиенту: текст или фото с подписью. Форматирование сохраню.",
            })
            return
        template = message_to_html(message)
        if template:
            set_setting("outreach_template_html", template)
            photo_id = photo_file_id(message)
            if photo_id:
                set_setting("outreach_template_photo_file_id", photo_id)
            else:
                set_setting("outreach_template_photo_file_id", "")
            telegram_request("sendMessage", {
                "chat_id": chat_id,
                "text": "Шаблон сохранён. До авторизации рабочего аккаунта клиентам ничего не отправляется.",
            })
        return
    if text.startswith("/start") and "БЕЗ ТГ" in str(chat.get("title", "")).upper():
        set_setting("no_tg_chat_id", chat_id)
        telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": "Чат для недоступных в Telegram лидов подключён.",
        })


def configure_report_channel(chat: dict[str, Any]) -> None:
    title = str(chat.get("title", "")).upper()
    chat_id = str(chat.get("id", ""))
    if chat_id and get_setting("report_chat_id") != chat_id and "ОТЧЕТ" in title and "ЛИД" in title:
        set_setting("report_chat_id", chat_id)
        telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": "Журнал рассылки подключён.",
        })


def bot_updates_loop() -> None:
    offset = int(get_setting("bot_update_offset") or "0")
    while True:
        try:
            result = telegram_request("getUpdates", {
                "offset": offset,
                "timeout": 20,
                "allowed_updates": ["message", "channel_post", "my_chat_member"],
            })
            for update in result.get("result", []):
                offset = int(update["update_id"]) + 1
                set_setting("bot_update_offset", str(offset))
                if isinstance(update.get("message"), dict):
                    configure_from_bot_update(update["message"])
                if isinstance(update.get("channel_post"), dict):
                    configure_from_bot_update(update["channel_post"])
                member_update = update.get("my_chat_member")
                if isinstance(member_update, dict):
                    configure_report_channel(member_update.get("chat", {}))
        except Exception as error:
            print(f"bot updates failed: {error}", flush=True)
            time.sleep(5)


def lead_number(connection: sqlite3.Connection, event_id: str) -> int:
    return connection.execute(
        """SELECT COUNT(*) FROM leads WHERE event_id NOT LIKE 'relay-%'
           AND rowid <= (SELECT rowid FROM leads WHERE event_id = ?)""",
        (event_id,),
    ).fetchone()[0]


def route_to_no_tg(event_id: str, phone: str, audio_url: str | None, reason: str) -> bool:
    chat_id = get_setting("no_tg_chat_id")
    if not chat_id:
        return False
    with sqlite3.connect(DATABASE_PATH) as connection:
        number = lead_number(connection, event_id)
    message: dict[str, Any] = {
        "chat_id": chat_id,
        "parse_mode": "HTML",
        "text": f"<b>НЕ УДАЛОСЬ НАПИСАТЬ В TG</b>\nТелефон: {phone}\nЛид №{number}\nПричина: {html.escape(reason)}",
    }
    if audio_url:
        message["reply_markup"] = {"inline_keyboard": [[{"text": "▶️ Запись звонка", "url": audio_url}]]}
    telegram_request("sendMessage", message)
    return True


def format_report(number: int, phone: str, event_id: str, status: str) -> str:
    result = {
        "sent": "Отправлено",
        "routed_no_tg": "Не отправлено → второй чат",
    }[status]
    return (
        f"Лид №{number}\n"
        f"Телефон: {html.escape(phone)}\n"
        f"Статус: {result}\n"
        f"ID: <code>{html.escape(event_id)}</code>"
    )


def deliver_outreach_reports() -> int:
    chat_id = get_setting("report_chat_id")
    if not chat_id:
        return 0
    with sqlite3.connect(DATABASE_PATH) as connection:
        rows = connection.execute(
            """SELECT event_id, phone, outreach_status FROM leads
               WHERE outreach_status IN ('sent', 'routed_no_tg')
                 AND report_status IS NULL
               ORDER BY rowid"""
        ).fetchall()
    delivered = 0
    for event_id, phone, status in rows:
        with sqlite3.connect(DATABASE_PATH) as connection:
            number = lead_number(connection, event_id)
        telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": format_report(number, phone, event_id, status),
            "parse_mode": "HTML",
        })
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.execute(
                "UPDATE leads SET report_status = ? WHERE event_id = ? AND report_status IS NULL",
                (status, event_id),
            )
        delivered += 1
    return delivered


def download_bot_photo(file_id: str) -> tuple[io.BytesIO, str]:
    """Download a saved Bot API photo so the working user account can send it."""
    result = telegram_request("getFile", {"file_id": file_id})
    file_path = result["result"]["file_path"]
    with urllib.request.urlopen(
        f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=35
    ) as response:
        content = response.read()
    return io.BytesIO(content), Path(file_path).name


async def attempt_outreach(phone: str, template_html: str, photo_id: str | None = None) -> tuple[str, str]:
    """Return sent, no_tg, retry, or not_authorized plus a human reason."""
    if not (USER_API_ID and USER_API_HASH and USER_PHONE):
        return "not_authorized", "Рабочий Telegram-аккаунт не настроен"
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError, PeerFloodError, SessionPasswordNeededError
    from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
    from telethon.tl.types import InputPhoneContact

    client = TelegramClient(SESSION_PATH, int(USER_API_ID), USER_API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return "not_authorized", "Нужна авторизация рабочего Telegram-аккаунта"
        response = await client(ImportContactsRequest([
            InputPhoneContact(client_id=random.randint(1, 2**63 - 1), phone=phone, first_name="Лид", last_name="")
        ]))
        if not response.users:
            return "no_tg", "Пользователь не найден в Telegram"
        user = response.users[0]
        try:
            if photo_id:
                photo, filename = download_bot_photo(photo_id)
                await client.send_file(
                    user, photo, file_name=filename, caption=template_html, parse_mode="html"
                )
            else:
                await client.send_message(user, template_html, parse_mode="html")
        except (PeerFloodError, FloodWaitError) as error:
            return "retry", f"Telegram ограничил отправку: {error.__class__.__name__}"
        finally:
            await client(DeleteContactsRequest(id=[user]))
        return "sent", "Сообщение отправлено"
    except SessionPasswordNeededError:
        return "not_authorized", "Нужен пароль двухэтапной проверки"
    except Exception as error:
        return "no_tg", f"Telegram не позволил написать: {error.__class__.__name__}"
    finally:
        await client.disconnect()


def outreach_loop() -> None:
    while True:
        try:
            deliver_outreach_reports()
            template = get_setting("outreach_template_html")
            photo_id = get_setting("outreach_template_photo_file_id") or None
            next_allowed = float(get_setting("outreach_next_allowed_at") or "0")
            if not template or time.time() < next_allowed:
                time.sleep(OUTREACH_POLL_SECONDS)
                continue
            with sqlite3.connect(DATABASE_PATH) as connection:
                row = connection.execute(
                    """SELECT event_id, phone, audio_url FROM leads
                       WHERE outreach_status = 'pending' AND outreach_due_at <= ?
                       ORDER BY outreach_due_at LIMIT 1""",
                    (time.time(),),
                ).fetchone()
                if row:
                    connection.execute("UPDATE leads SET outreach_status = 'processing' WHERE event_id = ?", (row[0],))
            if not row:
                time.sleep(OUTREACH_POLL_SECONDS)
                continue
            status, reason = asyncio.run(attempt_outreach(row[1], template, photo_id))
            if status == "sent":
                with sqlite3.connect(DATABASE_PATH) as connection:
                    connection.execute(
                        "UPDATE leads SET outreach_status = 'sent', outreach_sent_at = ? WHERE event_id = ?",
                        (datetime.now(timezone.utc).isoformat(), row[0]),
                    )
                set_setting("outreach_next_allowed_at", str(time.time() + random.randint(*OUTREACH_BETWEEN_DELAY_RANGE)))
            elif status == "no_tg" and route_to_no_tg(*row, reason):
                with sqlite3.connect(DATABASE_PATH) as connection:
                    connection.execute("UPDATE leads SET outreach_status = 'routed_no_tg' WHERE event_id = ?", (row[0],))
                set_setting("outreach_next_allowed_at", str(time.time() + random.randint(*OUTREACH_BETWEEN_DELAY_RANGE)))
            else:
                with sqlite3.connect(DATABASE_PATH) as connection:
                    connection.execute(
                        "UPDATE leads SET outreach_status = 'pending', outreach_due_at = ? WHERE event_id = ?",
                        (time.time() + OUTREACH_RETRY_DELAY_SECONDS, row[0]),
                    )
        except Exception as error:
            print(f"outreach failed: {error}", flush=True)
        time.sleep(OUTREACH_POLL_SECONDS)


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "ZvonokLeadRelay/1.0"

    def log_message(self, format: str, *args: object) -> None:
        print(format % args, flush=True)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(HTTPStatus.OK)
            self.end_headers()
            self.wfile.write(b"ok\n")
            return
        self._receive_get_webhook()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        try:
            if "application/json" in content_type:
                payload = json.loads(raw_body.decode("utf-8"))
            else:
                query = urllib.parse.parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
                payload = {key: values[-1] for key, values in query.items()}
            self._save_payload(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return

    def _receive_get_webhook(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != f"/zvonok/{WEBHOOK_SECRET}":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        try:
            self._save_payload({key: values[-1] for key, values in query.items()})
        except ValueError as error:
            self.send_error(HTTPStatus.BAD_REQUEST, str(error))

    def _save_payload(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != f"/zvonok/{WEBHOOK_SECRET}":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        # Advanced campaigns notify about every answered call; a lead is only digit 1.
        button = first_value(payload, "ct_button_num", "button_num")
        saved = False if button not in (None, "1") else save_lead(parse_lead(payload))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "new": saved}).encode("utf-8"))


def main() -> None:
    init_database()
    threading.Thread(target=delivery_loop, daemon=True).start()
    threading.Thread(target=bot_updates_loop, daemon=True).start()
    threading.Thread(target=outreach_loop, daemon=True).start()
    print(f"listening on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), WebhookHandler).serve_forever()


if __name__ == "__main__":
    main()
