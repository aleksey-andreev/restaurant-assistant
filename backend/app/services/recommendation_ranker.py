from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .cuisine_normalize import canonical_set_from_tags

from .external_rating import rating_score_normalized


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _normalize_token(s: str) -> str:
    return s.strip().lower()


def _contains_any(haystack: str, needles: List[str]) -> bool:
    h = haystack.lower()
    return any(n.lower() in h for n in needles if n)


def _interval_separation_gap(
    u_lo: float,
    u_hi: float,
    c_lo: float,
    c_hi: float,
) -> float:
    """Symmetric gap between closed intervals; 0 if they intersect (inclusive)."""
    if u_hi >= c_lo and c_hi >= u_lo:
        return 0.0
    if u_hi < c_lo:
        return float(c_lo - u_hi)
    return float(u_lo - c_hi)


def _budget_proximity_score(
    user_min: float,
    user_max: float,
    cand_min: float,
    cand_max: float,
    *,
    scale_rub: float = 2500.0,
) -> float:
    """
    Closer per-person budget interval to Afisha avg_check interval => higher score.
    Same penalty for restaurant cheaper or more expensive than the user's band (gap metric).
    Returns (0, 1], continuous.
    """
    gap = _interval_separation_gap(user_min, user_max, cand_min, cand_max)
    denom = max(float(scale_rub), 1.0)
    return 1.0 / (1.0 + gap / denom)


def _fallback_phrase_in_tags(cand_tags: List[str], phrases: List[str]) -> bool:
    """If canonical mapping missed, match user phrase as substring in raw tags."""
    blob = " ".join(_normalize_token(t) for t in cand_tags)
    for p in phrases:
        pn = _normalize_token(p)
        if pn and pn in blob:
            return True
    return False


def _cuisine_score(cand_tags: List[str], wanted: List[str], avoided: List[str]) -> float:
    """
    Cuisine match via normalized canonical ids (see cuisine_normalize).

    - No wanted/avoid → 1.0.
    - Any wanted: 1.0 if venue has at least one matching canonical (or substring fallback);
      0.0 if no match, or if venue maps only to avoided canonicals when avoid is set.
    - Only avoided: 1.0 if venue has no avoided-only specialization; 0.0 if all venue
      canonicals ⊆ avoided (user cannot escape avoided cuisine).
    """
    if not wanted and not avoided:
        return 1.0

    wanted_l = [w for w in wanted if isinstance(w, str) and w.strip()]
    avoided_l = [a for a in avoided if isinstance(a, str) and a.strip()]

    cand_c = canonical_set_from_tags(cand_tags)
    want_c = canonical_set_from_tags(wanted_l)
    avoid_c = canonical_set_from_tags(avoided_l)

    if wanted_l:
        if cand_c & want_c:
            return 1.0
        if not cand_c and _fallback_phrase_in_tags(cand_tags, wanted_l):
            return 1.0
        if avoid_c and cand_c and cand_c <= avoid_c:
            return 0.0
        return 0.0

    # only avoided
    if not cand_c:
        return 1.0
    if not (cand_c & avoid_c):
        return 1.0
    if cand_c <= avoid_c:
        return 0.0
    return 1.0


