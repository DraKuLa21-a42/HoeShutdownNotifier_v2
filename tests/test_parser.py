from hoe_notifier.hoe import (
    Option, ParseStatus, house_key, match_house, parse_events, parse_houses, parse_options, suggest_houses,
)

FULL = """
<table><thead><tr><th>Вид робіт</th><th>Черга (ГПВ)</th><th>Початок</th></tr></thead>
<tbody>
<tr><td>Графік погодинних відключень</td><td>2.2</td><td>26.11.2025 14:00</td></tr>
<tr><td>Аварійне</td><td>-</td><td>26.11.2025 18:00</td></tr>
</tbody></table>
"""


def test_full_table():
    p = parse_events(FULL)
    assert p.status is ParseStatus.OUTAGES
    assert p.text.splitlines()[0] == "Вид робіт: Графік погодинних відключень"
    assert "Черга (ГПВ): 2.2" in p.text
    assert "\n\n" in p.text  # рядки розділені порожнім рядком


def test_no_thead_no_tbody_does_not_crash():
    html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    p = parse_events(html)
    assert p.status is ParseStatus.OUTAGES
    assert p.text == "A: 1\nB: 2"


def test_mismatched_cells_not_lost():
    html = "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>x</td></tr></tbody></table>"
    p = parse_events(html)
    assert p.status is ParseStatus.OUTAGES and p.text == "x"


def test_no_outages_message():
    p = parse_events("<div>Відключень за вашою адресою не заплановано</div>")
    assert p.status is ParseStatus.NONE


def test_unknown_layout():
    assert parse_events("<html><body>Internal error</body></html>").status is ParseStatus.UNKNOWN


def test_prefers_table_shutdowns_class():
    html = ("<table class='other'><tr><td>сміття</td></tr></table>"
            "<table class='table table-shutdowns'><thead><tr><th>Вид</th></tr></thead>"
            "<tbody><tr><td>Планове</td></tr></tbody></table>")
    assert parse_events(html).text == "Вид: Планове"


def test_real_six_column_layout():
    html = """<table class="table table-shutdowns"><thead><tr>
      <th>Вид робіт</th><th>Тип відключення</th><th>Черга ГПВ</th><th>Черга ГАВ</th><th>Початок</th><th>Кінець</th>
    </tr></thead><tbody><tr><td>Планові</td><td>Відключення</td><td>2.1</td><td>-</td>
      <td>26.11.2025 14:00</td><td>26.11.2025 18:00</td></tr></tbody></table>"""
    p = parse_events(html)
    assert p.status is ParseStatus.OUTAGES
    assert "Тип відключення: Відключення" in p.text and "Кінець: 26.11.2025 18:00" in p.text


def test_parse_options_select2():
    assert parse_options([{"id": 5, "text": "Давидківці"}]) == [Option("5", "Давидківці")]
    assert parse_options({"results": [{"id": "7", "text": " вул. Подільська "}]}) == [Option("7", "вул. Подільська")]
    assert parse_options({"unexpected": 1}) == []
    assert parse_options([{"id": 1}, {"text": "без id"}, "рядок", {"id": "", "text": "x"}]) == []


def test_parse_houses_variants():
    assert parse_houses([1, "2", " 3А ", "2", None, ""]) == ["1", "2", "3А"]
    assert parse_houses({"results": [{"text": "5"}, {"id": 6}]}) == ["5", "6"]
    assert parse_houses("помилка") == []


HOUSES = ["1", "12А", "12/1", "13", "120", "5Б"]


def test_house_match_is_case_and_alphabet_insensitive():
    assert match_house(HOUSES, "12A") == "12А"     # латинська A → кирилична А
    assert match_house(HOUSES, "12 а") == "12А"
    assert match_house(HOUSES, "5б") == "5Б"
    assert match_house(HOUSES, "13") == "13"
    assert match_house(HOUSES, "99") is None
    assert house_key("12B") == house_key("12В")


def test_house_suggestions():
    assert suggest_houses(HOUSES, "12") == ["12А", "12/1", "120"]       # за префіксом
    assert suggest_houses(HOUSES, "12В") == ["12А", "12/1"]               # те саме число, інша літера
    assert suggest_houses(HOUSES, "7") == []
    assert suggest_houses(HOUSES, "") == []
    assert len(suggest_houses([str(i) for i in range(100, 200)], "1", limit=8)) == 8