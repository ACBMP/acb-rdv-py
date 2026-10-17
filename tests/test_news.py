"""In-game news: the UbiNews channel, headers and bodies over the wire, and the news sources."""

from __future__ import annotations

import datetime as dt

import httpx

from quazal.ddl import Reader, Writer, qdatetime_from

from acb_rdv.news import AnApiNews, FileNews, html_to_text
from acb_rdv.protocols.ubi import UBINEWS_CHANNEL_ID, NewsChannel, NewsHeader, NewsMessage

from .test_e2e import env  # noqa: F401  (fixture)

NEWS_TOML = '''
[[news]]
id = 7
date = 2026-10-04T12:30:00
sender = "AN"
subject = "Patch 2"
body = "Line one\\nLine two"
link = "https://example.org"

[[news]]
subject = "Welcome"
body = "Hello"
'''


async def test_news_flow_over_the_wire(env, tmp_path):
    server, storage, login, _ = env
    path = tmp_path / "news.toml"
    path.write_text(NEWS_TOML)
    server.dispatcher.protocols[0x1F].source = FileNews(path)
    alice = await login("Alice", "pw-a")

    reply = await alice.call(0x21, 1)
    assert reply.success
    channel = NewsChannel.read(Reader(reply.body))
    assert channel.id == UBINEWS_CHANNEL_ID and channel.class_name == "UbiNews"

    by_type = await alice.call(0x1F, 2, Writer().string_list(["UbiNews"]).u32(0).u32(10).bytes())
    assert Reader(by_type.body).list(NewsChannel.read)[0].id == UBINEWS_CHANNEL_ID
    maintenance = await alice.call(0x1F, 2, Writer().string_list(["Maintenance"]).u32(0).u32(1).bytes())
    assert Reader(maintenance.body).u32() == 0

    count = await alice.call(0x1F, 10, Writer().u32(channel.id).u32(0).bytes())
    assert Reader(count.body).u32() == 2

    reply = await alice.call(0x1F, 8, Writer().u32(channel.id).u32(0).u32(0).u32(10).bytes())
    r = Reader(reply.body)
    headers = r.list(NewsHeader.read)
    assert r.eof()
    assert [h.id for h in headers] == [7, 2]
    assert headers[0].subject == "Patch 2" and headers[0].sender_name == "AN"
    assert headers[0].link == "https://example.org"
    assert headers[0].publication_time == qdatetime_from(dt.datetime(2026, 10, 4, 12, 30))

    reply = await alice.call(0x1F, 9, Writer().u32_list([7, 99, 2]).bytes())
    r = Reader(reply.body)
    messages = r.list(NewsMessage.read)
    assert r.eof()
    assert [(m.id, m.body) for m in messages] == [(7, "Line one\nLine two"), (2, "Hello")]

    other = await alice.call(0x1F, 8, Writer().u32(5).u32(0).u32(0).u32(10).bytes())
    assert Reader(other.body).u32() == 0


async def test_no_news_when_disabled(env):
    server, storage, login, _ = env
    alice = await login("Alice", "pw-a")
    reply = await alice.call(0x1F, 8, Writer().u32(UBINEWS_CHANNEL_ID).u32(0).u32(0).u32(10).bytes())
    assert Reader(reply.body).u32() == 0


async def test_file_news_keeps_items_when_file_breaks(tmp_path):
    path = tmp_path / "news.toml"
    path.write_text(NEWS_TOML)
    source = FileNews(path)
    assert len(await source.items()) == 2
    path.write_text("[[news]]\nsubject = ")
    source._mtime = None
    assert len(await source.items()) == 2


async def test_anapi_news_flattens_html():
    posts = [{"_id": "66ff0a00aaaaaaaaaaaaaaaa", "title": "Season 3", "content": "<p>Hi &amp; welcome</p><ul><li>a</li><li>b</li></ul>"}]
    source = AnApiNews("http://an", sender="AN",
                       transport=httpx.MockTransport(lambda req: httpx.Response(200, json=posts)))
    [item] = await source.items()
    assert item.subject == "Season 3" and item.sender == "AN"
    assert item.body == "Hi & welcome\n\n- a\n- b"
    assert item.published == dt.datetime.fromtimestamp(0x66FF0A00, dt.timezone.utc)
    assert 0 < item.id < 2**31
    await source.close()


def test_html_to_text():
    assert html_to_text("plain &amp; simple") == "plain & simple"
    assert html_to_text("a<br>b") == "a\nb"
