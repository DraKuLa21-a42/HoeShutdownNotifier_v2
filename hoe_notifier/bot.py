"""Telegram-бот для керування адресами."""
from __future__ import annotations

import json
from dataclasses import dataclass
import logging
import re
import time
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand, BufferedInputFile, CallbackQuery, ForceReply, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, ChatMemberUpdated,
)

from .config import Settings
from .db import Database, Subscription
from .hoe import HoeClient, SearchUnavailable, match_house, suggest_houses
from .monitor import MANUAL_REFRESH_MAX_AGE, TEXT_GRAPH_SUBJECT, Monitor, describe_state, label_for
from .queues import QUEUES, filter_schedule, normalize_queue
from .sinks import ChannelError, TargetGone, fit_body, render_message

log = logging.getLogger(__name__)

HELP = (
    "<b>Сповіщення про погодинні відключення (Хмельницькобленерго)</b>\n\n"
    "/add — додати адресу: населений пункт → вулиця → будинок\n"
    "/add_id <code>&lt;streetId&gt; &lt;будинок&gt; [назва]</code> — додати адресу за ID вулиці\n"
    "/slack — підключити власного Slack-бота / керувати ними\n"
    "/list — мої адреси та налаштування (сповіщення, графік, звук, черга)\n"
    "/check — перевірити всі мої адреси зараз\n"
    "/graph — показати актуальний графік (картинку та/або текст); якщо вказано чергу — лише для неї, "
    "<code>/graph all</code> — повністю\n"
    "/admin — панель адміністратора (лише для ADMIN_IDS)\n"
    "/subs — усі підписки всіх чатів (лише для ADMIN_IDS)\n"
    "/cancel — скасувати поточну дію\n\n"
    "<b>Фільтр за чергою.</b> У /list натисни «🔢 Черга» біля адреси й обери свою підчергу (1.1 – 6.2): "
    "бот надсилатиме лише відключення твоєї черги (і текстовий графік лише з нею). "
    "Одразу під час додавання: <code>/add_id 123456 12 Дім --queue=1.1</code>.\n\n"
    "<b>Сповіщення в канал або групу.</b> Додай бота в канал адміністратором з правом "
    "«Публікація повідомлень» і дописуй до команди <code>--to=&lt;ID каналу або @username&gt;</code>:\n"
    "<code>/add_id 123456 12 Дім --to=-1001234567890</code>\n"
    "<code>/add --to=@my_channel</code>\n"
    "Команди надсилай мені в приватні повідомлення; керувати такими адресами можна з /list.\n"
    "У груповому чаті керувати адресами можуть лише адміністратори групи.\n\n"
    "<b>Slack.</b> Підключи свого Slack-бота командою /slack (токен, інструкція всередині), запроси його в канал "
    "(<code>/invite @назва_бота</code>) і дописуй <code>--slack=&lt;ID каналу або #назва&gt;</code>:\n"
    "<code>/add_id 123456 12 Дім --slack=C0123ABCD</code>\n"
    "<code>/add --slack=#svitlo</code>"
)

# На мобільних "--" часто автозамінюється на тире, тому приймаємо і його.
TO_RE = re.compile(r"(?:^|\s)(?:--|—|–)to(?:=|\s+)(\S+)", re.IGNORECASE)
SLACK_RE = re.compile(r"(?:^|\s)(?:--|—|–)slack(?:=|\s+)(\S+)", re.IGNORECASE)
QUEUE_RE = re.compile(r"(?:^|\s)(?:--|—|–)queue(?:=|\s+)(\S+)", re.IGNORECASE)
USERNAME_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,31}")
QUEUES_HINT = f"{QUEUES[0]} … {QUEUES[-1]}"


class AddFlow(StatesGroup):
    settlement_query = State()
    settlement_pick = State()
    street_query = State()
    street_pick = State()
    house = State()


@dataclass(frozen=True)
class Dest:
    """Куди надсилати сповіщення, якщо не в поточний чат."""
    channel: str          # "tg" | "slack"
    target: str
    title: str

    def as_list(self) -> list[str]:        # для збереження в FSM
        return [self.channel, self.target, self.title]


class SlackFlow(StatesGroup):
    token = State()


MAX_SLACK_BOTS = 3
SLACK_TOKEN_RE = re.compile(r"xoxb-[A-Za-z0-9-]{10,}")
SLACK_GUIDE = (
    "<b>Як підключити Slack</b>\n"
    "1. Відкрий api.slack.com/apps → <b>Create New App</b> → From scratch, обери воркспейс.\n"
    "2. <b>OAuth &amp; Permissions</b> → Bot Token Scopes: <code>chat:write</code>, <code>files:write</code>; "
    "щоб шукати канали за #назвою — ще <code>channels:read</code>, <code>groups:read</code>.\n"
    "3. <b>Install to Workspace</b> і скопіюй <b>Bot User OAuth Token</b> (починається з <code>xoxb-</code>).\n"
    "4. Надішли токен сюди одним повідомленням — я одразу видалю його з чату й збережу.\n\n"
    "Потім запроси бота в канал (<code>/invite @назва_бота</code>) і додавай адреси з <code>--slack=…</code>.\n"
    "Скасувати — /cancel."
)


class TargetError(Exception):
    """Користувачу показується текст помилки як є."""


def split_to(args: str | None) -> tuple[str, str | None]:
    """Відокремлює `--to=...` від решти аргументів команди."""
    args = args or ""
    m = TO_RE.search(args)
    if not m:
        return args.strip(), None
    return (args[: m.start()] + args[m.end():]).strip(), m.group(1)


def split_slack(args: str | None) -> tuple[str, str | None]:
    """Відокремлює `--slack=...` від решти аргументів команди."""
    args = args or ""
    m = SLACK_RE.search(args)
    if not m:
        return args.strip(), None
    return (args[: m.start()] + args[m.end():]).strip(), m.group(1)


