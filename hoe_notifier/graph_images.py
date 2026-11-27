"""Картинки графіка, які бот малює сам із текстового графіка, та їхній життєвий цикл у Telegram.

На кожну дату в кожному чаті є одне «живе» повідомлення-картинка:
  * з'явився графік на дату — нове повідомлення (зі звуком, якщо чат не вимкнув звук);
  * опівночі «Завтра» → «Сьогодні» (з лінією поточного часу), учорашнє «Сьогодні» → сіре «Минув» —
    це редагування тих самих повідомлень, без нових сповіщень;
  * протягом дня лінія часу й статус у підписі оновлюються раз на `live_minutes` хвилин;
  * графік на дату змінили — нове повідомлення з позначками змін (зі звуком, лише якщо зміни стосуються
    черг чату), попереднє стає сірим «Застарілим» і на нього дається відповідь.
У Slack так само, лише без кнопки «Оновити» і без відповіді на попереднє повідомлення (картинка в Slack —
приватний файл бота в image-блоці; оновлення — chat.update з новим файлом).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from typing import Awaitable, Callable

from . import render
from .db import Database, GraphMessage, GraphTarget
from .schedule import (
    DaySchedule, changes, day_label, day_state, fmt_intervals, hours_off, now_kyiv, relative_word, slots_from_json,
    slots_to_json, status_now,
)

log = logging.getLogger(__name__)

REFRESH_CALLBACK = "gimg:r:"          # + YYYY-MM-DD
REFRESH_COOLDOWN = 15                 # с між натисканнями «Оновити» на одному повідомленні
KEEP_DAYS = 7                         # скільки днів зберігати графіки й записи про повідомлення
TICK_INTERVAL = 60


@dataclass
class PublishReport:
    days: list[str] = field(default_factory=list)     # дати, які розіслано (нові або змінені)
    sent: int = 0
    targets: int = 0
    error: str | None = None


@dataclass(frozen=True)
class DayData:
    day: date
    hash: str
    slots: dict
    updated: str | None
    prev_hash: str | None


Deliver = Callable[..., Awaitable[tuple[bool, object, object]]]


class GraphImages:
    def __init__(self, db: Database, sinks: dict, *, deliver: Deliver | None = None, enabled: bool = True,
                 live_minutes: int = 30, send_delay: float = 0.05,
                 clock: Callable[[], datetime] = now_kyiv) -> None:
        self.db = db
        self.sinks = sinks
        self.deliver = deliver or self._plain_deliver
        self.enabled = enabled
        self.live_minutes = live_minutes
        self.send_delay = send_delay
        self.clock = clock
        self.lock = asyncio.Lock()
        self._pressed: dict[tuple[str, str, str], float] = {}
        self.last_tick_at: float | None = None

    # --- стан ------------------------------------------------------------
    def problem(self) -> str | None:
        """None — можна малювати й розсилати; інакше причина (для адмін-панелі)."""
        if not self.enabled:
            return "вимкнено в /settings"
        return render.available()

    def load_day(self, day: str) -> DayData | None:
        cur = self.db.graph_day(day)
        if not cur:
            return None
        version = self.db.graph_version(day, cur[0])
        if not version:
            return None
        return DayData(date.fromisoformat(day), cur[0], slots_from_json(version[0]), version[1], cur[1])

    def version_slots(self, day: str, hash_: str | None) -> tuple[dict, str | None] | None:
        if not hash_:
            return None
        version = self.db.graph_version(day, hash_)
        return (slots_from_json(version[0]), version[1]) if version else None

    def current_days(self) -> list[DayData]:
        today = self.clock().date().isoformat()
        return [d for d in (self.load_day(k) for k in self.db.graph_days_since(today)) if d]

    def chat_queues(self, channel: str, target: str) -> frozenset[str] | None:
        """Черги, які підсвічувати: адреси чату з «Графік: картинка» (або всі адреси, якщо таких немає)."""
        subs = [s for s in self.db.list_subscriptions(channel, target)]
        image_subs = [s for s in subs if s.graph_images] or subs
        if not image_subs or any(s.queue is None for s in image_subs):
            return None
        return frozenset(s.queue for s in image_subs)

    # --- малювання ---------------------------------------------------------
    def render_key(self, state: str, now: datetime) -> str:
        if state != "today":
            return state
        if not self.live_minutes:
            return "today"
        return f"today:{(now.hour * 60 + now.minute) // self.live_minutes}"

    def compose(self, day: str, hash_: str, state: str, queues: frozenset[str] | None, *,
                diff_hash: str | None = None, now: datetime | None = None, snapshot: bool = False,
                footer: str | None = None, channel: str = "tg") -> tuple[bytes, str, list[tuple[str, str]]]:
        """(PNG, підпис, кнопки) для версії `hash_` графіка на `day` у стані `state`."""
        data = self.version_slots(day, hash_)
        if data is None:
            raise LookupError(f"немає графіка {day}/{hash_}")
        slots, updated = data
        prev = self.version_slots(day, diff_hash)
        prev_slots = prev[0] if prev else None
        now = now or self.clock()
        d = date.fromisoformat(day)
        show_now = state == "today" and (self.live_minutes or snapshot)
        # на сірих (минулих/застарілих) картинках позначки змін лише заважають
        png = render.render_day(d, slots, state=state, highlight=queues,
                                previous=prev_slots if state in {"today", "future"} else None,
                                site_updated=updated, now=now if show_now else None, footer=footer)
        caption = self.caption(d, state, slots, queues, prev_slots=prev_slots, updated=updated, now=now,
                               snapshot=snapshot, channel=channel)
        buttons = [("🔄 Оновити", REFRESH_CALLBACK + day)] if state in {"today", "future"} else []
        return png, caption, buttons

    def caption(self, d: date, state: str, slots: dict, queues: frozenset[str] | None, *,
                prev_slots: dict | None, updated: str | None, now: datetime, snapshot: bool = False,
                channel: str = "tg") -> str:
        today = now.date()
        when = f"{relative_word(d, today)}, {day_label(d)}" if abs((d - today).days) <= 1 else day_label(d)
        head = {
            "future": f"📅 <b>{when}</b> — графік відключень",
            "today": f"📆 <b>{when}</b> — графік відключень",
            "past": f"⚪ <b>Минув:</b> графік на {day_label(d)}",
            "superseded": f"❌ <b>Застарів:</b> графік на {day_label(d)} — нижче новіша версія",
        }[state]
        lines = []
        if prev_slots is not None and state in {"today", "future"}:
            lines.append("⚠️ <b>Графік змінено</b>" + (f" (на сайті оновлено о {escape(updated)})" if updated else ""))
        lines.append(head)
        if state in {"past", "superseded"}:
            return "\n".join(lines)
        if queues:
            for q in sorted(queues):
                lines.append(f"\n<b>{q}</b>: {fmt_intervals(slots[q])} ({hours_off(slots[q])})")
                if prev_slots is not None:
                    diff = changes(prev_slots[q], slots[q])
                    lines.append(f"зміни: {diff}" if diff else "для цієї черги без змін")
                if state == "today":
                    lines.append(status_now(slots[q], now, with_left=snapshot))
        elif channel == "tg":
            lines.append("\nУсі черги. Свою чергу можна обрати в /list → «🔢 Черга» — її буде підсвічено.")
        else:   # у Slack команд бота немає — чергу задають у Telegram, тож підказка там лише заплутує
            lines.append("\nУсі черги.")
        if state == "today":
            lines.append(f"\n<i>{'Знімок' if snapshot else 'Оновлюється автоматично'} · {now.strftime('%H:%M')}</i>")
        return "\n".join(lines)

    # --- розсилка ------------------------------------------------------------
    async def _plain_deliver(self, target: GraphTarget, method: str, *args, **kwargs):
        sink = self.sinks.get(target.channel)
        if sink is None:
            return False, None, target
        try:
            return True, await getattr(sink, method)(target.target, *args, **kwargs), target
        except Exception:
            log.exception("Не вдалось надіслати картинку графіка %s/%s", target.channel, target.target)
            return False, None, target

    async def publish(self, days: list[DaySchedule] | tuple[DaySchedule, ...], targets: list[GraphTarget], *,
                      force: bool = False) -> PublishReport:
        """Зберігає графіки з сайту; нові та змінені розсилає. `force` — надіслати всі актуальні заново."""
        report = PublishReport(targets=len(targets))
        if self.problem():
            report.error = self.problem()
            return report
        async with self.lock:
            today = self.clock().date()
            changed: dict[str, str | None] = {}           # день → попередній хеш (None — дата нова)
            for day in sorted(days, key=lambda d: d.day):
                if day.day < today:
                    continue                              # графік на минулу дату — не цікаво
                prev = self.db.save_graph_version(day.key, day.hash, slots_to_json(day.slots), day.updated)
                if prev is not False:
                    changed[day.key] = prev
                    log.info("Графік на %s: %s", day.key, "новий" if prev is None else "змінено")
            if force:
                for d in self.db.graph_days_since(today.isoformat()):
                    changed.setdefault(d, "resend")
            for day_key in sorted(changed):
                prev = changed[day_key]
                report.days.append(day_key)
                kind = "resend" if prev == "resend" else ("new" if prev is None else "update")
                for t in targets:
                    if await self._send_day(t, day_key, kind):
                        report.sent += 1
                    await asyncio.sleep(self.send_delay)
            self.db.purge_graphs((today - timedelta(days=KEEP_DAYS)).isoformat())
        return report

    async def ensure_chat(self, channel: str, target: str, *, silent: bool = True) -> int:
        """Чат щойно ввімкнув картинку: надсилає актуальні графіки, яких у ньому ще немає."""
        if self.problem():
            return 0
        sent = 0
        async with self.lock:
            t = GraphTarget(channel, str(target), silent, self.chat_queues(channel, str(target)))
            for day in self.current_days():
                if not self.db.live_graph_messages(channel, str(target), day.day.isoformat()):
                    sent += bool(await self._send_day(t, day.day.isoformat(), "new"))
        return sent

    async def _send_day(self, t: GraphTarget, day: str, kind: str) -> bool:
        data = self.load_day(day)
        if data is None:
            return False
        now = self.clock()
        state = day_state(data.day, now.date())
        if state == "past":
            return False
        diff_hash = data.prev_hash if kind == "update" else None
        old_live = self.db.live_graph_messages(t.channel, t.target, day)
        silent = t.silent
        if kind == "update" and diff_hash:
            prev = self.version_slots(day, diff_hash)
            if prev and t.queues is not None and all(prev[0][q] == data.slots[q] for q in t.queues):
                silent = True                         # зміни не стосуються черг цього чату — тихо
        try:
            png, caption, buttons = self.compose(day, data.hash, state, t.queues, diff_hash=diff_hash, now=now,
                                                 channel=t.channel)
        except Exception:
            log.exception("Не вдалось намалювати графік %s", day)
            return False
        sink = self.sinks.get(t.channel)
        tg = t.channel == "tg"                        # кнопки й відповіді — лише в Telegram
        if sink is not None and hasattr(sink, "send_graph"):
            reply_to = old_live[-1].message_id if old_live and kind == "update" and tg else None
            ok, message_id, delivered = await self.deliver(t, "send_graph", png, caption, silent=silent,
                                                           buttons=buttons if tg else (), reply_to=reply_to)
        else:
            ok, message_id, delivered = await self.deliver(t, "send_photo", png, silent=silent)
        if not ok:
            return False
        if message_id and hasattr(sink, "edit_graph"):
            self.db.add_graph_message(delivered.channel, delivered.target, day, str(message_id), data.hash,
                                      diff_hash, state, self.render_key(state, now))
        for m in old_live:                            # попередні повідомлення на цю дату більше не живі
            footer = "Надіслано новішу копію" if kind == "resend" else None
            await self._edit(m, "superseded", now, footer=footer)
        return True

    async def _edit(self, m: GraphMessage, state: str, now: datetime, *, queues: frozenset[str] | None = None,
                    footer: str | None = None) -> bool:
        """Перемальовує повідомлення в стані `state`. False — повідомлення вже недоступне."""
        from .sinks import MessageGone, TargetGone
        sink = self.sinks.get(m.channel)
        if sink is None or not hasattr(sink, "edit_graph"):
            self.db.update_graph_message(m.id, state="gone")
            return False
        if queues is None:
            queues = self.chat_queues(m.channel, m.target)
        try:
            png, caption, buttons = self.compose(m.day, m.hash, state, queues, diff_hash=m.diff_hash, now=now,
                                                 footer=footer, channel=m.channel)
            new_id = await sink.edit_graph(m.target, m.message_id, png, caption,
                                           buttons=buttons if m.channel == "tg" else ())
        except (MessageGone, TargetGone) as exc:
            log.info("Картинку графіка %s/%s (%s) більше не можна змінити: %s", m.channel, m.target,
                     m.message_id, exc)
            self.db.update_graph_message(m.id, state="gone")
            return False
        except LookupError:
            self.db.update_graph_message(m.id, state="gone")
            return False
        except Exception:
            log.warning("Не вдалось оновити картинку графіка %s/%s", m.channel, m.target, exc_info=True)
            return False
        changed = {"message_id": str(new_id)} if new_id and str(new_id) != m.message_id else {}   # Slack: новий файл
        self.db.update_graph_message(m.id, state=state, render_key=self.render_key(state, now), **changed)
        return True

    # --- щохвилинне оновлення ----------------------------------------------------
    async def tick(self) -> int:
        """Переводить повідомлення між станами й оновлює лінію часу. Повертає кількість змінених."""
        if self.problem():
            return 0
        edited = 0
        async with self.lock:
            now = self.clock()
            today = now.date()
            for m in self.db.live_graph_messages():
                cur = self.db.graph_day(m.day)
                if cur and cur[0] != m.hash:
                    state = "superseded"                  # нова версія з'явилась, а це повідомлення пропущено
                else:
                    state = day_state(date.fromisoformat(m.day), today)
                if self.render_key(state, now) == m.render_key:
                    continue
                edited += await self._edit(m, state, now)
                await asyncio.sleep(self.send_delay)
            self.last_tick_at = time.time()
        return edited

    async def run_forever(self, interval: float = TICK_INTERVAL) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Збій оновлення картинок графіка")
            # прокидаємось на початку хвилини, щоб перехід доби й межі слотів не запізнювались
            await asyncio.sleep(max(5.0, interval - self.clock().second))

    # --- кнопка «Оновити» та знімки ----------------------------------------------
    async def refresh(self, channel: str, target: str, message_id: str, day: str) -> str | None:
        """Перемальовує повідомлення з кнопки. Повертає підказку для користувача (None — оновлено)."""
        key = (channel, str(target), str(message_id))
        if time.monotonic() - self._pressed.get(key, 0) < REFRESH_COOLDOWN:
            return "Щойно оновлено — спробуй трохи згодом."
        self._pressed[key] = time.monotonic()
        if len(self._pressed) > 2000:
            self._pressed.clear()
        if self.problem():
            return f"Картинки графіка недоступні: {self.problem()}."
        async with self.lock:
            now = self.clock()
            m = self.db.find_graph_message(channel, str(target), str(message_id))
            cur = self.db.graph_day(day)
            if cur is None:
                return "Графіка на цю дату більше немає."
            if m is not None:
                if m.state == "superseded" or cur[0] != m.hash:
                    await self._edit(m, "superseded", now)
                    return "Є новіша версія графіка — дивись нижче."
                state = day_state(date.fromisoformat(day), now.date())
                await self._edit(m, state, now)
                return None
            # знімок із /graph: показуємо найновішу версію на цю дату
            sink = self.sinks.get(channel)
            state = day_state(date.fromisoformat(day), now.date())
            png, caption, buttons = self.compose(day, cur[0], state, self.chat_queues(channel, str(target)),
                                                 now=now, snapshot=True)
            await sink.edit_graph(str(target), str(message_id), png, caption, buttons=buttons)
            return None

    def snapshots(self, queues: frozenset[str] | None) -> list[tuple[bytes, str, list[tuple[str, str]]]]:
        """Картинки актуальних графіків (сьогодні, завтра) для /graph і адмін-панелі — без відстеження."""
        now = self.clock()
        out = []
        for day in self.current_days():
            state = day_state(day.day, now.date())
            out.append(self.compose(day.day.isoformat(), day.hash, state, queues, now=now, snapshot=True))
        return out