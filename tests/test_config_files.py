from hoe_notifier.addresses_file import load_addresses
from hoe_notifier.config import normalize_bot_token


def test_token_legacy_prefix():
    assert normalize_bot_token("bot6191234558:AAG-x") == "6191234558:AAG-x"
    assert normalize_bot_token("6191234558:AAGbot-x") == "6191234558:AAGbot-x"
    assert normalize_bot_token("") is None


def test_addresses_file(tmp_path):
    f = tmp_path / "a.toml"
    f.write_text('''
[[address]]
name = "Дім"
street_id = 123456
house = "12"
target = -1001553812345
graphs = true

[[address]]
street_id = "1"          # немає house — запис пропускається
target = "5"

[[address]]
street_id = "2"
house = "3"
send_to = "slack"
target = "C02"
''', encoding="utf-8")
    e = load_addresses(f, available_channels={"tg"})
    assert len(e) == 1
    assert e[0]["street_id"] == "123456" and e[0]["target"] == "-1001553812345"
    assert e[0]["graphs"] is True and e[0]["silent"] is True
    assert len(load_addresses(f, available_channels={"tg", "slack"})) == 2


def test_missing_file(tmp_path):
    assert load_addresses(tmp_path / "nope.toml") == []
