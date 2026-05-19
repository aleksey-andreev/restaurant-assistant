from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple

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


def _iter_menu_items_raw(menu_tree: Dict[str, Any]) -> Iterator[Tuple[Dict[str, Any], str, str]]:
    """Yield (menu_item dict, card_name, path) for every sellable row."""
    for card in menu_tree.get("items") or []:
        if not isinstance(card, dict):
            continue
        card_name = str(card.get("name") or "").strip()
        for root in card.get("tree") or []:
            if isinstance(root, dict):
                yield from _walk_menu_items_raw(root, card_name, "")


def _walk_menu_items_raw(
    node: Dict[str, Any],
    card_name: str,
    path: str,
) -> Iterator[Tuple[Dict[str, Any], str, str]]:
    name = str(node.get("name") or "").strip()
    seg = f"{path} / {name}".strip(" /") if path else name
    for item in node.get("items") or []:
        if not isinstance(item, dict):
            continue
        mid = item.get("menu_item_id")
        if mid is None or str(mid).strip() == "":
            continue
        yield item, card_name, seg or card_name
    for ch in node.get("children") or []:
        if isinstance(ch, dict):
            yield from _walk_menu_items_raw(ch, card_name, seg or path)


def _slim_nutrition(*sources: Any) -> Optional[Dict[str, Any]]:
    """KBJU from cpfc object and/or flat Calories/Protein/Fat/Hydrocarbons on item/product."""
    aliases = (
        ("calories", ("calories", "Calories")),
        ("proteins", ("proteins", "Protein", "protein")),
        ("fats", ("fats", "Fat", "fat")),
        ("carbohydrates", ("carbohydrates", "Hydrocarbons", "hydrocarbons", "carbohydrate")),
    )
    out: Dict[str, Any] = {}
    for src in sources:
        if not isinstance(src, dict):
            continue
        cpfc = src.get("cpfc")
        if isinstance(cpfc, dict):
            src = {**src, **cpfc}
        for out_key, keys in aliases:
            if out_key in out:
                continue
            for k in keys:
                if k not in src or src[k] is None:
                    continue
                try:
                    out[out_key] = float(src[k])
                except (TypeError, ValueError):
                    out[out_key] = src[k]
                break
    return out or None


def _slim_ingredient_names(raw: Any) -> Optional[List[str]]:
    if not isinstance(raw, list):
        return None
    names: List[str] = []
    for ing in raw:
        if not isinstance(ing, dict):
            continue
        ipc = ing.get("ingredient_product_class")
        if isinstance(ipc, dict):
            nm = str(ipc.get("name") or "").strip()
        else:
            nm = str(ing.get("name") or "").strip()
        if nm and nm not in names:
            names.append(nm)
    return names or None


def _portion_block(item: Dict[str, Any], product: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    block: Dict[str, Any] = {}
    for src in (item, product):
        for key in ("output", "output_measure", "netto", "measure_name"):
            val = src.get(key)
            if val is None or val == "" or val == 0 or val == 0.0:
                continue
            out_key = "measure" if key == "measure_name" else key
            if out_key not in block:
                block[out_key] = val
    return block or None


def slim_menu_item_for_llm(
    item: Dict[str, Any],
    *,
    section: str,
    path: str,
) -> Dict[str, Any]:
    """Compact menu row for LLM: criteria-relevant fields only (no colors, ids, images)."""
    product = item.get("product") if isinstance(item.get("product"), dict) else {}
    mid = str(item.get("menu_item_id") or "").strip()
    title = str(item.get("title") or product.get("name") or "").strip() or "—"
    try:
        price = float(item.get("price") if item.get("price") is not None else product.get("price") or 0.0)
    except (TypeError, ValueError):
        price = 0.0

    row: Dict[str, Any] = {
        "id": mid,
        "title": title,
        "price": price,
        "section": section,
    }
    if path and path != section:
        row["path"] = path

    desc = product.get("description") or item.get("description")
    if isinstance(desc, str) and desc.strip():
        row["description"] = desc.strip()[:2000]

    cat = product.get("category_name") or item.get("category")
    if isinstance(cat, str) and cat.strip():
        row["category"] = cat.strip()

    nutrition = _slim_nutrition(item, product)
    if nutrition:
        row["nutrition"] = nutrition

    portion = _portion_block(item, product)
    if portion:
        row["portion"] = portion

    ingredients = _slim_ingredient_names(product.get("ingredients"))
    if ingredients:
        row["ingredients"] = ingredients

    if product.get("is_age_limited") is True or item.get("is_age_limited") is True:
        row["age_limited"] = True
    if product.get("is_excisable") is True:
        row["excisable"] = True
    try:
        abv = product.get("alcohol_by_volume")
        if abv is not None and float(abv) > 0:
            row["alcohol_by_volume"] = float(abv)
    except (TypeError, ValueError):
        pass
    if product.get("need_to_weigh") is True:
        row["sold_by_weight"] = True

    return row


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


def compact_menu_for_llm(menu_tree: Dict[str, Any]) -> List[Dict[str, Any]]:
    """All menu positions, slimmed for LLM (nutrition, portion, ingredients; no UI noise)."""
    return [
        slim_menu_item_for_llm(item, section=card_name, path=seg or card_name)
        for item, card_name, seg in _iter_menu_items_raw(menu_tree)
    ]


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


def preorder_menu_pick_response_format(*, strict: bool = True) -> Dict[str, Any]:
    """OpenAI-compatible structured output for preorder LLM pick (GLM json_schema)."""
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "menu_item_id": {"type": "string"},
                        "quantity": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                        },
                    },
                    "required": ["menu_item_id", "quantity"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    json_schema: Dict[str, Any] = {
        "name": "preorder_menu_pick",
        "schema": schema,
    }
    if strict:
        json_schema["strict"] = True
    return {
        "response_format": {
            "type": "json_schema",
            "json_schema": json_schema,
        }
    }


def normalize_llm_json_text(raw: str) -> str:
    """Strip markdown fences and isolate a JSON object from model text."""
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```\s*$", "", s.strip())
    if s and s[0] not in "{[":
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            s = m.group(0)
    return s.strip()


def parse_llm_menu_pick_json(raw: str) -> List[Dict[str, Any]]:
    """Expect JSON object with key items: [{menu_item_id, quantity}, ...]."""
    s = normalize_llm_json_text(raw)
    if not s:
        return []
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
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
