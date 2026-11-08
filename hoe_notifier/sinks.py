"""Канали доставки: Telegram і Slack."""
from __future__ import annotations

import asyncio
import logging
import re
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


class ChannelError(Exception):
    """Канал не вдалося знайти або перевірити; текст призначений для користувача."""


SLACK_ID_RE = re.compile(r"[CGD][A-Z0-9]{8,}")

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


SLACK_GONE_ERRORS = {"channel_not_found", "not_in_channel", "is_archived",
                     "invalid_auth", "token_revoked", "token_expired", "account_inactive"}


class SlackSink:
    """Надсилає у Slack від імені ботів, яких користувачі підключили самі.

    Адресат має вигляд «<id бота в базі>/<ID каналу>». Адресат без «/» (запис з addresses.toml)
    використовує глобальний `SLACK_TOKEN`.
    """

    def __init__(self, token: str | None = None, db=None) -> None:
        self.token = token
        self.db = db
        self._clients: dict = {}

    def _client(self, token: str):
        if token not in self._clients:
            from slack_sdk import WebClient
            self._clients[token] = WebClient(token=token)
        return self._clients[token]

    def _client_and_channel(self, target: str):
        if "/" in target:
            bot_id, channel = target.split("/", 1)
            bot = self.db.get_slack_bot(int(bot_id)) if self.db and bot_id.isdigit() else None
            if bot is None:
                raise TargetGone("Slack-бота видалено")
            return self._client(bot.token), channel
        if not self.token:
            raise TargetGone("не задано SLACK_TOKEN")
        return self._client(self.token), target

    async def _run(self, func, **kwargs):
        from slack_sdk.errors import SlackApiError
        try:
            return await asyncio.to_thread(func, **kwargs)
        except SlackApiError as exc:
            if exc.response.get("error") in SLACK_GONE_ERRORS:
                raise TargetGone(str(exc)) from exc
            raise

    async def inspect(self, token: str) -> tuple[str, str]:
        """Перевіряє токен бота: (назва воркспейсу, ім'я бота). ChannelError — токен непридатний."""
        from slack_sdk.errors import SlackApiError
        try:
            info = await asyncio.to_thread(self._client(token).auth_test)
        except SlackApiError as exc:
            raise ChannelError("Slack не прийняв цей токен (перевір, що це «Bot User OAuth Token», "
                               "xoxb-…, і що застосунок встановлено у воркспейс).") from exc
        return info.get("team") or "Slack", info.get("user") or "bot"

    async def resolve(self, bot_id: int, ref: str) -> tuple[str, str]:
        """Перетворює ID (C0123…) або #назву на (ID каналу, назва для показу) від імені бота `bot_id`."""
        from slack_sdk.errors import SlackApiError
        bot = self.db.get_slack_bot(bot_id)
        if bot is None:
            raise ChannelError("Slack-бота не знайдено.")
        client = self._client(bot.token)
        ref = ref.strip()
        try:
            if SLACK_ID_RE.fullmatch(ref):
                try:
                    info = await asyncio.to_thread(client.conversations_info, channel=ref)
                except SlackApiError as exc:
                    if exc.response.get("error") == "missing_scope":
                        return ref, ref          # назву дізнатися не можемо, але ID достатньо
                    raise
                ch = info["channel"]
                return ch["id"], f"#{ch['name']}" if ch.get("name") else ref
            name = ref.lstrip("#").lower()
            for types in ("public_channel,private_channel", "public_channel"):
                try:
                    cursor = None
                    while True:
                        page = await asyncio.to_thread(
                            client.conversations_list, types=types, exclude_archived=True,
                            limit=200, cursor=cursor)
                        for ch in page["channels"]:
                            if ch.get("name") == name:
                                return ch["id"], f"#{ch['name']}"
                        cursor = (page.get("response_metadata") or {}).get("next_cursor")
                        if not cursor:
                            break
                    raise ChannelError(f"Канал #{name} не знайдено (приватні канали бот бачить, лише якщо його "
                                       "запрошено). Вкажи ID каналу, наприклад C0123ABCD.")
                except SlackApiError as exc:
                    if exc.response.get("error") != "missing_scope" or types == "public_channel":
                        if exc.response.get("error") == "missing_scope":
                            raise ChannelError("Для пошуку за назвою потрібні права channels:read "
                                               "(або вкажи ID каналу).") from exc
                        raise
        except SlackApiError as exc:
            err = exc.response.get("error")
            if err in {"channel_not_found", "invalid_arguments"}:
                raise ChannelError("Не знайшов такий канал у Slack. Перевір ID та що бота запрошено в канал.") from exc
            raise ChannelError(f"Slack відповів помилкою: {err}.") from exc

    async def send_text(self, target, subject, label, body, *, silent, pre=True):
        body = fit_body(body, SLACK_BODY_LIMIT)
        head = "\n".join(x for x in (f"*{subject}*" if subject else "", f"📍 {label}" if label else "") if x)
        shown = (f"```{body}```" if pre else body) if body else ""
        text = head + ("\n" if head and shown else "") + shown
        client, channel = self._client_and_channel(target)
        await self._run(
            client.chat_postMessage, channel=channel,
            text=(f"{subject} {label}".strip() or body[:100]),
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )

    async def send_photo(self, target, data, *, silent):
        client, channel = self._client_and_channel(target)
        await self._run(client.files_upload_v2, channel=channel, content=data, filename="graph.png")