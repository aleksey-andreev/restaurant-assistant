/** Subset of graph_state.context fields used by the SPA. */

export type RestaurantCandidate = {
  name?: string | null;
  address?: string | null;
  url?: string | null;
  explanation?: string[] | null;
  avg_check?: { raw?: string | null; min?: number | null; max?: number | null } | null;
  tags?: string[] | null;
  flags?: {
    parking?: boolean | null;
    banquets?: boolean | null;
    delivery?: boolean | null;
    catering?: boolean | null;
    breakfast?: boolean | null;
    business_lunch?: boolean | null;
  } | null;
  final_score?: number | null;
  formal_score?: number | null;
  /** true if Toka halls confirm a table fits party_size; omitted/falsey if unverified */
  toka_capacity_verified?: boolean | null;
  /** Shown when стол could not be confirmed in Toka (stub/env/API) */
  toka_capacity_message?: string | null;
};

export type RecommendationRequirements = {
  city?: string | null;
  city_slug?: string | null;
  party_size?: number | null;
  budget_range?: { min?: number; max?: number } | null;
  location?: { type?: string; value?: string | null } | null;
  cuisine_wanted?: string[];
  cuisine_avoid?: string[];
  occasion?: string | null;
};

export type BookingRequirements = {
  starts_at?: string | null;
  guest_count?: number | null;
  guest_name?: string | null;
  guest_phone?: string | null;
  table_id?: string | null;
};

export type ReservationResult = {
  starts_at?: string | null;
  guest_name?: string | null;
  guest_phone?: string | null;
  restaurant_name?: string | null;
  restaurant_address?: string | null;
  table_id?: string | null;
  table_title?: string | null;
  [key: string]: unknown;
};

export type DialogContext = {
  booking_intent_mode?: "specific_restaurant" | "search" | null;
  /** IANA timezone from browser; set once per session */
  client_time_zone?: string | null;
  final_recommendations?: RestaurantCandidate[];
  recommendations?: RestaurantCandidate[];
  shortlist?: RestaurantCandidate[];
  recommendation_requirements?: RecommendationRequirements;
  requirements_complete?: boolean;
  missing_fields?: string[];
  search_plan_confirmed?: boolean;
  search_plan_fingerprint?: string | null;
  /** Internal: user declined plan summary; next reply asks what to change */
  search_plan_revision_requested?: boolean;
  booking_pending?: boolean;
  booking_complete?: boolean;
  booking_selected_candidate?: RestaurantCandidate | null;
  booking_requirements?: BookingRequirements;
  booking_missing_fields?: string[];
  booking_errors?: string[];
  reservation_result?: ReservationResult | null;
  specific_restaurant_requirements?: {
    name?: string | null;
    city?: string | null;
    city_slug?: string | null;
    address_or_hint?: string | null;
    source_url?: string | null;
  };
  specific_restaurant_missing_fields?: string[];
  specific_restaurant_resolved?: boolean;
};

export type DialogStatePayload = {
  context?: DialogContext;
};

export function getRecommendationList(ctx: DialogContext | null | undefined): RestaurantCandidate[] {
  if (!ctx) return [];
  const raw = ctx.final_recommendations ?? ctx.recommendations ?? ctx.shortlist;
  return Array.isArray(raw) ? raw : [];
}

export function candidateKey(c: RestaurantCandidate, index: number): string {
  const u = c.url;
  if (typeof u === "string" && u) return u;
  return `idx-${index}`;
}

/** Plan step: all mandatory search fields filled but user has not confirmed yet. */
export function needsSearchPlanConfirm(ctx: DialogContext | null | undefined): boolean {
  if (!ctx || ctx.booking_pending) return false;
  if (!ctx.requirements_complete) return false;
  if (ctx.search_plan_confirmed) return false;
  return true;
}

/**
 * Кнопка «Подтвердить» — только когда граф закончил ход на узле `confirm_search_plan`
 * (там же формируется текст «Параметры поиска (проверьте и подтвердите): …»).
 *
 * Одних полей контекста недостаточно: после пустой выдачи бэкенд сбрасывает
 * `search_plan_confirmed` в false при `requirements_complete`, и тогда
 * `needsSearchPlanConfirm` снова true при `current_node === "format_reply"` — без проверки
 * узла кнопка ошибочно показывалась бы снова.
 */
export function shouldShowSearchPlanConfirmButton(
  ctx: DialogContext | null | undefined,
  graphCurrentNode: string | null | undefined
): boolean {
  if (graphCurrentNode !== "confirm_search_plan") return false;
  return needsSearchPlanConfirm(ctx);
}
