from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


class ReviewAspectExtractionError(Exception):
    pass


def _extract_first_json_object(text: str) -> str:
    # best-effort: pick the first {...} block
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ReviewAspectExtractionError("No JSON object found in model output")
    return m.group(0)


def _scores_default() -> Dict[str, Any]:
    return {
        "food": {"score": 0.0, "evidence": None},
        "service": {"score": 0.0, "evidence": None},
        "ambience": {"score": 0.0, "evidence": None},
        "noise": {"score": 0.0, "evidence": None},
        "value": {"score": 0.0, "evidence": None},
    }


async def extract_aspects_from_reviews(
    llm_client: Any,
    node_params: Dict[str, Any],
    *,
    reviews: List[str],
    restaurant_name: Optional[str] = None,
    max_chars: int = 12000,
) -> Dict[str, Any]:
    """
    Ask LLM to convert review texts into structured aspect scores.

    Guardrails:
    - Output must be strict JSON.
    - Scores are 0..1.
    """
    safe_reviews = reviews[:12]
    joined = []
    total = 0
    for i, r in enumerate(safe_reviews, start=1):
        r2 = r.strip()
        if not r2:
            continue
        if total + len(r2) > max_chars:
            r2 = r2[: max(0, max_chars - total)]
        joined.append(f"ОТЗЫВ {i}:\n{r2}")
        total += len(r2)
        if total >= max_chars:
            break

    reviews_text = "\n\n".join(joined).strip()
    if not reviews_text:
        return {"aspects": _scores_default(), "evidence_count": 0}

    sys = (
        "Ты извлекаешь факты из отзывов посетителей ресторана. "
        "Нельзя придумывать то, чего нет в тексте. "
        "Отвечай ТОЛЬКО JSON без markdown."
    )
    user = {
        "restaurant_name": restaurant_name,
        "reviews_text": reviews_text,
        "task": (
            "Оцени по отзывам следующие аспекты по шкале 0..1: "
            "food (еда), service (сервис/персонал), ambience (атмосфера/уют), "
            "noise (тихо/шумно, где больше позитив — тем тише), "
            "value (ценность/соотношение цена-качество). "
            "Для каждого аспекта дай evidence: 1 короткую цитату или фрагмент текста из отзывов "
            "(может быть null если в тексте нет явных признаков). "
            "Добавь evidence_count — сколько аспектов имели evidence не null."
        ),
        "output_schema": {
            "aspects": {
                "food": {"score": 0.0, "evidence": None},
                "service": {"score": 0.0, "evidence": None},
                "ambience": {"score": 0.0, "evidence": None},
                "noise": {"score": 0.0, "evidence": None},
                "value": {"score": 0.0, "evidence": None},
            },
            "evidence_count": 0,
        },
    }

    messages = [
        {"role": "system", "content": sys},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]

    raw = await llm_client.chat(messages=messages, **node_params)

    try:
        json_text = _extract_first_json_object(raw)
        parsed = json.loads(json_text)
    except Exception as exc:
        # last resort: return defaults
        return {"aspects": _scores_default(), "evidence_count": 0, "error": str(exc)}

    # normalize missing keys
    aspects = parsed.get("aspects") or _scores_default()
    return {
        "aspects": aspects,
        "evidence_count": parsed.get("evidence_count", None) or 0,
    }

