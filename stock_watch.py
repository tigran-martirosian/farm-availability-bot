"""Stock change detection and per-chat watch alerts. Pure logic plus small JSON storage."""

import json
import os
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from shop_notifier import escape_md

STATE_FILE = Path(os.getenv("STATE_FILE", "stock_state.json"))
WATCHES_FILE = Path(os.getenv("WATCHES_FILE", "watches.json"))

IN_STOCK, OUT_OF_STOCK, REMOVED = "in_stock", "out_of_stock", "removed"

class Change(NamedTuple):
    key: str
    name: str
    kind: str    # "new", "back_in_stock", "out_of_stock" or "removed"
    state: str   # stock state after the change


# ---------- storage ----------
def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
    return default

def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

def load_state(path: Path = None) -> Optional[dict]:
    """Return {"products": ..., "alerted": ...}, or None if no state was saved yet."""
    return load_json(path or STATE_FILE, None)

def save_state(state: dict, path: Path = None) -> None:
    save_json(path or STATE_FILE, state)

def load_watches(path: Path = None) -> Dict[str, List[str]]:
    return load_json(path or WATCHES_FILE, {})

def save_watches(watches: Dict[str, List[str]], path: Path = None) -> None:
    save_json(path or WATCHES_FILE, watches)


# ---------- change detection ----------
def product_key(name: str) -> str:
    return name.strip().lower()

def snapshot(in_stock: list, out_stock: list) -> Dict[str, dict]:
    """Catalog lists from parse_catalog -> {key: {"name", "in_stock"}}."""
    cur = {}
    for p in out_stock:
        cur[product_key(p["name"])] = {"name": p["name"], "in_stock": False}
    for p in in_stock:
        cur[product_key(p["name"])] = {"name": p["name"], "in_stock": True}
    return cur

def detect_changes(prev: Optional[Dict[str, dict]], cur: Dict[str, dict]) -> List[Change]:
    """Compare the previous and current snapshots. No previous snapshot means no changes (baseline)."""
    if prev is None:
        return []
    changes = []
    for key, item in cur.items():
        state = IN_STOCK if item["in_stock"] else OUT_OF_STOCK
        if key not in prev:
            changes.append(Change(key, item["name"], "new", state))
        elif prev[key]["in_stock"] != item["in_stock"]:
            kind = "back_in_stock" if item["in_stock"] else "out_of_stock"
            changes.append(Change(key, item["name"], kind, state))
    for key, item in prev.items():
        if key not in cur:
            changes.append(Change(key, item["name"], "removed", REMOVED))
    return changes


# ---------- watches and alerts ----------
def matches_watch(name: str, term: str) -> bool:
    return term.strip().lower() in name.lower()

def select_alerts(changes: List[Change], watches: Dict[str, List[str]],
                  alerted: Dict[str, Dict[str, str]]) -> Dict[str, List[Change]]:
    """Per chat: changes matching a watch term, unless that chat was already alerted about this state."""
    out = {}
    for chat_id, terms in watches.items():
        for c in changes:
            if alerted.get(chat_id, {}).get(c.key) == c.state:
                continue
            if any(matches_watch(c.name, t) for t in terms):
                out.setdefault(chat_id, []).append(c)
    return out

def process_poll(state: Optional[dict], cur: Dict[str, dict],
                 watches: Dict[str, List[str]]):
    """Return (new_state, alerts) for one catalog read. Persisting the new state is up to the caller."""
    if not cur:
        return state, {}
    prev = state["products"] if state else None
    alerted = {chat: dict(m) for chat, m in (state or {}).get("alerted", {}).items()}
    alerts = select_alerts(detect_changes(prev, cur), watches, alerted)
    for chat_id, items in alerts.items():
        for c in items:
            alerted.setdefault(chat_id, {})[c.key] = c.state
    return {"products": cur, "alerted": alerted}, alerts

_KIND_TEXT = {
    "new": "new in the catalog",
    "back_in_stock": "back in stock",
    "out_of_stock": "went out of stock",
    "removed": "removed from the catalog",
}

def build_alert(change: Change, shop_name: str) -> str:
    return f"🔔 *{escape_md(shop_name)}*: {escape_md(change.name)} is {_KIND_TEXT[change.kind]}."
