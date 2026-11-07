"""Ядро: опитування сайту, порівняння зі збереженим станом, розсилка змін."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Awaitable, Callable

from .db import AddressState, Database, GraphTarget, Subscription
from .hoe import NO_OUTAGES, HoeClient, HoeUnavailable, ParseStatus, Parsed, parse_events
from .queues import filter_schedule, outage_view
from .sinks import Sink, TargetGone, TargetMigrated

log = logging.getLogger(__name__)

SUBJECT_NEW = "З'явились погодинні відключення!"
SUBJECT_CHANGED = "Змінились погодинні відключення!"
SUBJECT_NONE = "Погодинних відключень немає!"
FAILURE_ALERT_AFTER = 3  # поспіль невдалих циклів, перш ніж писати адміну
FAIL_FAST_AFTER = 3      # скільки адрес поспіль мають не відповісти, щоб перервати цикл
TEXT_GRAPH_SUBJECT = "Графік погодинних відключень"
MANUAL_REFRESH_MAX_AGE = 60  # /check та додавання адреси не б'ють у сайт частіше, ніж раз на стільки секунд


@dataclass
class GraphReport:
    """Підсумок розсилки графіка (для адмін-панелі та логів)."""
    page_ok: bool = True        # сторінку графіків вдалось завантажити
    image_found: bool = False   # на сторінці є картинка
    text_found: bool = False    # на сторінці є текстовий графік
    targets: int = 0            # чатів із увімкненим «Графіком»
    image_sent: int = 0         # скільком чатам доставлено картинку
    text_sent: int = 0          # скільком чатам доставлено текст
    error: str | None = None
    text: str | None = None     # текст графіка, як він на сайті


def subject_none(queue: str | None = None) -> str:
    return SUBJECT_NONE if not queue else f"Погодинних відключень для черги {queue} немає!"


def label_for(sub: Subscription) -> str:
    """Підпис адреси в повідомленні; для адрес із фільтром — із зазначенням черги."""
    return sub.label if not sub.queue else f"{sub.label} · черга {sub.queue}"


def describe_state(state: AddressState | None, queue: str | None = None) -> str:
    """Поточний стан адреси простим текстом; `queue` — показати лише записи цієї черги."""
    if state is None:
        return "Дані поки не отримано."
    return outage_view(state.last_text, queue) or subject_none(queue)


class Monitor:
    def __init__(
        self,
        db: Database,
        hoe: HoeClient,
        sinks: dict[str, Sink],
        *,
        poll_interval: int = 900,
        concurrency: int = 3,
        admin_notify: Callable[[str], Awaitable[None]] | None = None,
        send_delay: float = 0.05,
    ) -> None:
        self.db = db
        self.hoe = hoe
        self.sinks = sinks
        self.poll_interval = poll_interval
        self.concurrency = concurrency
        self.admin_notify = admin_notify
        self.send_delay = send_delay
        self.last_poll_at: float | None = None
        self.last_poll_summary = "ще не запускався"
        self._failed_cycles = 0
        self._alert_sent = False
        self._locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._fetched: dict[tuple[str, str], tuple[float, Parsed]] = {}
        self._fail_streak = 0
        self._abort_cycle = False

    # --- одна адреса ----------------------------------------------------
    async def refresh_address(self, street_id: str, house: str, *, max_age: float = 0) -> Parsed | None:
        """Оновлює стан адреси і розсилає зміни. None — сайт недоступний.

        `max_age` > 0: якщо адресу вже опитували менше стількох секунд тому, повертає результат без запиту
        (захист сайту від частих ручних перевірок). Паралельні виклики для однієї адреси виконуються по черзі.
        """
        key = (street_id, house)
        async with self._locks[key]:
            cached = self._fetched.get(key)
            if max_age and cached and time.monotonic() - cached[0] < max_age:
                return cached[1]
            return await self._refresh(street_id, house, key)

    async def _refresh(self, street_id: str, house: str, key: tuple[str, str]) -> Parsed | None:
        try:
            html = await self.hoe.fetch_events_html(street_id, house)
        except HoeUnavailable as exc:
            log.warning("Адреса %s/%s: сайт недоступний (%s)", street_id, house, exc)
            return None

        parsed = parse_events(html)
        if parsed.status is ParseStatus.UNKNOWN:
            log.error("Адреса %s/%s: невідомий формат відповіді (верстка змінилась?)", street_id, house)
            return parsed
        self._fetched[key] = (time.monotonic(), parsed)

        current = parsed.text if parsed.status is ParseStatus.OUTAGES else NO_OUTAGES
        state = self.db.get_state(street_id, house)
        previous = state.last_text if state else None
        if current == previous:
            return parsed

        # стан зберігаємо до розсилки: збій доставки не має спричиняти нескінченні повтори
        self.db.save_state(street_id, house, current)
        await self._broadcast_changes(self.db.event_subscribers(street_id, house), previous, current)
        return parsed

    async def _send(self, sub: Subscription | GraphTarget, method: str, *args, **kwargs) -> bool:
        """Доставка одному адресату. True — доставлено.

        Недоступного адресата відписуємо, перенесеному (group→supergroup) — переносимо підписки.
        """
        sink = self.sinks.get(sub.channel)
        if sink is None:
            log.warning("Немає каналу доставки %r для %s", sub.channel, sub.target)
            return False
        for _ in range(2):
            try:
                await getattr(sink, method)(sub.target, *args, **kwargs)
                return True
            except TargetMigrated as moved:
                log.warning("Чат %s перенесено на %s — оновлюю підписки", sub.target, moved.new_target)
                self.db.move_target(sub.channel, sub.target, moved.new_target)
                sub = dataclasses.replace(sub, target=moved.new_target)
            except TargetGone as exc:
                removed = self.db.delete_target(sub.channel, sub.target)
                log.warning("Адресат %s/%s недоступний, видалено підписок: %d (%s)",
                            sub.channel, sub.target, removed, exc)
                return False
            except Exception:
                log.exception("Не вдалось надіслати повідомлення %s/%s", sub.channel, sub.target)
                return False
        return False

    async def _broadcast_changes(self, subs: list[Subscription], previous: str | None, current: str) -> None:
        """Кожному підписнику — лише те, що стосується його черги, і лише якщо це змінилось.

        `previous` = None — адресу бачимо вперше (пишемо всім, навіть якщо для черги відключень немає).
        """
        for sub in subs:
            new_view = outage_view(current, sub.queue)
            old_view = outage_view(previous, sub.queue)
            if previous is not None and new_view == old_view:
                continue  # змінилось лише те, що цю підписку не стосується
            if new_view:
                subject = SUBJECT_CHANGED if old_view else SUBJECT_NEW
            else:
                subject = subject_none(sub.queue)
            await self._send(sub, "send_text", subject, label_for(sub), new_view, silent=sub.silent)
            await asyncio.sleep(self.send_delay)

    # --- графіки (картинка та/або текст) ---------------------------------
    async def send_graphs(self, *, image: bool = True, text: bool = True, force: bool = False) -> GraphReport:
        """Надсилає графік підписникам із увімкненим «Графіком».

        Без `force` — лише якщо він змінився з минулого разу (картинка — за URL, текст — за вмістом).
        `force=True` — розсилка вручну з адмін-панелі, незалежно від змін.
        """
        report = GraphReport()
        graphs = await self.hoe.fetch_graphs()
        if graphs is None:
            report.page_ok = False
            return report
        targets = self.db.graph_targets()
        report.targets = len(targets)
        report.image_found, report.text_found, report.text = bool(graphs.image_url), bool(graphs.text), graphs.text

        if image and graphs.image_url and (force or graphs.image_url != self.db.kv_get("last_image_url")):
            await self._publish_image(graphs.image_url, targets, report)
        if text and graphs.text and (force or graphs.text != self.db.kv_get("last_text_graph")):
            await self._publish_text(graphs.text, targets, report, force=force)
        return report

    async def _publish_image(self, url: str, targets: list[GraphTarget], report: GraphReport) -> None:
        data = b""
        if targets:
            try:
                data = await self.hoe.download(url)
            except Exception as exc:
                log.warning("Не вдалось завантажити графік %s: %s", url, exc)
                report.error = "не вдалось завантажити картинку"
                return  # last_image_url не оновлюємо — спробуємо в наступному циклі
        for sub in targets:
            if await self._send(sub, "send_photo", data, silent=sub.silent):
                report.image_sent += 1
            await asyncio.sleep(self.send_delay)
        self.db.kv_set("last_image_url", url)

    async def _publish_text(self, text: str, targets: list[GraphTarget], report: GraphReport, *,
                            force: bool = False) -> None:
        """Кожен чат отримує графік лише своїх черг; якщо для них нічого не змінилось — мовчимо."""
        for target in targets:
            body = filter_schedule(text, target.queues)
            key = f"graph_text:{target.channel}:{target.target}"
            if not force and body == self.db.kv_get(key):
                continue  # змінились лише рядки інших черг
            if await self._send(target, "send_text", TEXT_GRAPH_SUBJECT, "", body, silent=target.silent, pre=False):
                report.text_sent += 1
                self.db.kv_set(key, body)
            await asyncio.sleep(self.send_delay)
        self.db.kv_set("last_text_graph", text)

    async def check_graphs(self) -> None:
        await self.send_graphs()

    # --- цикл -----------------------------------------------------------
    async def poll_once(self) -> None:
        addresses = self.db.event_addresses()
        sem = asyncio.Semaphore(self.concurrency)

        self._fail_streak, self._abort_cycle = 0, False

        async def one(addr: tuple[str, str]):
            if self._abort_cycle:
                return None
            async with sem:
                if self._abort_cycle:
                    return None
                res = await self.refresh_address(*addr)
            if res is None:  # сайт не відповів
                self._fail_streak += 1
                if self._fail_streak >= FAIL_FAST_AFTER and not self._abort_cycle:
                    self._abort_cycle = True
                    log.warning("Сайт не відповів на %d адреси поспіль — цикл перервано, повторю наступного разу",
                                self._fail_streak)
            else:
                self._fail_streak = 0
            return res

        results = await asyncio.gather(*(one(a) for a in addresses), return_exceptions=True)
        ok = 0
        for addr, res in zip(addresses, results):
            if isinstance(res, BaseException):
                log.error("Адреса %s: помилка опитування", addr, exc_info=res)
            elif res is not None and res.status is not ParseStatus.UNKNOWN:
                ok += 1
        try:
            await self.check_graphs()
        except Exception:
            log.exception("Помилка перевірки графіків")

        self.last_poll_at = time.time()
        self.last_poll_summary = f"{ok}/{len(addresses)} адрес оновлено"
        await self._track_health(len(addresses), ok)

    async def _track_health(self, total: int, ok: int) -> None:
        if total == 0:
            return
        if ok == 0:
            self._failed_cycles += 1
            if self._failed_cycles >= FAILURE_ALERT_AFTER and not self._alert_sent:
                self._alert_sent = True
                await self._notify_admin(
                    f"⚠️ hoe.com.ua: {self._failed_cycles} цикли поспіль жодна адреса не оновилась "
                    "(сайт недоступний або змінилась верстка). Перевір логи.")
        else:
            if self._alert_sent:
                await self._notify_admin("✅ hoe.com.ua знову працює, дані оновлюються.")
            self._failed_cycles = 0
            self._alert_sent = False

    async def _notify_admin(self, text: str) -> None:
        log.warning(text)
        if self.admin_notify:
            try:
                await self.admin_notify(text)
            except Exception:
                log.exception("Не вдалось сповістити адміна")

    async def run_forever(self) -> None:
        log.info("Моніторинг запущено, інтервал %d с", self.poll_interval)
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Збій циклу опитування")
            await asyncio.sleep(self.poll_interval + random.uniform(0, min(30, self.poll_interval / 10)))
