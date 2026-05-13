from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Optional


def menu_tree_has_positions(menu_tree: Dict[str, Any]) -> bool:
    """True if Toka menus/tree response has at least one sellable row with menu_item_id."""
    for row in iter_menu_positions(menu_tree):
        if row.get("menu_item_id"):
            return True
    return False


def iter_menu_positions(menu_tree: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for card in menu_tree.get("items") or []:
        if not isinstance(card, dict):
            continue
        card_name = str(card.get("name") or "").strip()
        for root in card.get("tree") or []:
            if isinstance(root, dict):
                _walk_menu_node(root, card_name, "", out)
    return out


def _walk_menu_node(
    node: Dict[str, Any],
    card_name: str,
    path: str,
    out: List[Dict[str, Any]],
) -> None:
    name = str(node.get("name") or "").strip()
    seg = f"{path} / {name}".strip(" /") if path else name
    for item in node.get("items") or []:
        if not isinstance(item, dict):
            continue
        mid = item.get("menu_item_id")
        if mid is None or str(mid).strip() == "":
            continue
        title = str(item.get("title") or item.get("product", {}).get("name") or "").strip()
        price = item.get("price")
        try:
            price_f = float(price) if price is not None else 0.0
        except (TypeError, ValueError):
            price_f = 0.0
        out.append(
            {
                "menu_item_id": str(mid).strip(),
                "title": title or "—",
                "price": price_f,
                "section": card_name,
                "path": seg or card_name,
            }
        )
    for ch in node.get("children") or []:
        if isinstance(ch, dict):
            _walk_menu_node(ch, card_name, seg or path, out)


def compact_menu_for_llm(menu_tree: Dict[str, Any], *, limit: int = 350) -> List[Dict[str, Any]]:
    rows = iter_menu_positions(menu_tree)
    slim: List[Dict[str, Any]] = []
    for r in rows[:limit]:
        slim.append(
            {
                "id": r["menu_item_id"],
                "title": r["title"],
                "price": r["price"],
                "section": r.get("section") or "",
            }
        )
    return slim


def lines_from_menu_item_ids(
    menu_tree: Dict[str, Any],
    picks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Enrich LLM picks {menu_item_id, quantity} with title/price from tree."""
    by_id = {r["menu_item_id"]: r for r in iter_menu_positions(menu_tree)}
    out: List[Dict[str, Any]] = []
    for p in picks:
        if not isinstance(p, dict):
            continue
        mid = str(p.get("menu_item_id") or "").strip()
        if not mid:
            continue
        try:
            q = int(p.get("quantity") or 1)
        except (TypeError, ValueError):
            q = 1
        q = max(1, min(99, q))
        base = by_id.get(mid, {})
        title = str(base.get("title") or "—")
        try:
            unit = float(base.get("price") or 0.0)
        except (TypeError, ValueError):
            unit = 0.0
        out.append(
            {
                "menu_item_id": str(mid).strip(),
                "quantity": q,
                "title": title,
                "price": unit,
                "line_total": round(unit * q, 2),
                "section": str(base.get("section") or ""),
            }
        )
    return out


def preorder_cart_total(lines: List[Dict[str, Any]]) -> float:
    t = 0.0
    for ln in lines:
        try:
            t += float(ln.get("line_total") or 0.0)
        except (TypeError, ValueError):
            pass
    return round(t, 2)


def format_preorder_summary_ru(lines: List[Dict[str, Any]]) -> str:
    if not lines:
        return "Корзина пуста."
    parts: List[str] = ["Состав предзаказа:"]
    by_sec: Dict[str, List[Dict[str, Any]]] = {}
    for ln in lines:
        sec = str(ln.get("section") or "Блюда")
        by_sec.setdefault(sec, []).append(ln)
    for sec, lns in by_sec.items():
        parts.append(f"\n{sec}")
        for ln in lns:
            title = str(ln.get("title") or "—")
            q = int(ln.get("quantity") or 1)
            lt = ln.get("line_total")
            try:
                lt_f = float(lt) if lt is not None else float(ln.get("price") or 0) * q
            except (TypeError, ValueError):
                lt_f = 0.0
            parts.append(f"- {title} × {q} — {lt_f:.0f} ₽")
    parts.append(f"\nИтого: {preorder_cart_total(lines):.0f} ₽")
    return "\n".join(parts)


def build_toka_order_payload(
    *,
    client_name: str,
    client_phone: str,
    guest_count: int,
    table_id: str,
    menu_lines: List[Dict[str, Any]],
) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for ln in menu_lines:
        mid = str(ln.get("menu_item_id") or "").strip()
        if not mid:
            continue
        try:
            q = int(ln.get("quantity") or 1)
        except (TypeError, ValueError):
            q = 1
        q = max(1, min(99, q))
        items.append(
            {
                "menu_item_id": mid,
                "quantity": q,
                "applied_modifiers": [],
                "selected_combo_items": [],
            }
        )
    gc = max(1, min(1000, int(guest_count) if guest_count else 1))
    tid = (table_id or "").strip() or None
    return {
        "client_phone_number": (client_phone or "").strip() or None,
        "client_name": (client_name or "").strip() or None,
        "menu_items": items,
        "table_id": tid,
        "service_type": "on_site",
        "guest_count": gc,
        "pos_order_id": str(uuid.uuid4()),
    }


_AFFIRM_RE = re.compile(
    r"^(да|давай|окей|ок|угу|ага|конечно|хорошо|согласен|согласна|подтверждаю|yes)\b",
    re.IGNORECASE,
)


def is_short_affirmative_reply(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t or len(t) > 48:
        return False
    if _AFFIRM_RE.match(t):
        return True
    if t in {"+", "👍", "ок.", "ок!"}:
        return True
    return False


def is_short_decline_reply(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t or len(t) > 48:
        return False
    return bool(re.match(r"^(нет|неа|не надо|не хочу|потом|откажусь)\b", t, re.IGNORECASE))


def wants_open_menu_phrase(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    keys = (
        "открыть меню",
        "открой меню",
        "выберу сам",
        "выберу сама",
        "сам выберу",
        "сама выберу",
        "из меню",
        "вручную",
        "самостоятельно",
        "ручной выбор",
    )
    return any(k in t for k in keys)


def wants_llm_pick_phrase(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(
        k in t
        for k in (
            "подбери",
            "подберите",
            "автоматически",
            "предложи",
            "предложите",
            "по предпочтен",
            "за меня",
        )
    )


def parse_llm_menu_pick_json(raw: str) -> List[Dict[str, Any]]:
    """Expect JSON object with key items: [{menu_item_id, quantity}, ...]."""
    s = (raw or "").strip()
    if not s:
        return []
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}\s*$", s)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(data, list):
        arr = data
    elif isinstance(data, dict):
        arr = data.get("items") or data.get("menu_items") or data.get("picks") or []
    else:
        return []
    out: List[Dict[str, Any]] = []
    for x in arr:
        if not isinstance(x, dict):
            continue
        mid = str(x.get("menu_item_id") or x.get("id") or "").strip()
        if not mid:
            continue
        try:
            q = int(x.get("quantity") or 1)
        except (TypeError, ValueError):
            q = 1
        out.append({"menu_item_id": mid, "quantity": max(1, min(99, q))})
    return out