def prefilter_candidates_by_cuisine(
    candidates: List[Dict[str, Any]],
    requirements: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Drop candidates that fail cuisine gate when user specified wanted or avoided cuisines.
    If neither wanted nor avoided — keep all.
    """
    wanted = requirements.get("cuisine_wanted") or requirements.get("cuisines_wanted") or []
    avoided = requirements.get("cuisine_avoid") or requirements.get("cuisines_avoid") or []
    if not wanted and not avoided:
        return list(candidates)
    out: List[Dict[str, Any]] = []
    for c in candidates:
        tags = c.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        sc = _cuisine_score(
            [str(x) for x in tags if x is not None],
            [str(x) for x in wanted if isinstance(x, str)],
            [str(x) for x in avoided if isinstance(x, str)],
        )
        if sc > 0:
            out.append(c)
    return out


def _requirements_has_geo(requirements: Dict[str, Any]) -> bool:
    loc = requirements.get("location")
    if not isinstance(loc, dict):
        return False
    if loc.get("type") not in {"metro", "area"}:
        return False
    v = loc.get("value")
    return isinstance(v, str) and bool(v.strip())


@dataclass
class RankedCandidate:
    candidate: Dict[str, Any]
    score: float
    budget_score: Optional[float]
    cuisine_score: Optional[float]
    location_score: Optional[float]
    external_rating_score: Optional[float]
    occasion_score: Optional[float]
    hard_pass: bool
    reasons: List[str]


def rank_candidates(
    candidates: List[Dict[str, Any]],
    requirements: Dict[str, Any],
    *,
    min_score_floor: float = 0.35,
) -> Dict[str, Any]:
    """
    Formal scoring without reviews.

    Guardrails:
    - If a must-have is provided in requirements and cand has an explicit "Нет" => hard_pass
    - If a field is unknown (None) => don't punish, treat as unknown and reduce weight via normalization
    """
    budget = requirements.get("budget_range") or {}
    user_min = budget.get("min")
    user_max = budget.get("max")
    if user_min is None or user_max is None:
        user_min, user_max = None, None

    wanted_cuisines = requirements.get("cuisine_wanted") or requirements.get("cuisines_wanted") or []
    avoided_cuisines = requirements.get("cuisine_avoid") or requirements.get("cuisines_avoid") or []
    must_have = requirements.get("must_have") or []

    scored: List[RankedCandidate] = []
    for cand in candidates:
        flags: Dict[str, Optional[bool]] = cand.get("flags") or {}
        reasons: List[str] = []
        hard_pass = False

        # Hard-pass checks from must_have
        for mh in must_have:
            key = str(mh).strip().lower()
            if key in {"delivery", "доставка"}:
                if flags.get("delivery") is False:
                    hard_pass = True
                    reasons.append("Must-have: delivery not available")
            if key in {"parking", "парковка"}:
                if flags.get("parking") is False:
                    hard_pass = True
                    reasons.append("Must-have: parking not available")
            if key in {"banquets", "банкеты"}:
                if flags.get("banquets") is False:
                    hard_pass = True
                    reasons.append("Must-have: banquets not available")
            if key in {"catering", "кейтеринг"}:
                if flags.get("catering") is False:
                    hard_pass = True
                    reasons.append("Must-have: catering not available")

        budget_score: Optional[float] = None
        if user_min is not None and user_max is not None:
            cand_check = cand.get("avg_check") or {}
            cand_min = cand_check.get("min")
            cand_max = cand_check.get("max")
            if cand_min is not None and cand_max is not None:
                budget_score = _budget_proximity_score(
                    float(user_min),
                    float(user_max),
                    float(cand_min),
                    float(cand_max),
                )
                if budget_score >= 0.99:
                    reasons.append("Budget (per person) close to average check")
                elif budget_score > 0.5:
                    reasons.append("Budget (per person) moderately aligned with average check")

        cuisine_score = _cuisine_score(cand.get("tags") or [], wanted_cuisines, avoided_cuisines)
        if wanted_cuisines or avoided_cuisines:
            if cuisine_score > 0:
                reasons.append("Cuisine/tags match")

        has_geo = _requirements_has_geo(requirements)
        loc_raw = cand.get("geo_location_score")
        if has_geo:
            location_score: Optional[float] = (
                float(loc_raw) if loc_raw is not None and _safe_float(loc_raw) is not None else 0.45
            )
        else:
            location_score = None

        ext = cand.get("external_rating")
        ext_conf = cand.get("external_rating_confidence")
        ext_f = _safe_float(ext) if ext is not None else None
        ext_c = float(ext_conf) if ext_conf is not None and _safe_float(ext_conf) is not None else 0.0
        external_rating_score = rating_score_normalized(ext_f, ext_c)
        if external_rating_score is not None and external_rating_score > 0:
            reasons.append("External rating signal")

        occ_score: Optional[float] = None

        # Weighted composite: normalize over components with known values.
        parts: List[Tuple[str, float, Optional[float]]] = [("cuisine", 0.35, float(cuisine_score))]
        if budget_score is not None:
            parts.append(("budget", 0.25, float(budget_score)))
        if has_geo and location_score is not None:
            parts.append(("location", 0.30, float(location_score)))
        if external_rating_score is not None:
            parts.append(("rating", 0.20, float(external_rating_score)))

        if has_geo and external_rating_score is None:
            # Redistribute rating weight into location when geo matters but no rating.
            parts = [("cuisine", 0.30, float(cuisine_score))]
            if budget_score is not None:
                parts.append(("budget", 0.20, float(budget_score)))
            if location_score is not None:
                parts.append(("location", 0.50, float(location_score)))
        elif not has_geo and external_rating_score is not None:
            parts = [("cuisine", 0.40, float(cuisine_score))]
            if budget_score is not None:
                parts.append(("budget", 0.40, float(budget_score)))
            parts.append(("rating", 0.20, float(external_rating_score)))
        elif not has_geo and external_rating_score is None:
            parts = [("cuisine", 0.47, float(cuisine_score))]
            if budget_score is not None:
                parts.append(("budget", 0.53, float(budget_score)))

        known = [(n, w, v) for n, w, v in parts if v is not None]
        if hard_pass:
            total = 0.0
            scored.append(
                RankedCandidate(
                    candidate=cand,
                    score=0.0,
                    budget_score=budget_score,
                    cuisine_score=cuisine_score,
                    location_score=location_score,
                    external_rating_score=external_rating_score,
                    occasion_score=occ_score,
                    hard_pass=True,
                    reasons=reasons + ["Hard pass"],
                )
            )
            continue

        if not known:
            total = 0.0
        else:
            weight_sum = sum(w for _, w, _ in known)
            total = sum((w / weight_sum) * float(v) for _name, w, v in known)
        total = max(0.0, min(1.0, float(total)))

        scored.append(
            RankedCandidate(
                candidate=cand,
                score=total,
                budget_score=budget_score,
                cuisine_score=cuisine_score,
                location_score=location_score,
                external_rating_score=external_rating_score,
                occasion_score=occ_score,
                hard_pass=False,
                reasons=reasons,
            )
        )

    # Determine min_score based on percentile among non-hardpassed
    non_hard = [s for s in scored if not s.hard_pass]
    if not non_hard:
        min_score = 1.0
    else:
        sorted_scores = sorted(s.score for s in non_hard)
        idx = int(math.floor(len(sorted_scores) * 0.6))
        idx = min(max(idx, 0), len(sorted_scores) - 1)
        min_score = max(min_score_floor, sorted_scores[idx])

    above = [s for s in scored if (not s.hard_pass) and s.score >= min_score and s.score > 0]
    above_threshold_count = len(above)

    above_sorted = sorted(above, key=lambda x: x.score, reverse=True)
    # annotate reasons/evidence fields for top candidates
    scored_out = []
    for s in scored:
        scored_out.append(
            {
                **s.candidate,
                "formal_score": s.score,
                "budget_score": s.budget_score,
                "cuisine_score": s.cuisine_score,
                "location_score": s.location_score,
                "external_rating_score": s.external_rating_score,
                "occasion_score": s.occasion_score,
                "hard_pass": s.hard_pass,
                "reasons": s.reasons,
            }
        )

    return {
        "scored_candidates": scored_out,
        "min_score": float(min_score),
        "above_threshold": [
            u for x in above_sorted if (u := x.candidate.get("url"))
        ],
        "above_threshold_count": above_threshold_count,
        "top_candidates_sorted": above_sorted[:10],
    }


def recalc_formal_thresholds(
    scored_candidates: List[Dict[str, Any]],
    *,
    min_score_floor: float = 0.35,
) -> Tuple[float, int]:
    """
    Same percentile rule as rank_candidates, but on dicts with formal_score already set.
    Use after post-processing formal_score (e.g. Toka unverified penalty).
    """
    non_hard = [c for c in scored_candidates if not c.get("hard_pass")]
    if not non_hard:
        return 1.0, 0
    sorted_scores = sorted(float(c.get("formal_score") or 0.0) for c in non_hard)
    idx = int(math.floor(len(sorted_scores) * 0.6))
    idx = min(max(idx, 0), len(sorted_scores) - 1)
    min_score = max(min_score_floor, sorted_scores[idx])
    above_n = len(
        [
            c
            for c in non_hard
            if float(c.get("formal_score") or 0) >= min_score and float(c.get("formal_score") or 0) > 0
        ]
    )
    return float(min_score), above_n

