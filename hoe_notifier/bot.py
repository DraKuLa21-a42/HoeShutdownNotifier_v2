"""Telegram-бот для керування адресами."""
from __future__ import annotations

import json
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
from .monitor import MANUAL_REFRESH_MAX_AGE, Monitor, describe_state

log = logging.getLogger(__name__)

HELP = (
    "<b>Сповіщення про погодинні відключення (Хмельницькобленерго)</b>\n\n"
    "/add — додати адресу: населений пункт → вулиця → будинок\n"
    "/add_id <code>&lt;streetId&gt; &lt;будинок&gt; [назва]</code> — додати адресу за ID вулиці\n"
    "/list — мої адреси та налаштування (сповіщення, графіки, звук)\n"
    "/check — перевірити всі мої адреси зараз\n"
    "/graph — показати актуальний графік\n"
    "/cancel — скасувати поточну дію\n\n"
    "<b>Сповіщення в канал або групу.</b> Додай бота в канал адміністратором з правом "
    "«Публікація повідомлень» і дописуй до команди <code>--to=&lt;ID каналу або @username&gt;</code>:\n"
    "<code>/add_id 123456 12 Дім --to=-1001234567890</code>\n"
    "<code>/add --to=@my_channel</code>\n"
    "Команди надсилай мені в приватні повідомлення; керувати такими адресами можна з /list.\n"
    "У груповому чаті керувати адресами можуть лише адміністратори групи."
)

# На мобільних "--" часто автозамінюється на тире, тому приймаємо і його.
TO_RE = re.compile(r"(?:^|\s)(?:--|—|–)to(?:=|\s+)(\S+)", re.IGNORECASE)
USERNAME_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,31}")


class AddFlow(StatesGroup):
    settlement_query = State()
    settlement_pick = State()
    street_query = State()
    street_pick = State()
    house = State()


class TargetError(Exception):
    """Користувачу показується текст помилки як є."""


