/** Subset of graph_state.context fields used by the SPA. */

export type RestaurantCandidate = {
  name?: string | null;
  url?: string | null;
  explanation?: string[] | null;
  final_score?: number | null;
  formal_score?: number | null;
};

export type DialogContext = {
  final_recommendations?: RestaurantCandidate[];
  recommendations?: RestaurantCandidate[];
  shortlist?: RestaurantCandidate[];
  booking_pending?: boolean;
  booking_complete?: boolean;
  booking_selected_candidate?: RestaurantCandidate | null;
  booking_missing_fields?: string[];
  booking_errors?: string[];
  reservation_result?: Record<string, unknown> | null;
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
