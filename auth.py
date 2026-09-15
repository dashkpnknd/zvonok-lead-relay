#!/usr/bin/env python3
"""Authorize the dedicated Telegram sender account without putting its code in git."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


SESSION_PATH = os.environ.get("TELEGRAM_SESSION_PATH", "/data/outreach.session")
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
PHONE = os.environ["TELEGRAM_USER_PHONE"]
CODE_HASH_PATH = Path(f"{SESSION_PATH}.phone_code_hash")


def save_code_hash(value: str) -> None:
    CODE_HASH_PATH.write_text(value, encoding="utf-8")
    CODE_HASH_PATH.chmod(0o600)


def read_code_hash() -> str:
    if not CODE_HASH_PATH.exists():
        raise RuntimeError("request a new Telegram code before signing in")
    return CODE_HASH_PATH.read_text(encoding="utf-8").strip()


async def request_code() -> None:
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("already-authorized")
        else:
            sent_code = await client.send_code_request(PHONE)
            save_code_hash(sent_code.phone_code_hash)
            print("code-requested")
    finally:
        await client.disconnect()


async def sign_in(code: str) -> None:
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    try:
        try:
            await client.sign_in(PHONE, code, phone_code_hash=read_code_hash())
        except SessionPasswordNeededError:
            password = os.environ.get("TELEGRAM_2FA_PASSWORD")
            if not password:
                raise RuntimeError("two-factor password is required")
            await client.sign_in(password=password)
        authorized = await client.is_user_authorized()
        if authorized:
            CODE_HASH_PATH.unlink(missing_ok=True)
        print("authorized" if authorized else "not-authorized")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "request-code":
        asyncio.run(request_code())
    elif len(sys.argv) == 3 and sys.argv[1] == "sign-in":
        asyncio.run(sign_in(sys.argv[2]))
    else:
        raise SystemExit("usage: auth.py request-code | auth.py sign-in CODE")