def split_to(args: str | None) -> tuple[str, str | None]:
    """Відокремлює `--to=...` від решти аргументів команди."""
    args = args or ""
    m = TO_RE.search(args)
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
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2])


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
                         target: int | None = None, target_title: str | None = None) -> None:
        external = target is not None and target != message.chat.id
        chat_id = str(target if external else message.chat.id)
        if db.count_for_target("tg", chat_id) >= settings.max_addresses_per_chat:
            where = f"«{escape(target_title or chat_id)}»" if external else "чат"
            await message.answer(f"Ліміт: {settings.max_addresses_per_chat} адрес на {where}. Видали зайві в /list.")
            return
        if db.find_subscription("tg", chat_id, street_id, house):
            await message.answer("Ця адреса вже є в списку" + (" цього каналу." if external else "."))
            return

        # Спочатку оновлюємо стан (розсилка існуючим підписникам), потім підписуємо — без дублів.
        await monitor.refresh_address(street_id, house, max_age=MANUAL_REFRESH_MAX_AGE)
        state = db.get_state(street_id, house)
        status = describe_state(state) if state else (
            "Не вдалося отримати дані з сайту зараз — повідомлю, щойно вони з'являться.")

        if external:
            # Перше повідомлення в канал водночас перевіряє, що публікація справді працює.
            try:
                await message.bot.send_message(
                    int(chat_id), f"📍 <b>{escape(label)}</b>\n\n<pre>{escape(status)}</pre>",
                    disable_notification=settings.default_silent)
            except TelegramAPIError as exc:
                log.warning("Не вдалося написати в %s: %s", chat_id, exc)
                await message.answer("Не вдалося надіслати повідомлення в цей чат (перевір права бота). "
                                     "Адресу не додано.")
                return

        db.add_subscription(
            "tg", chat_id, street_id, house, label,
            graphs=settings.default_graphs, silent=settings.default_silent, owner_id=user_id,
        )
        if external:
            await message.answer(f"✅ Додано в «{escape(target_title or chat_id)}»: <b>{escape(label)}</b>\n"
                                 "Налаштування (графік, звук) — у /list.")
        else:
            await message.answer(
                f"✅ Додано: <b>{escape(label)}</b>\n\nПоточний стан:\n<pre>{escape(status)}</pre>\n"
                "Налаштування (графік, звук) — у /list.")

    async def maybe_resolve(message: Message, raw_to: str | None) -> tuple[int | None, str | None, bool]:
        """(target, title, ok). ok=False — помилку вже показано користувачу."""
        if raw_to is None:
            return None, None, True
        try:
            target, title = await resolve_target(message.bot, raw_to, message.from_user.id)
        except TargetError as exc:
            await message.answer(str(exc))
            return None, None, False
        return target, title, True

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
        _, raw_to = split_to(command.args)
        target, title, ok = await maybe_resolve(message, raw_to)
        if not ok:
            return
        await state.clear()
        await state.set_state(AddFlow.settlement_query)
        await state.update_data(target=target, target_title=title)
        where = f" (у «{escape(title)}»)" if title else ""
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
        await finish_add(message, user_id, data["street_id"], house_no, label,
                         data.get("target"), data.get("target_title"))

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
        parts = rest.split(maxsplit=2)
        if len(parts) < 2:
            await message.answer("Формат: <code>/add_id &lt;streetId&gt; &lt;будинок&gt; [назва] "
                                 "[--to=&lt;ID каналу або @username&gt;]</code>")
            return
        target, title, ok = await maybe_resolve(message, raw_to)
        if not ok:
            return
        street_id, house_no = parts[0], parts[1]
        label = parts[2].strip() if len(parts) > 2 else f"вул. #{street_id}, {house_no}"
        await state.clear()
        await finish_add(message, message.from_user.id, street_id, house_no, label[:80], target, title)

    # --- список і керування -----------------------------------------------
    async def chat_title(bot: Bot, chat_id: str) -> str:
        try:
            chat = await bot.get_chat(int(chat_id))
            return chat.title or chat.username or chat_id
        except (TelegramAPIError, ValueError):
            return chat_id

    @router.message(Command("list"))
    async def list_(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not allowed(message.chat.id, user_id):
            return
        subs = [(s, None) for s in db.list_subscriptions("tg", str(message.chat.id))]
        if message.chat.type == ChatType.PRIVATE:
            for s in db.list_by_owner(user_id, "tg", str(message.chat.id)):
                subs.append((s, await chat_title(message.bot, s.target)))
        if not subs:
            await message.answer("Адрес ще немає. Додай через /add.")
            return
        for sub, where in subs:
            await message.answer(sub_text(sub, where), reply_markup=sub_keyboard(sub))

    @router.callback_query(F.data.regexp(r"^sub:\d+:(events|graphs|silent|del)$"))
    async def sub_action(cb: CallbackQuery) -> None:
        _, sub_id, action = cb.data.split(":")
        sub = db.get_subscription(int(sub_id))
        chat = cb.message.chat
        user_id = cb.from_user.id
        if sub is None or sub.channel != "tg":
            await cb.answer("Запис не знайдено.", show_alert=True)
            return
        if sub.target == str(chat.id):
            permitted = await can_manage(cb.bot, chat.id, chat.type, user_id)
        else:
            # Підписка в каналі/групі, якою керують із приватного чату: лише її автор,
            # і він досі має бути адміністратором цього чату.
            permitted = (chat.type == ChatType.PRIVATE and sub.owner_id == user_id
                         and await can_manage(cb.bot, int(sub.target), ChatType.CHANNEL, user_id))
        if not permitted:
            await cb.answer("Недостатньо прав або запис не знайдено.", show_alert=True)
            return
        if sub.source == "config":
            await cb.answer("Цей запис керується через addresses.toml.", show_alert=True)
            return
        if action == "del":
            db.delete_subscription(sub.id)
            await cb.message.edit_text(f"🗑 Видалено: <b>{escape(sub.label)}</b>")
        else:
            sub = db.toggle(sub.id, action)
            await cb.message.edit_reply_markup(reply_markup=sub_keyboard(sub))
        await cb.answer()

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
                f"📍 <b>{escape(sub.label)}</b>\n<pre>{escape(describe_state(state))}</pre>{suffix}")

    @router.message(Command("graph"))
    async def graph(message: Message) -> None:
        if not allowed(message.chat.id, message.from_user.id if message.from_user else 0):
            return
        url = await hoe.fetch_graph_url()
        if not url:
            await message.answer("Не вдалося знайти графік на сайті.")
            return
        try:
            data = await hoe.download(url)
        except Exception:
            await message.answer("Не вдалося завантажити графік.")
            return
        await message.answer_photo(BufferedInputFile(data, filename="graph.png"))

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if not message.from_user or message.from_user.id not in settings.admin_ids:
            return
        s = db.stats()
        last = (time.strftime("%H:%M:%S %d.%m.%Y", time.localtime(monitor.last_poll_at))
                if monitor.last_poll_at else "—")
        await message.answer(
            f"Чатів: {s['targets']}\nПідписок: {s['subscriptions']}\nУнікальних адрес: {s['addresses']}\n"
            f"Останній цикл: {last} ({monitor.last_poll_summary})\n"
            f"Інтервал: {settings.poll_interval} с")

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