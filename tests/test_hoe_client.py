import httpx
import pytest
from hoe_notifier.hoe import HoeClient, HoeUnavailable, Option, SearchUnavailable


def make(handler, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HoeClient("https://hoe.test", retries=3, retry_delay=0, client=client, **kw)


async def test_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        assert request.headers["x-requested-with"] == "XMLHttpRequest"
        assert b"streetId=1" in request.content and b"house=2" in request.content
        return httpx.Response(200, text="<table></table>")

    c = make(handler)
    assert "table" in await c.fetch_events_html("1", "2")
    assert calls["n"] == 3


async def test_gives_up_after_retries():
    c = make(lambda r: httpx.Response(200, text="   "))
    with pytest.raises(HoeUnavailable):
        await c.fetch_events_html("1", "2")


async def test_fetch_graphs_image_only():
    html = '<img alt="Графік" src="/a.png"><img alt="ГПВ на сьогодні" src="/Content/gpv.png">'
    g = await make(lambda r: httpx.Response(200, text=html)).fetch_graphs()
    assert g.image_url == "https://hoe.test/Content/gpv.png" and g.text is None


async def test_fetch_graphs_text_only_from_real_page():
    from pathlib import Path
    html = (Path(__file__).parent / "fixtures" / "graph_text_page.html").read_text(encoding="utf-8")
    g = await make(lambda r: httpx.Response(200, text=html)).fetch_graphs()
    assert g.image_url is None
    assert g.text.startswith("За розпорядженням НЕК") and "Підчерга 6.2 – 08:00-13:00; 17:00-22:00." in g.text


async def test_fetch_graphs_none_on_error():
    assert await make(lambda r: httpx.Response(500)).fetch_graphs() is None


async def test_fetch_graphs_requests_the_graph_page():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, text="<html></html>")

    g = await make(handler).fetch_graphs()
    assert seen["path"] == "/page/pogodinni-vidkljuchennja"
    assert g.image_url is None and g.text is None


async def test_ajax_headers_like_the_site():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, text="<table></table>")

    await make(handler).fetch_events_html("1", "2")
    assert seen["referer"] == "https://hoe.test/shutdown/new"
    assert seen["x-requested-with"] == "XMLHttpRequest"


async def test_search_settlements_request_shape():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["referer"] = request.headers["referer"]
        return httpx.Response(200, json=[{"id": 11, "text": "Давидківці"}])

    res = await make(handler).search_settlements("Давид")
    assert res == [Option("11", "Давидківці")]
    assert seen["path"] == "/settlements/"
    assert seen["params"] == {"term": "Давид", "_type": "query", "q": "Давид"}
    assert seen["referer"].endswith("/shutdown/new")


async def test_search_streets_uses_settlement_id_in_path():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["term"] = request.url.params["term"]
        return httpx.Response(200, json=[{"id": 77, "text": "Подільська"}])

    res = await make(handler).search_streets("11", "Поділ")
    assert res == [Option("77", "Подільська")]
    assert seen == {"path": "/streets/11", "term": "Поділ"}


async def test_get_houses():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json=[1, 2, "3А"])

    assert await make(handler).get_houses("77") == ["1", "2", "3А"]
    assert seen["path"] == "/houses/77"


async def test_search_retries_once_then_raises():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500)

    with pytest.raises(SearchUnavailable):
        await make(handler).search_settlements("x")
    assert calls["n"] == 2


async def test_search_raises_on_non_json():
    c = make(lambda r: httpx.Response(200, text="<html>не json</html>"))
    with pytest.raises(SearchUnavailable):
        await c.get_houses("1")