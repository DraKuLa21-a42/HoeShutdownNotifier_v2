"""Канали доставки: Telegram і Slack."""
from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import Protocol

log = logging.getLogger(__name__)


class TargetGone(Exception):
    """Адресат більше недоступний (бота заблоковано/вилучено з чату, канал не існує)."""


class TargetMigrated(Exception):
    """Чат змінив ідентифікатор (група → супергрупа)."""

    def __init__(self, new_target: str) -> None:
        super().__init__(new_target)
        self.new_target = new_target


TG_BODY_LIMIT = 3500      # ліміт Telegram на повідомлення — 4096 символів разом із розміткою
SLACK_BODY_LIMIT = 2800   # ліміт тексту блоку Slack — 3000


def fit_body(body: str, limit: int, measure=len) -> str:
    """Обрізає надто довгий текст, додаючи «…»; `measure` дає довжину після екранування."""
    if measure(body) <= limit:
        return body
    while body and measure(body) + 2 > limit:
        body = body[: int(len(body) * 0.9)]
    return body.rstrip() + "\n…"


def render_message(subject: str, label: str, body: str, *, pre: bool = True) -> str:
    """HTML для Telegram: <b>заголовок</b>, 📍адреса, текст (у блоці <pre> або звичайним абзацом)."""
    parts = []
    if subject:
        parts.append(f"<b>{escape(subject)}</b>")
    if label:
        parts.append(f"📍 {escape(label)}")
    text = "\n".join(parts)
    body = fit_body(body, TG_BODY_LIMIT, measure=lambda b: len(escape(b)))
    if body:
        shown = f"<pre>{escape(body)}</pre>" if pre else escape(body)
        text += ("\n\n" if text else "") + shown
    return text


class Sink(Protocol):
    async def send_text(self, target: str, subject: str, label: str, body: str, *, silent: bool,
                        pre: bool = True) -> None: ...
    async def send_photo(self, target: str, data: bytes, *, silent: bool) -> None: ...


class TelegramSink:
    def __init__(self, bot) -> None:
        self.bot = bot

    async def _call(self, func, *args, **kwargs):
        from aiogram.exceptions import (
            TelegramBadRequest, TelegramForbiddenError, TelegramMigrateToChat, TelegramRetryAfter,
        )
        for attempt in range(2):
            try:
                return await func(*args, **kwargs)
            except TelegramRetryAfter as exc:
                if attempt == 1:
                    raise
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramMigrateToChat as exc:
                raise TargetMigrated(str(exc.migrate_to_chat_id)) from exc
            except TelegramForbiddenError as exc:
                raise TargetGone(str(exc)) from exc
            except TelegramBadRequest as exc:
                if "chat not found" in str(exc).lower():
                    raise TargetGone(str(exc)) from exc
                raise

    async def send_text(self, target, subject, label, body, *, silent, pre=True):
        await self._call(
            self.bot.send_message, chat_id=int(target),
            text=render_message(subject, label, body, pre=pre),
            parse_mode="HTML", disable_notification=silent,
        )

    async def send_photo(self, target, data, *, silent):
        from aiogram.types import BufferedInputFile
        await self._call(
            self.bot.send_photo, chat_id=int(target),
            photo=BufferedInputFile(data, filename="graph.png"), disable_notification=silent,
        )


class SlackSink:
    def __init__(self, token: str) -> None:
        from slack_sdk import WebClient
        self.client = WebClient(token=token)

    async def _run(self, func, **kwargs):
        from slack_sdk.errors import SlackApiError
        try:
            return await asyncio.to_thread(func, **kwargs)
        except SlackApiError as exc:
            if exc.response.get("error") in {"channel_not_found", "not_in_channel", "is_archived"}:
                raise TargetGone(str(exc)) from exc
            raise

    async def send_text(self, target, subject, label, body, *, silent, pre=True):
        body = fit_body(body, SLACK_BODY_LIMIT)
        head = "\n".join(x for x in (f"*{subject}*" if subject else "", f"📍 {label}" if label else "") if x)
        shown = (f"```{body}```" if pre else body) if body else ""
        text = head + ("\n" if head and shown else "") + shown
        await self._run(
            self.client.chat_postMessage, channel=target,
            text=(f"{subject} {label}".strip() or body[:100]),
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )

    async def send_photo(self, target, data, *, silent):
        await self._run(self.client.files_upload_v2, channel=target, content=data, filename="graph.png")