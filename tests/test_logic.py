"""Offline tests: catalog parsing and schedule storage. No network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime

from bs4 import BeautifulSoup

import bot_polling
from shop_notifier import build_message, card_has_buy_button, card_is_oos, parse_catalog

HTML = """
<ul class="products">
  <li class="product"><h2 class="woocommerce-loop-product__title">Product A</h2>
    <a class="woocommerce-LoopProduct-link" href="/p/a"></a>
    <a class="button add_to_cart_button">Add to cart</a></li>
  <li class="product outofstock"><h2 class="woocommerce-loop-product__title">Product B</h2>
    <a href="/p/b"></a><span>Out of stock</span></li>
  <li class="product"><h2 class="woocommerce-loop-product__title">Product A</h2>
    <a class="button add_to_cart_button">Add to cart</a></li>
</ul>
"""

def test_parse_catalog_splits_stock_and_dedupes():
    in_stock, out_stock = parse_catalog(HTML, "https://example-farm.test/shop/")
    assert [p["name"] for p in in_stock] == ["Product A"]
    assert in_stock[0]["url"] == "https://example-farm.test/p/a"
    assert [p["name"] for p in out_stock] == ["Product B"]

def test_parse_hhmm():
    assert bot_polling.parse_hhmm("8:30") == (8, 30)
    assert bot_polling.parse_hhmm(" 23:59 ") == (23, 59)
    assert bot_polling.parse_hhmm("24:00") is None
    assert bot_polling.parse_hhmm("abc") is None

def test_schedule_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_polling, "DATA_FILE", tmp_path / "s.json")
    assert bot_polling.load_schedules() == {}
    bot_polling.save_schedules({"123": "08:30"})
    assert bot_polling.load_schedules() == {"123": "08:30"}

def test_scheduled_time_has_local_tzinfo():
    t = bot_polling.scheduled_time(8, 30)
    assert (t.hour, t.minute) == (8, 30)
    assert t.tzinfo is not None
    assert t.utcoffset() == datetime.now().astimezone().utcoffset()

def test_build_message_escapes_markdown_in_names():
    items = [{"name": "Grass_fed *beef* [pack]", "url": "https://example-farm.test/p/x"}]
    msg = build_message(items, [{"name": "Snake_case", "url": ""}], "https://example-farm.test/shop/")
    assert r"Grass\_fed \*beef\* \[pack]" in msg
    assert r"Snake\_case" in msg

def _card(html):
    return BeautifulSoup(html, "html.parser").select_one("li")

def test_buy_button_enabled_and_disabled():
    assert card_has_buy_button(_card('<li><a class="add_to_cart_button">Add</a></li>'))
    assert not card_has_buy_button(_card('<li><a class="add_to_cart_button disabled">Add</a></li>'))
    assert not card_has_buy_button(_card('<li><button class="add_to_cart_button" disabled>Add</button></li>'))
    assert not card_has_buy_button(_card('<li><a class="add_to_cart_button" aria-disabled="true">Add</a></li>'))
    assert card_has_buy_button(_card('<li><a class="button">BUY NOW</a></li>'))
    assert not card_has_buy_button(_card('<li><a class="button" aria-disabled="true">Buy now</a></li>'))
    assert not card_has_buy_button(_card('<li><a href="/p">Title</a></li>'))

def test_out_of_stock_marker_wins_over_buy_button():
    html = '<li class="product"><h2>Item</h2><span class="outofstock"></span><a class="add_to_cart_button">Add</a></li>'
    in_stock, out_stock = parse_catalog(f"<ul>{html}</ul>", "https://example-farm.test/shop/")
    assert in_stock == [] and [p["name"] for p in out_stock] == ["Item"]
    assert card_is_oos(_card('<li>Out of stock</li>'))


# ---------- change detection and watch alerts ----------
import stock_watch as sw

def _cat(**kw):
    """_cat(a=True, b=False) -> snapshot with a in stock, b out of stock."""
    return {k: {"name": k.title(), "in_stock": v} for k, v in kw.items()}

def _kinds(prev, cur):
    return {(c.key, c.kind) for c in sw.detect_changes(prev, cur)}

def test_detect_changes_each_type():
    prev = _cat(a=False, b=True, c=True)
    cur = _cat(a=True, b=False, d=True)
    assert _kinds(prev, cur) == {
        ("a", "back_in_stock"), ("b", "out_of_stock"), ("c", "removed"), ("d", "new"),
    }

def test_detect_changes_none_and_baseline():
    cat = _cat(a=True, b=False)
    assert sw.detect_changes(cat, cat) == []
    assert sw.detect_changes(None, cat) == []

def test_watch_matching_is_case_insensitive_partial():
    assert sw.matches_watch("Grass Fed Beef Mince", "beef")
    assert sw.matches_watch("Grass Fed Beef Mince", "  FED beef ")
    assert not sw.matches_watch("Raw Honey", "beef")

def test_duplicate_alert_suppressed_across_polls_and_restart(tmp_path):
    path = tmp_path / "state.json"
    watches = {"1": ["honey"], "2": ["honey"], "3": ["eggs"]}
    state, alerts = sw.process_poll(None, _cat(honey=True, eggs=True), watches)
    assert alerts == {}  # first read is only the baseline
    sw.save_state(state, path)

    state, alerts = sw.process_poll(sw.load_state(path), _cat(honey=False, eggs=True), watches)
    assert sorted(alerts) == ["1", "2"]  # only chats watching honey
    assert alerts["1"][0].kind == "out_of_stock"
    sw.save_state(state, path)

    # next poll, state unchanged: no repeat alert
    state, alerts = sw.process_poll(sw.load_state(path), _cat(honey=False, eggs=True), watches)
    assert alerts == {}

    # simulated restart: everything is reloaded from the file, still no repeat
    state, alerts = sw.process_poll(sw.load_state(path), _cat(honey=False, eggs=True), watches)
    assert alerts == {}

    # state changes again: alerts resume
    state, alerts = sw.process_poll(state, _cat(honey=True, eggs=True), watches)
    assert alerts["1"][0].kind == "back_in_stock"

def test_alert_already_sent_for_same_state_is_not_repeated():
    # e.g. the state file was lost after the alert went out: same product and state, same chat
    prev = {"products": _cat(honey=True), "alerted": {"1": {"honey": "out_of_stock"}}}
    _, alerts = sw.process_poll(prev, _cat(honey=False), {"1": ["honey"]})
    assert alerts == {}

def test_watch_file_roundtrip(tmp_path):
    path = tmp_path / "w.json"
    assert sw.load_watches(path) == {}
    sw.save_watches({"1": ["beef"]}, path)
    assert sw.load_watches(path) == {"1": ["beef"]}

def test_empty_read_changes_nothing():
    state = {"products": _cat(honey=True), "alerted": {}}
    new_state, alerts = sw.process_poll(state, {}, {"1": ["honey"]})
    assert alerts == {}
    assert new_state == state
