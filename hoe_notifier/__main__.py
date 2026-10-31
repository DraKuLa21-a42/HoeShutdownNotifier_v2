"""Запуск: `python -m hoe_notifier` (сервіс) або `python -m hoe_notifier import-legacy home`."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from .addresses_file import load_addresses
from .config import ConfigError, load_settings
from .db import Database
from .hoe import HoeClient
from .monitor import Monitor
from .sinks import SlackSink, TelegramSink

log = logging.getLogger("hoe_notifier")


def import_legacy(path: str) -> int:
    """Друкує фрагменти нових конфігів на основі старого файлу `home` (нічого не записує)."""
    from dotenv import dotenv_values

    old = dotenv_values(path)
    send_to = (old.get("SEND_TO") or "TG").upper()
    channel = "slack" if send_to == "SLACK" else "tg"
    target = (old.get("SLACK_CHANNEL") or old.get("SLACK_CNANNEL")) if channel == "slack" else old.get("TG_CHAT_ID")
    print("# --- додай у .env ---")
    if old.get("TG_BOT_ID"):
        print(f"BOT_TOKEN={old['TG_BOT_ID']}")
    if old.get("SLACK_TOKEN"):
        print(f"SLACK_TOKEN={old['SLACK_TOKEN']}")
    print(f"DEFAULT_SILENT={(old.get('TG_DIS_NOTIFY') or 'true').lower()}")
    print("\n# --- додай в addresses.toml ---")
    print("[[address]]")
    print('name = "Моя адреса"')
    print(f'street_id = "{old.get("STREET_ID", "")}"')
    print(f'house = "{old.get("HOUSE", "")}"')
    print(f'send_to = "{channel}"')
    print(f'target = "{target or ""}"')
    print(f'graphs = {str((old.get("SEND_GRAPHS") or "no").lower() == "yes").lower()}')
    print(f'events = {str((old.get("SEND_SHUTDOWN_EVENTS") or "yes").lower() == "yes").lower()}')
    return 0


async def run() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Помилка конфігурації: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db = Database(settings.db_path)
    hoe = HoeClient(
        settings.hoe_base_url, timeout=settings.request_timeout, retries=settings.retries,
        retry_delay=settings.retry_delay,
    )

    bot = dp = None
    sinks: dict = {}
    if settings.bot_token:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode

        bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        sinks["tg"] = TelegramSink(bot)
    if settings.slack_token:
        sinks["slack"] = SlackSink(settings.slack_token)

    async def admin_notify(text: str) -> None:
        if "tg" not in sinks:
            return
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, text, parse_mode=None)
            except Exception:
                log.exception("Не вдалось надіслати повідомлення адміну %s", admin_id)

    entries = load_addresses(
        settings.addresses_file, default_silent=settings.default_silent,
        default_graphs=settings.default_graphs, available_channels=set(sinks),
    )
    db.sync_config_subscriptions(entries)
    log.info("Адрес з addresses.toml: %d; усього підписок: %s", len(entries), db.stats())

    monitor = Monitor(db, hoe, sinks, poll_interval=settings.poll_interval,
                      concurrency=settings.concurrency, admin_notify=admin_notify)
    monitor_task = asyncio.create_task(monitor.run_forever(), name="monitor")

    try:
        if bot:
            from .bot import build_dispatcher, setup_bot_commands

            dp = build_dispatcher(settings, db, hoe, monitor)
            try:
                await setup_bot_commands(bot)
            except Exception as exc:  # меню команд не критичне; polling сам перепідключається
                log.warning("Не вдалось встановити меню команд (не критично): %s", exc)
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        else:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, monitor_task.cancel)
            await monitor_task
    except asyncio.CancelledError:
        pass
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        if bot:
            await bot.session.close()
        await hoe.aclose()
        db.close()
        log.info("Зупинено")
    return 0


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "import-legacy":
        sys.exit(import_legacy(sys.argv[2]))
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()