from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .cuisine_normalize import canonical_set_from_tags


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


def _occasion_score(requirements: Dict[str, Any], cand: Dict[str, Any]) -> float:
    occasion = (requirements.get("occasion") or "").strip().lower()
    flags: Dict[str, Optional[bool]] = cand.get("flags") or {}

    # Keep "romantic" and similar as soft signals: only use what's available as structure.
    if occasion in {"birthday", "день рождения"}:
        # For birthday, банкет/кейтеринг usually correlates with readiness.
        return 0.6 * (1.0 if flags.get("banquets") else 0.0) + 0.4 * (1.0 if flags.get("catering") else 0.0)
    if occasion in {"anniversary", "юбилей"}:
        return 0.5 * (1.0 if flags.get("banquets") else 0.0) + 0.5 * (1.0 if flags.get("catering") else 0.0)
    if occasion in {"romantic", "романтическая встреча", "романтический"}:
        # We don't have "quiet/cozy" structured fields from Afisha reliably,
        # so use only presence of delivery/parking/etc. as weak proxies.
        s = 0.0
        if flags.get("parking") is True:
            s += 0.15
        if flags.get("delivery") is True:
            s += 0.1
        # banquets/catering are neutral but can imply "appropriate for date group"
        if flags.get("banquets") is True:
            s += 0.1
        return min(0.5, s)

    return 0.0


@dataclass
class RankedCandidate:
    candidate: Dict[str, Any]
    score: float
    budget_score: Optional[float]
    cuisine_score: Optional[float]
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

        occ_score: Optional[float] = None
        occasion = str(requirements.get("occasion") or "").strip().lower()
        if occasion:
            occ_score = _occasion_score(requirements, cand)
            if occ_score and occ_score > 0:
                reasons.append("Occasion fit (banquets/catering/availability)")

        # Assemble weighted total using only known components
        components: List[Tuple[str, float, Optional[float]]] = [
            ("budget", 0.40, budget_score),
            ("cuisine", 0.35, cuisine_score),
            ("occasion", 0.20, occ_score),
        ]
        known = [c for c in components if c[2] is not None]
        if hard_pass:
            total = 0.0
            scored.append(
                RankedCandidate(
                    candidate=cand,
                    score=0.0,
                    budget_score=budget_score,
                    cuisine_score=cuisine_score,
                    occasion_score=occ_score,
                    hard_pass=True,
                    reasons=reasons + ["Hard pass"],
                )
            )
            continue

        if not known:
            total = 0.0
        else:
            weight_sum = sum(w for _, w, _v in known)
            total = sum((w / weight_sum) * float(v) for _name, w, v in known if v is not None)
        total = max(0.0, min(1.0, float(total)))

        scored.append(
            RankedCandidate(
                candidate=cand,
                score=total,
                budget_score=budget_score,
                cuisine_score=cuisine_score,
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

