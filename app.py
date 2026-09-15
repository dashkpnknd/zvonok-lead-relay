#!/usr/bin/env python3
"""Receive Zvonok lead webhooks and batch them into a Telegram group."""

from __future__ import annotations

import json
import os
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


def first_value(payload: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return str(value)
    return None


def parse_lead(payload: dict[str, Any]) -> Lead:
    """Normalise the fields shown in Zvonok lead notifications."""
    phone = first_value(payload, "phone", "dst_phone", "client_phone")
    if not phone:
        raise ValueError("Zvonok webhook has no phone number")

    call_id = first_value(payload, "call_id", "ats_call_id", "call_uuid")
    completed_at = first_value(payload, "completed_date", "completed_at", "call_start")
    event_id = call_id or f"{phone}:{completed_at or json.dumps(payload, sort_keys=True)}"

    return Lead(
        event_id=event_id,
        phone=phone,
        audio_url=first_value(payload, "recorded_audio_url", "audio_url"),
        campaign_id=first_value(payload, "campaign_id", "ats_campaign_id"),
        completed_at=completed_at,
    )


def save_lead(lead: Lead) -> bool:
    with sqlite3.connect(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO leads
                (event_id, phone, audio_url, campaign_id, completed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (lead.event_id, lead.phone, lead.audio_url, lead.campaign_id, lead.completed_at),
        )
    return cursor.rowcount == 1


def telegram_request(method: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected {method}: {result}")


def format_lead(phone: str) -> str:
    return f"🟢 <b>Новый лид из Zvonok</b>\nТелефон: <code>{phone}</code>"


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
            message: dict[str, Any] = {
                "chat_id": CHAT_ID,
                "text": format_lead(phone),
                "parse_mode": "HTML",
            }
            if audio_url:
                message["reply_markup"] = {
                    "inline_keyboard": [[{"text": "▶️ Запись звонка", "url": audio_url}]]
                }
            telegram_request("sendMessage", message)
            connection.execute(
                "UPDATE leads SET delivered_at = ? WHERE event_id = ?",
                (datetime.now(timezone.utc).isoformat(), event_id),
            )
        return len(rows)


def delivery_loop() -> None:
    while True:
        try:
            deliver_pending()
        except Exception as error:  # Keep queued leads for the next retry.
            print(f"delivery failed: {error}", flush=True)
        time.sleep(DELIVERY_INTERVAL_SECONDS)


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
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        expected_path = f"/zvonok/{WEBHOOK_SECRET}"
        if self.path != expected_path:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        try:
            if "application/json" in content_type:
                payload = json.loads(raw_body.decode("utf-8"))
            else:
                query = urllib.parse.parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
                payload = {key: values[-1] for key, values in query.items()}
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            saved = save_lead(parse_lead(payload))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "new": saved}).encode("utf-8"))


def main() -> None:
    init_database()
    threading.Thread(target=delivery_loop, daemon=True).start()
    print(f"listening on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), WebhookHandler).serve_forever()


if __name__ == "__main__":
    main()