def split_queue(args: str | None) -> tuple[str, str | None]:
    """Відокремлює `--queue=...` від решти аргументів команди."""
    args = args or ""
    m = QUEUE_RE.search(args)
    if not m:
        return args.strip(), None
    return (args[: m.start()] + args[m.end():]).strip(), m.group(1)


def sub_keyboard(sub: Subscription) -> InlineKeyboardMarkup | None:
    if sub.source == "config":
        return None
    on = lambda v: "✅" if v else "⬜"  # noqa: E731
    row1 = [
        InlineKeyboardButton(text=f"{on(sub.events)} Відключення", callback_data=f"sub:{sub.id}:events"),
        InlineKeyboardButton(text=f"{on(sub.graphs)} Графік", callback_data=f"sub:{sub.id}:graphs"),
    ]
    row2 = [
        InlineKeyboardButton(text=f"{'🔕' if sub.silent else '🔔'} Звук", callback_data=f"sub:{sub.id}:silent"),
        InlineKeyboardButton(text="🗑 Видалити", callback_data=f"sub:{sub.id}:del"),
    ]
    row3 = [InlineKeyboardButton(text=f"🔢 Черга: {sub.queue or 'усі'}", callback_data=f"sub:{sub.id}:queue")]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3])


def queue_keyboard(sub: Subscription) -> InlineKeyboardMarkup:
    """Вибір підчерги: по дві в рядку (1.1 1.2 / 2.1 2.2 …), «усі» та «назад»."""
    mark = lambda q: "✅ " if sub.queue == q else ""  # noqa: E731
    rows = [
        [InlineKeyboardButton(text=f"{mark(q)}{q}", callback_data=f"q:{sub.id}:{q}") for q in QUEUES[i:i + 2]]
        for i in range(0, len(QUEUES), 2)
    ]
    rows.append([InlineKeyboardButton(text=f"{'✅ ' if sub.queue is None else ''}Усі черги (без фільтра)",
                                      callback_data=f"q:{sub.id}:all")])
    rows.append([InlineKeyboardButton(text="↩ Назад", callback_data=f"q:{sub.id}:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sub_text(sub: Subscription, where: str | None = None) -> str:
    lock = " 🔒 (з addresses.toml)" if sub.source == "config" else ""
    dest = f"\n➡️ Куди: <b>{escape(where)}</b>" if where else ""
    return (f"📍 <b>{escape(sub.label)}</b>{lock}{dest}\n"
            f"streetId: <code>{escape(sub.street_id)}</code>, будинок: <code>{escape(sub.house)}</code>")


def prompt_markup(chat_type: str) -> ForceReply | None:
    # У групах (privacy mode) бот бачить лише команди та відповіді на свої повідомлення.
    return None if chat_type == ChatType.PRIVATE else ForceReply(selective=True)


def build_router(settings: Settings, db: Database, hoe: HoeClient, monitor: Monitor) -> Router:
    router = Router()

    def allowed(chat_id: int, user_id: int) -> bool:
        if not settings.allowed_chat_ids or user_id in settings.admin_ids:
            return True
        return chat_id in settings.allowed_chat_ids or user_id in settings.allowed_chat_ids

    async def can_manage(bot: Bot, chat_id: int, chat_type: str, user_id: int) -> bool:
        if chat_type == ChatType.PRIVATE or user_id in settings.admin_ids:
            return True
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except TelegramAPIError:
            return False
        return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}

    async def guard(message: Message) -> bool:
        """True, якщо користувачу дозволено змінювати підписки в цьому чаті."""
        user_id = message.from_user.id if message.from_user else 0
        if not allowed(message.chat.id, user_id):
            await message.answer("Цей бот приватний.")
            return False
        if not await can_manage(message.bot, message.chat.id, message.chat.type, user_id):
            await message.answer("Керувати адресами в групі можуть лише адміністратори.")
            return False
        return True

    async def resolve_target(bot: Bot, raw: str, user_id: int) -> tuple[int, str]:
        """Перевіряє `--to`: чат існує, бот може туди писати, користувач — адмін чату."""
        raw = raw.strip()
        try:
            ref: int | str = int(raw)
        except ValueError:
            if not USERNAME_RE.fullmatch(raw):
                raise TargetError("Вкажи числовий ID чату (на кшталт <code>-1001234567890</code>) "
                                  "або @username каналу.") from None
            ref = raw
        try:
            chat = await bot.get_chat(ref)
        except TelegramAPIError:
            raise TargetError("Не знайшов такий чат. Перевір ID/@username і що бот доданий у канал.") from None
        if chat.type == ChatType.PRIVATE:
            raise TargetError("Це особистий чат. Для себе просто використовуй команду без <code>--to</code>.")

        try:
            me = await bot.me()
            bot_member = await bot.get_chat_member(chat.id, me.id)
        except TelegramAPIError:
            raise TargetError("Бот не є учасником цього чату. Додай його і спробуй ще раз.") from None
        if bot_member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
            raise TargetError("Бот не є учасником цього чату. Додай його і спробуй ще раз.")
        if chat.type == ChatType.CHANNEL:
            can_post = bot_member.status == ChatMemberStatus.CREATOR or (
                bot_member.status == ChatMemberStatus.ADMINISTRATOR
                and getattr(bot_member, "can_post_messages", False))
            if not can_post:
                raise TargetError("Зроби бота адміністратором каналу з правом «Публікація повідомлень».")

        if user_id not in settings.admin_ids:
            try:
                member = await bot.get_chat_member(chat.id, user_id)
            except TelegramAPIError:
                raise TargetError("Не вдалося перевірити твої права в цьому чаті.") from None
            if member.status not in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
                raise TargetError("Додавати адреси в цей чат можуть лише його адміністратори.")
        return chat.id, chat.title or chat.username or str(chat.id)

    async def finish_add(message: Message, user_id: int, street_id: str, house: str, label: str,
                         dest: Dest | None = None, queue: str | None = None) -> None:
        slack = dest is not None and dest.channel == "slack"
        external = slack or (dest is not None and dest.target != str(message.chat.id))
        channel = "slack" if slack else "tg"
        chat_id = dest.target if dest else str(message.chat.id)
        title = dest.title if dest else chat_id
        if db.count_for_target(channel, chat_id) >= settings.max_addresses_per_chat:
            where = f"«{escape(title)}»" if external else "чат"
            await message.answer(f"Ліміт: {settings.max_addresses_per_chat} адрес на {where}. Видали зайві в /list.")
            return
        if db.find_subscription(channel, chat_id, street_id, house):
            await message.answer("Ця адреса вже є в списку" + (" цього каналу." if external else "."))
            return

        # Спочатку оновлюємо стан (розсилка існуючим підписникам), потім підписуємо — без дублів.
        await monitor.refresh_address(street_id, house, max_age=MANUAL_REFRESH_MAX_AGE)
        state = db.get_state(street_id, house)
        status = describe_state(state, queue) if state else (
            "Не вдалося отримати дані з сайту зараз — повідомлю, щойно вони з'являться.")
        heading = f"{label}{' · черга ' + queue if queue else ''}"

        if slack:
            # Перше повідомлення водночас перевіряє, що бот справді може писати в канал.
            try:
                await monitor.sinks["slack"].send_text(chat_id, "", heading, status, silent=True)
            except TargetGone:
                await message.answer("Бот не може писати в цей канал: запроси його командою "
                                     "<code>/invite @назва_бота</code> і спробуй ще раз. Адресу не додано.")
                return
            except Exception as exc:
                log.warning("Не вдалося написати в Slack %s: %s", chat_id, exc)
                await message.answer("Не вдалося надіслати повідомлення в Slack (перевір права бота). "
                                     "Адресу не додано.")
                return
        elif external:
            # Перше повідомлення в канал водночас перевіряє, що публікація справді працює.
            try:
                await message.bot.send_message(
                    int(chat_id), f"📍 <b>{escape(heading)}</b>\n\n<pre>{escape(status)}</pre>",
                    disable_notification=settings.default_silent)
            except TelegramAPIError as exc:
                log.warning("Не вдалося написати в %s: %s", chat_id, exc)
                await message.answer("Не вдалося надіслати повідомлення в цей чат (перевір права бота). "
                                     "Адресу не додано.")
                return

        sub, _ = db.add_subscription(
            channel, chat_id, street_id, house, label,
            graphs=settings.default_graphs, silent=settings.default_silent, owner_id=user_id, queue=queue,
        )
        hint = ("Налаштування — кнопками нижче або в /list." if queue else
                "Щоб отримувати лише відключення своєї черги, натисни «🔢 Черга».")
        if external:
            kind = "Slack" if slack else "«" + escape(title) + "»"
            where = f"у Slack ({escape(title)})" if slack else f"в {kind}"
            await message.answer(f"✅ Додано {where}: <b>{escape(label)}</b>\n{hint}",
                                 reply_markup=sub_keyboard(sub))
        else:
            await message.answer(
                f"✅ Додано: <b>{escape(label)}</b>\n\nПоточний стан:\n<pre>{escape(status)}</pre>\n{hint}",
                reply_markup=sub_keyboard(sub))

    async def parse_queue_option(message: Message, raw: str | None) -> tuple[str | None, bool]:
        """(черга, ok). ok=False — помилку вже показано користувачу."""
        if raw is None:
            return None, True
        queue = normalize_queue(raw)
        if queue is None:
            await message.answer(f"Невідома черга «{escape(raw)}». Доступні підчерги: {QUEUES_HINT} (наприклад, 1.1).")
            return None, False
        return queue, True

    async def resolve_slack(raw: str, user_id: int) -> Dest:
        """Перевіряє `--slack=[№бота:]канал`: у користувача є свій Slack-бот і канал для нього існує."""
        sink = monitor.sinks.get("slack")
        bots = db.list_slack_bots(user_id)
        if sink is None or not bots:
            raise TargetError("Спершу підключи свого Slack-бота: /slack")
        m = re.fullmatch(r"(\d+):(.+)", raw)
        if m:
            bot = next((b for b in bots if b.id == int(m.group(1))), None)
            if bot is None:
                raise TargetError("Немає твого Slack-бота з таким номером. Список — у /slack.")
            ref = m.group(2)
        elif len(bots) == 1:
            bot, ref = bots[0], raw
        else:
            lst = "\n".join(f"№{b.id} — {escape(b.team)} ({escape(b.bot_name)})" for b in bots)
            raise TargetError(f"У тебе кілька Slack-ботів, вкажи номер: <code>--slack=&lt;№&gt;:&lt;канал&gt;</code>\n{lst}")
        try:
            channel_id, title = await sink.resolve(bot.id, ref)
        except ChannelError as exc:
            raise TargetError(escape(str(exc))) from None
        except Exception:
            log.exception("Slack: не вдалося перевірити канал %r", ref)
            raise TargetError("Не вдалося звернутися до Slack. Спробуй пізніше.") from None
        return Dest("slack", f"{bot.id}/{channel_id}", f"{bot.team} {title}")

    async def maybe_resolve(message: Message, raw_to: str | None,
                            raw_slack: str | None = None) -> tuple[Dest | None, bool]:
        """(призначення, ok). ok=False — помилку вже показано користувачу."""
        if raw_to is not None and raw_slack is not None:
            await message.answer("Вкажи або <code>--to</code> (Telegram), або <code>--slack</code>, не обидва.")
            return None, False
        if raw_to is None and raw_slack is None:
            return None, True
        try:
            if raw_slack is not None:
                return await resolve_slack(raw_slack, message.from_user.id), True
            target, title = await resolve_target(message.bot, raw_to, message.from_user.id)
        except TargetError as exc:
            await message.answer(str(exc))
            return None, False
        return Dest("tg", str(target), title), True

    # --- бота вилучили з чату / користувач його заблокував ----------------
    @router.my_chat_member()
    async def on_my_chat_member(event: ChatMemberUpdated) -> None:
        if event.new_chat_member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
            removed = db.delete_target("tg", str(event.chat.id))
            if removed:
                log.info("Бота вилучено з чату %s — видалено підписок: %d", event.chat.id, removed)

    # --- базові команди ---------------------------------------------------
    @router.message(Command("start", "help"))
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(HELP)

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Скасовано.")

    # --- додавання адреси: населений пункт → вулиця → будинок --------------
    def last_settlement(user_id: int) -> tuple[str, str] | None:
        raw = db.kv_get(f"last_settlement:{user_id}")
        try:
            sid, name = json.loads(raw) if raw else (None, None)
        except ValueError:
            return None
        return (sid, name) if sid and name else None

    def options_keyboard(prefix: str, options) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=o.name[:60], callback_data=f"{prefix}:{i}")]
            for i, o in enumerate(options)
        ])

    async def ask_street(message: Message, state: FSMContext, settlement_name: str) -> None:
        await state.set_state(AddFlow.street_query)
        await message.answer(f"Населений пункт: <b>{escape(settlement_name)}</b>\nТепер введи назву вулиці "
                             "(можна частину):", reply_markup=prompt_markup(message.chat.type))

    @router.message(Command("add"))
    async def add(message: Message, command: CommandObject, state: FSMContext) -> None:
        if not await guard(message):
            return
        rest, raw_to = split_to(command.args)
        rest, raw_slack = split_slack(rest)
        _, raw_queue = split_queue(rest)
        queue, ok = await parse_queue_option(message, raw_queue)
        if not ok:
            return
        dest, ok = await maybe_resolve(message, raw_to, raw_slack)
        if not ok:
            return
        await state.clear()
        await state.set_state(AddFlow.settlement_query)
        await state.update_data(dest=dest.as_list() if dest else None, queue=queue)
        where = (f" (у Slack: {escape(dest.title)})" if dest.channel == "slack" else
                 f" (у «{escape(dest.title)}»)") if dest else ""
        markup = prompt_markup(message.chat.type)
        last = last_settlement(message.from_user.id)
        if markup is None and last:  # у приватному чаті пропонуємо швидкий вибір останнього міста
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"📍 {last[1][:55]}", callback_data="sp:last")]])
        await message.answer(f"Введи назву населеного пункту{where} (можна частину). /cancel — скасувати.",
                             reply_markup=markup)

    async def search_or_fail(message: Message, state: FSMContext, coro):
        try:
            return await coro
        except SearchUnavailable as exc:
            log.warning("Пошук на сайті не вдався: %s", exc)
            await state.clear()
            await message.answer("Пошук на сайті зараз недоступний. Спробуй пізніше або скористайся /add_id.")
            return None

    @router.message(StateFilter(AddFlow.settlement_query), F.text, ~F.text.startswith("/"))
    async def settlement_query(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        if len(query) < 2:
            await message.answer("Введи хоча б 2 символи.", reply_markup=prompt_markup(message.chat.type))
            return
        found = await search_or_fail(message, state, hoe.search_settlements(query))
        if found is None:
            return
        if not found:
            await message.answer("Населений пункт не знайдено. Спробуй іншу назву.",
                                 reply_markup=prompt_markup(message.chat.type))
            return
        found = found[:8]
        await state.update_data(settlements=[[o.id, o.name] for o in found])
        await state.set_state(AddFlow.settlement_pick)
        await message.answer("Обери населений пункт:", reply_markup=options_keyboard("sp", found))

    @router.callback_query(StateFilter(AddFlow.settlement_query, AddFlow.settlement_pick),
                           F.data.regexp(r"^sp:(\d+|last)$"))
    async def settlement_pick(cb: CallbackQuery, state: FSMContext) -> None:
        key = cb.data.split(":")[1]
        if key == "last":
            chosen = last_settlement(cb.from_user.id)
        else:
            items = (await state.get_data()).get("settlements", [])
            chosen = tuple(items[int(key)]) if int(key) < len(items) else None
        if not chosen:
            await cb.answer("Список застарів, почни з /add", show_alert=True)
            return
        sid, name = chosen
        db.kv_set(f"last_settlement:{cb.from_user.id}", json.dumps([sid, name], ensure_ascii=False))
        await state.update_data(settlement_id=sid, settlement_name=name)
        await cb.message.edit_text(f"Населений пункт: <b>{escape(name)}</b>")
        await cb.answer()
        await ask_street(cb.message, state, name)

    @router.message(StateFilter(AddFlow.street_query), F.text, ~F.text.startswith("/"))
    async def street_query(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        if len(query) < 2:
            await message.answer("Введи хоча б 2 символи.", reply_markup=prompt_markup(message.chat.type))
            return
        data = await state.get_data()
        found = await search_or_fail(message, state, hoe.search_streets(data["settlement_id"], query))
        if found is None:
            return
        if not found:
            await message.answer("Вулицю не знайдено. Спробуй іншу назву.",
                                 reply_markup=prompt_markup(message.chat.type))
            return
        found = found[:8]
        await state.update_data(streets=[[o.id, o.name] for o in found])
        await state.set_state(AddFlow.street_pick)
        await message.answer("Обери вулицю:", reply_markup=options_keyboard("st", found))

    @router.callback_query(StateFilter(AddFlow.street_pick), F.data.regexp(r"^st:\d+$"))
    async def street_pick(cb: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        idx = int(cb.data.split(":")[1])
        streets = data.get("streets", [])
        if not (0 <= idx < len(streets)):
            await cb.answer("Список застарів, почни з /add", show_alert=True)
            return
        street_id, name = streets[idx]
        await cb.message.edit_text(f"Вулиця: <b>{escape(name)}</b>")
        await cb.answer()
        try:
            houses = await hoe.get_houses(street_id)
        except SearchUnavailable as exc:
            log.warning("Не вдалось отримати список будинків: %s", exc)
            houses = []
        await state.update_data(street_id=street_id, street_name=name, houses=houses)
        await state.set_state(AddFlow.house)
        if not houses:
            hint = "Список будинків із сайту отримати не вдалось — перевірити номер не зможу."
        elif len(houses) <= 30:
            hint = "Є такі будинки: " + ", ".join(houses)
        else:
            hint = f"На сайті {len(houses)} будинків на цій вулиці."
        await cb.message.answer(f"Введи номер будинку (наприклад 12 або 12А).\n{escape(hint)}",
                                reply_markup=prompt_markup(cb.message.chat.type))

    async def complete_add(message: Message, state: FSMContext, user_id: int, house_no: str) -> None:
        data = await state.get_data()
        await state.clear()
        label = f"{data['settlement_name']}, {data['street_name']}, {house_no}"[:80]
        raw_dest = data.get("dest")
        await finish_add(message, user_id, data["street_id"], house_no, label,
                         Dest(*raw_dest) if raw_dest else None, data.get("queue"))

    @router.message(StateFilter(AddFlow.house), F.text, ~F.text.startswith("/"))
    async def house(message: Message, state: FSMContext) -> None:
        typed = re.sub(r"\s+", "", message.text or "")   # "12 А" → "12А"
        if not typed or len(typed) > 12:
            await message.answer("Некоректний номер будинку. Приклад: 12 або 12А.",
                                 reply_markup=prompt_markup(message.chat.type))
            return
        houses = (await state.get_data()).get("houses") or []
        if houses:
            canonical = match_house(houses, typed)
            if canonical is None:
                suggestions = suggest_houses(houses, typed)
                await state.update_data(house_suggest=suggestions, house_typed=typed)
                rows = [[InlineKeyboardButton(text=h, callback_data=f"hs:{i}")] for i, h in enumerate(suggestions)]
                rows.append([InlineKeyboardButton(text=f"Все одно додати «{typed}»", callback_data="hs:force")])
                lead = "Може, один із цих?" if suggestions else "Схожих не знайшов."
                await message.answer(f"Будинку «{escape(typed)}» немає у списку сайту. {lead}",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                return
            typed = canonical  # зберігаємо написання, як на сайті
        await complete_add(message, state, message.from_user.id, typed)

    @router.callback_query(StateFilter(AddFlow.house), F.data.regexp(r"^hs:(\d+|force)$"))
    async def house_pick(cb: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        key = cb.data.split(":")[1]
        if key == "force":
            chosen = data.get("house_typed")
        else:
            suggestions = data.get("house_suggest", [])
            chosen = suggestions[int(key)] if int(key) < len(suggestions) else None
        if not chosen:
            await cb.answer("Список застарів, введи номер ще раз.", show_alert=True)
            return
        await cb.message.edit_text(f"Будинок: <b>{escape(chosen)}</b>")
        await cb.answer()
        await complete_add(cb.message, state, cb.from_user.id, chosen)

    @router.message(Command("add_id"))
    async def add_id(message: Message, command: CommandObject, state: FSMContext) -> None:
        if not await guard(message):
            return
        rest, raw_to = split_to(command.args)
        rest, raw_slack = split_slack(rest)
        rest, raw_queue = split_queue(rest)
        parts = rest.split(maxsplit=2)
        if len(parts) < 2:
            await message.answer("Формат: <code>/add_id &lt;streetId&gt; &lt;будинок&gt; [назва] "
                                 "[--queue=1.1] [--to=&lt;ID каналу або @username&gt; | "
                                 "--slack=&lt;ID каналу або #назва&gt;]</code>")
            return
        queue, ok = await parse_queue_option(message, raw_queue)
        if not ok:
            return
        dest, ok = await maybe_resolve(message, raw_to, raw_slack)
        if not ok:
            return
        street_id, house_no = parts[0], parts[1]
        label = parts[2].strip() if len(parts) > 2 else f"вул. #{street_id}, {house_no}"
        await state.clear()
        await finish_add(message, message.from_user.id, street_id, house_no, label[:80], dest, queue)

    # --- Slack-боти користувачів ---------------------------------------------
    async def show_slack_menu(message: Message, user_id: int) -> None:
        bots = db.list_slack_bots(user_id)
        lines = ["<b>Твої Slack-боти</b>"] if bots else ["Slack-ботів ще немає."]
        rows = []
        for b in bots:
            n = db.stats_for_slack_bot(b.id)
            lines.append(f"№{b.id} — {escape(b.team)}, бот {escape(b.bot_name)} (<code>{b.masked}</code>), "
                         f"підписок: {n}")
            rows.append([InlineKeyboardButton(text=f"🗑 №{b.id} {b.team}"[:60], callback_data=f"slk:del:{b.id}")])
        rows.append([InlineKeyboardButton(text="➕ Підключити Slack-бота", callback_data="slk:add")])
        lines.append("\nКанал вказуй у командах як <code>--slack=C0123ABCD</code> або <code>--slack=#назва</code>"
                     + (" (з кількома ботами — <code>--slack=№:канал</code>)." if len(bots) > 1 else "."))
        await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def connect_slack(message: Message, user_id: int, token: str) -> bool:
        """Перевіряє й зберігає токен. True — розмову завершено (успіх чи остаточна помилка)."""
        try:                                   # токен — секрет: прибираємо його з історії чату
            await message.delete()
        except TelegramAPIError:
            pass
        if len(db.list_slack_bots(user_id)) >= MAX_SLACK_BOTS:
            await message.answer(f"Максимум {MAX_SLACK_BOTS} Slack-ботів. Видали зайвого в /slack.")
            return True
        sink = monitor.sinks.get("slack")
        if sink is None:
            await message.answer("Slack на цьому сервері недоступний.")
            return True
        try:
            team, bot_name = await sink.inspect(token)
        except ChannelError as exc:
            await message.answer(f"{escape(str(exc))}\nНадішли токен ще раз або /cancel.")
            return False
        except Exception:
            log.exception("Slack: не вдалося перевірити токен")
            await message.answer("Не вдалося звернутися до Slack. Спробуй пізніше.")
            return True
        bot, created = db.add_slack_bot(user_id, token, team, bot_name)
        await message.answer(
            f"✅ {'Підключено' if created else 'Цей бот уже підключено'}: <b>{escape(team)}</b>, "
            f"бот {escape(bot_name)} (№{bot.id}).\n\nЗапроси його в канал: <code>/invite @{escape(bot_name)}</code>, "
            "а потім додай адресу:\n<code>/add_id 123456 12 Дім --slack=C0123ABCD</code>")
        return True

    @router.message(Command("slack"))
    async def slack_command(message: Message, command: CommandObject, state: FSMContext) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not allowed(message.chat.id, user_id):
            return
        if message.chat.type != ChatType.PRIVATE:
            await message.answer("Токен — це секрет, тож /slack працює лише в приватному чаті зі мною.")
            return
        arg = (command.args or "").strip()
        if arg:
            m = SLACK_TOKEN_RE.fullmatch(arg)
            if not m:
                await message.answer("Це не схоже на токен бота Slack (xoxb-…). Інструкція: /slack")
                return
            await connect_slack(message, user_id, arg)
            return
        await state.clear()
        await show_slack_menu(message, user_id)

    @router.callback_query(F.data.regexp(r"^slk:(add|del:\d+|delyes:\d+|no)$"))
    async def slack_action(cb: CallbackQuery, state: FSMContext) -> None:
        user_id = cb.from_user.id
        if cb.message.chat.type != ChatType.PRIVATE or not allowed(cb.message.chat.id, user_id):
            await cb.answer("Лише в приватному чаті.", show_alert=True)
            return
        parts = cb.data.split(":")
        if parts[1] == "add":
            await state.clear()
            await state.set_state(SlackFlow.token)
            await cb.message.answer(SLACK_GUIDE)
        elif parts[1] == "no":
            await cb.message.edit_text("Скасовано.")
        else:
            bot = db.get_slack_bot(int(parts[2]))
            if bot is None or bot.owner_id != user_id:
                await cb.answer("Бота не знайдено.", show_alert=True)
                return
            if parts[1] == "del":
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Так, видалити", callback_data=f"slk:delyes:{bot.id}"),
                    InlineKeyboardButton(text="✖ Ні", callback_data="slk:no")]])
                await cb.message.answer(
                    f"Видалити бота №{bot.id} ({escape(bot.team)}) і всі підписки на його канали "
                    f"({db.stats_for_slack_bot(bot.id)})?", reply_markup=kb)
            else:
                removed = db.delete_slack_bot(bot.id)
                await cb.message.edit_text(f"🗑 Видалено бота №{bot.id}. Підписок прибрано: {removed}.")
        await cb.answer()

    @router.message(StateFilter(SlackFlow.token), F.text, ~F.text.startswith("/"))
    async def slack_token_received(message: Message, state: FSMContext) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if message.chat.type != ChatType.PRIVATE:
            return
        token = message.text.strip()
        if not SLACK_TOKEN_RE.fullmatch(token):
            try:
                await message.delete()
            except TelegramAPIError:
                pass
            await message.answer("Це не схоже на токен бота (має починатися з xoxb-). Спробуй ще раз або /cancel.")
            return
        if await connect_slack(message, user_id, token):
            await state.clear()

    # --- список і керування -----------------------------------------------
    async def chat_title(bot: Bot, chat_id: str) -> str:
        try:
            chat = await bot.get_chat(int(chat_id))
            return chat.title or chat.username or chat_id
        except (TelegramAPIError, ValueError):
            return chat_id

    async def dest_title(bot: Bot, sub: Subscription) -> str:
        if sub.channel != "slack":
            return await chat_title(bot, sub.target)
        bot_id, _, channel_id = sub.target.partition("/")
        slack_bot = db.get_slack_bot(int(bot_id)) if bot_id.isdigit() else None
        team = slack_bot.team if slack_bot else "Slack"
        try:
            return f"Slack: {team} {(await monitor.sinks['slack'].resolve(slack_bot.id, channel_id))[1]}"
        except Exception:
            return f"Slack: {team} {channel_id}"

    @router.message(Command("list"))
    async def list_(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not allowed(message.chat.id, user_id):
            return
        subs = [(s, None) for s in db.list_subscriptions("tg", str(message.chat.id))]
        if message.chat.type == ChatType.PRIVATE:
            for s in db.list_by_owner(user_id, "tg", str(message.chat.id)):
                subs.append((s, await dest_title(message.bot, s)))
        if not subs:
            await message.answer("Адрес ще немає. Додай через /add.")
            return
        for sub, where in subs:
            await message.answer(sub_text(sub, where), reply_markup=sub_keyboard(sub))

    async def authorize_sub(cb: CallbackQuery, sub: Subscription | None) -> bool:
        """Чи може той, хто натиснув кнопку, змінювати цю підписку. Якщо ні — показує причину."""
        chat = cb.message.chat
        user_id = cb.from_user.id
        if sub is None or sub.channel not in {"tg", "slack"}:
            await cb.answer("Запис не знайдено.", show_alert=True)
            return False
        if sub.channel == "slack":
            # Slack-підписками керують із приватного чату: той, хто підключив бота, та адміністратори.
            permitted = chat.type == ChatType.PRIVATE and (
                sub.owner_id == user_id or user_id in settings.admin_ids)
        elif sub.target == str(chat.id):
            permitted = await can_manage(cb.bot, chat.id, chat.type, user_id)
        else:
            # Підписка в каналі/групі, якою керують із приватного чату: лише її автор,
            # і він досі має бути адміністратором цього чату.
            permitted = (chat.type == ChatType.PRIVATE and sub.owner_id == user_id
                         and await can_manage(cb.bot, int(sub.target), ChatType.CHANNEL, user_id))
        if not permitted:
            await cb.answer("Недостатньо прав або запис не знайдено.", show_alert=True)
            return False
        if sub.source == "config":
            await cb.answer("Цей запис керується через addresses.toml.", show_alert=True)
            return False
        return True

    @router.callback_query(F.data.regexp(r"^sub:\d+:(events|graphs|silent|queue|del)$"))
    async def sub_action(cb: CallbackQuery) -> None:
        _, sub_id, action = cb.data.split(":")
        sub = db.get_subscription(int(sub_id))
        if not await authorize_sub(cb, sub):
            return
        if action == "del":
            db.delete_subscription(sub.id)
            await cb.message.edit_text(f"🗑 Видалено: <b>{escape(sub.label)}</b>")
        elif action == "queue":
            await cb.message.edit_reply_markup(reply_markup=queue_keyboard(sub))
        else:
            sub = db.toggle(sub.id, action)
            await cb.message.edit_reply_markup(reply_markup=sub_keyboard(sub))
        await cb.answer()

    @router.callback_query(F.data.regexp(r"^q:\d+:(\d\.\d|all|back)$"))
    async def queue_action(cb: CallbackQuery) -> None:
        _, sub_id, value = cb.data.split(":")
        sub = db.get_subscription(int(sub_id))
        if not await authorize_sub(cb, sub):
            return
        toast = None
        if value != "back":
            queue = None if value == "all" else normalize_queue(value)
            if value != "all" and queue is None:
                await cb.answer(f"Невідома черга. Доступні: {QUEUES_HINT}.", show_alert=True)
                return
            sub = db.set_queue(sub.id, queue)
            toast = f"Черга: {queue}" if queue else "Фільтр за чергою вимкнено"
        await cb.message.edit_reply_markup(reply_markup=sub_keyboard(sub))
        await cb.answer(toast)

    # --- перегляд ---------------------------------------------------------
    @router.message(Command("check"))
    async def check(message: Message) -> None:
        if not allowed(message.chat.id, message.from_user.id if message.from_user else 0):
            return
        subs = db.list_subscriptions("tg", str(message.chat.id))
        if not subs:
            await message.answer("Адрес ще немає. Додай через /add.")
            return
        for sub in subs:
            parsed = await monitor.refresh_address(sub.street_id, sub.house, max_age=MANUAL_REFRESH_MAX_AGE)
            suffix = "" if parsed else "\n<i>(сайт недоступний, показано останні відомі дані)</i>"
            state = db.get_state(sub.street_id, sub.house)
            await message.answer(
                f"📍 <b>{escape(label_for(sub))}</b>\n"
                f"<pre>{escape(describe_state(state, sub.queue))}</pre>{suffix}")

    async def send_site_graph(message: Message, *, text: bool = True, image: bool = True,
                              queues: frozenset[str] | None = None) -> bool:
        """Показує поточний графік із сайту в цьому чаті. True — щось показано."""
        graphs = await hoe.fetch_graphs()
        if graphs is None:
            await message.answer("Не вдалося завантажити сторінку графіків.")
            return False
        shown = False
        if text and graphs.text:
            body = filter_schedule(graphs.text, queues)
            if queues and body != graphs.text:
                body += "\n\nПоказано лише вашу чергу. Повний графік: /graph all"
            await message.answer(render_message(TEXT_GRAPH_SUBJECT, "", body, pre=False))
            shown = True
        if image and graphs.image_url:
            try:
                data = await hoe.download(graphs.image_url)
                await message.answer_photo(BufferedInputFile(data, filename="graph.png"))
                shown = True
            except Exception:
                log.warning("Не вдалось завантажити картинку графіка", exc_info=True)
                await message.answer("Не вдалося завантажити картинку графіка.")
        if not shown:
            kind = "графіка" if text and image else ("текстового графіка" if text else "картинки-графіка")
            await message.answer(f"На сайті зараз немає {kind}.")
        return shown

    @router.message(Command("graph"))
    async def graph(message: Message, command: CommandObject) -> None:
        if not allowed(message.chat.id, message.from_user.id if message.from_user else 0):
            return
        everything = (command.args or "").strip().lower() in {"all", "усі", "все"}
        queues = None if everything else db.chat_queues("tg", str(message.chat.id))
        await send_site_graph(message, queues=queues)

    # --- панель адміністратора --------------------------------------------
    def stats_text() -> str:
        s = db.stats()
        last = (time.strftime("%H:%M:%S %d.%m.%Y", time.localtime(monitor.last_poll_at))
                if monitor.last_poll_at else "—")
        return (f"Чатів: {s['targets']}\nПідписок: {s['subscriptions']}\nУнікальних адрес: {s['addresses']}\n"
                f"Чатів із графіком: {len(db.graph_targets())}\n"
                f"Адрес із фільтром за чергою: {s['with_queue']}\n"
                f"Останній цикл: {last} ({monitor.last_poll_summary})\n"
                f"Інтервал: {settings.poll_interval} с")

    def admin_keyboard() -> InlineKeyboardMarkup:
        def btn(text: str, data: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(text=text, callback_data=data)
        return InlineKeyboardMarkup(inline_keyboard=[
            [btn("📊 Статистика", "adm:stats"), btn("🔄 Опитати зараз", "adm:poll")],
            [btn("👥 Усі підписки", "adm:subs:0")],
            [btn("📋 Показати текстовий графік", "adm:show_text")],
            [btn("🖼 Показати картинку-графік", "adm:show_img")],
            [btn("📤 Розіслати текстовий графік", "adm:send_text")],
            [btn("📤 Розіслати картинку-графік", "adm:send_img")],
        ])

    ADMIN_HELP = ("<b>Панель адміністратора</b>\n"
                  "«Показати» — лише тобі. «Розіслати» — усім чатам, що ввімкнули «Графік» (з підтвердженням); "
                  "автоматично графік надсилається сам, щойно змінюється на сайті.")

    @router.message(Command("admin"))
    async def admin(message: Message) -> None:
        if (not message.from_user or message.from_user.id not in settings.admin_ids
                or message.chat.type != ChatType.PRIVATE):
            return
        await message.answer(ADMIN_HELP, reply_markup=admin_keyboard())

    SUBS_PER_PAGE = 10

    async def subs_page(bot: Bot, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
        subs = db.all_subscriptions()
        if not subs:
            return "Підписок поки немає.", None
        pages = (len(subs) + SUBS_PER_PAGE - 1) // SUBS_PER_PAGE
        page = max(0, min(page, pages - 1))
        chunk = subs[page * SUBS_PER_PAGE:(page + 1) * SUBS_PER_PAGE]
        titles: dict[str, str] = {}
        for sub in chunk:
            if sub.channel == "tg" and sub.target not in titles:
                titles[sub.target] = ""
                try:
                    chat = await bot.get_chat(int(sub.target))
                    titles[sub.target] = chat.title or chat.full_name or chat.username or ""
                except Exception:   # чат недоступний — покажемо лише ID
                    pass
        lines, last = [f"<b>Підписки</b> ({len(subs)}), сторінка {page + 1}/{pages}"], None
        for sub in chunk:
            key = (sub.channel, sub.target)
            if key != last:
                title = titles.get(sub.target, "")
                lines.append(f"\n<b>{sub.channel}:{escape(sub.target)}</b>" + (f" — {escape(title)}" if title else ""))
                last = key
            flags = ("" if sub.events else " 🔕відкл.") + (" 🖼графік" if sub.graphs else "") + \
                    ("" if sub.silent else " 🔔зі звуком") + (" 🔒" if sub.source == "config" else "")
            queue = f" · черга {sub.queue}" if sub.queue else ""
            lines.append(f"• {escape(sub.label)}{queue} <code>({sub.street_id}, {escape(sub.house)})</code>{flags}")
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀", callback_data=f"adm:subs:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶", callback_data=f"adm:subs:{page + 1}"))
        return "\n".join(lines), (InlineKeyboardMarkup(inline_keyboard=[nav]) if nav else None)

    @router.message(Command("subs"))
    async def subs_command(message: Message, command: CommandObject, bot: Bot) -> None:
        if (not message.from_user or message.from_user.id not in settings.admin_ids
                or message.chat.type != ChatType.PRIVATE):
            return
        arg = (command.args or "").strip()
        text, kb = await subs_page(bot, int(arg) - 1 if arg.isdigit() else 0)
        await message.answer(text, reply_markup=kb)

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if not message.from_user or message.from_user.id not in settings.admin_ids:
            return
        await message.answer(stats_text())

    @router.callback_query(F.data.regexp(r"^adm:(stats|poll|show_text|show_img|send_text|send_img|subs)(:(yes|no|\d+))?$"))
    async def admin_action(cb: CallbackQuery) -> None:
        if cb.from_user.id not in settings.admin_ids or cb.message.chat.type != ChatType.PRIVATE:
            await cb.answer("Лише для адміністраторів.", show_alert=True)
            return
        parts = cb.data.split(":")
        action, confirm = parts[1], (parts[2] if len(parts) > 2 else None)

        if action == "subs":
            text, kb = await subs_page(cb.bot, int(confirm or 0))
            try:
                if confirm is None:
                    await cb.message.answer(text, reply_markup=kb)
                else:
                    await cb.message.edit_text(text, reply_markup=kb)
            except TelegramAPIError:   # напр. «message is not modified»
                pass
            await cb.answer()
        elif action == "stats":
            await cb.answer()
            await cb.message.answer(stats_text())
        elif action == "poll":
            await cb.answer("Опитую сайт…")
            await monitor.poll_once()
            await cb.message.answer(f"🔄 Готово: {monitor.last_poll_summary}")
        elif action in {"show_text", "show_img"}:
            await cb.answer()
            await send_site_graph(cb.message, text=action == "show_text", image=action == "show_img")
        else:  # send_text / send_img — розсилка вручну, з підтвердженням
            as_text = action == "send_text"
            kind = "текстовий графік" if as_text else "картинку-графік"
            if confirm == "no":
                await cb.message.edit_text("Розсилку скасовано.")
                await cb.answer()
            elif confirm is None:
                graphs = await hoe.fetch_graphs()
                available = graphs and (graphs.text if as_text else graphs.image_url)
                if graphs is None:
                    await cb.answer("Сторінку графіків не вдалося завантажити.", show_alert=True)
                elif not available:
                    await cb.answer(f"На сайті зараз немає: {kind}.", show_alert=True)
                else:
                    preview = ("\n\n" + escape(fit_body(graphs.text, 1200))) if as_text else ""
                    targets = db.graph_targets()
                    n = len(targets)
                    if as_text and any(t.queues for t in targets):
                        preview += "\n\n<i>Чатам із вказаною чергою піде лише рядки їхніх черг.</i>"
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="✅ Так, розіслати", callback_data=f"adm:{action}:yes"),
                        InlineKeyboardButton(text="✖ Скасувати", callback_data=f"adm:{action}:no"),
                    ]])
                    await cb.message.answer(f"Розіслати {kind} <b>{n}</b> чатам, які ввімкнули «Графік»?{preview}",
                                            reply_markup=kb)
                    await cb.answer()
            else:  # yes
                await cb.answer()
                await cb.message.edit_text("⏳ Розсилаю…")
                report = await monitor.send_graphs(image=not as_text, text=as_text, force=True)
                sent = report.text_sent if as_text else report.image_sent
                if not report.page_ok:
                    result = "❌ Сторінку графіків не вдалося завантажити."
                elif report.error:
                    result = f"❌ Помилка: {escape(report.error)}."
                else:
                    result = f"✅ Надіслано {sent} з {report.targets} чатів."
                await cb.message.edit_text(result)

    return router


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="add", description="Додати адресу"),
        BotCommand(command="list", description="Мої адреси"),
        BotCommand(command="check", description="Перевірити зараз"),
        BotCommand(command="graph", description="Актуальний графік"),
        BotCommand(command="help", description="Довідка"),
    ])


def build_dispatcher(settings: Settings, db: Database, hoe: HoeClient, monitor: Monitor) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(settings, db, hoe, monitor))
    return dp