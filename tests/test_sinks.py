import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramMigrateToChat
from aiogram.methods import SendMessage

from hoe_notifier.sinks import TargetGone, TargetMigrated, TelegramSink, fit_body


class FakeBot:
    def __init__(self, error=None):
        self.sent, self.error = [], error

    async def send_message(self, **kwargs):
        if self.error:
            raise self.error
        self.sent.append(kwargs)


def test_fit_body_short_text_untouched():
    assert fit_body("abc", 100) == "abc"


def test_fit_body_truncates_with_ellipsis():
    out = fit_body("рядок\n" * 1000, 500)
    assert len(out) <= 500 and out.endswith("…")


async def test_long_body_fits_telegram_limit_after_escaping():
    bot = FakeBot()
    body = "<b>&</b> текст\n" * 2000                # екранування роздуває довжину
    await TelegramSink(bot).send_text("1", "Заголовок", "Дім", body, silent=True)
    text = bot.sent[0]["text"]
    assert len(text) <= 4096
    assert text.endswith("…</pre>")
    assert "<b>&</b>" not in text                      # користувацький HTML екрановано
    assert bot.sent[0]["disable_notification"] is True


async def test_short_message_format():
    bot = FakeBot()
    await TelegramSink(bot).send_text("-100", "Змінились", "Дім", "Черга: 1.1", silent=False)
    assert bot.sent[0]["text"] == "<b>Змінились</b>\n📍 Дім\n\n<pre>Черга: 1.1</pre>"
    assert bot.sent[0]["chat_id"] == -100


async def test_migrate_error_becomes_target_migrated():
    err = TelegramMigrateToChat(method=SendMessage(chat_id=1, text="x"), message="upgraded", migrate_to_chat_id=-1009)
    with pytest.raises(TargetMigrated) as info:
        await TelegramSink(FakeBot(err)).send_text("-100", "s", "l", "b", silent=True)
    assert info.value.new_target == "-1009"


async def test_forbidden_error_becomes_target_gone():
    err = TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="Forbidden: bot was blocked")
    with pytest.raises(TargetGone):
        await TelegramSink(FakeBot(err)).send_text("1", "s", "l", "b", silent=True)