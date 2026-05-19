import React, { useEffect, useMemo, useState } from "react";
import {
  candidateKey,
  DialogContext,
  getRecommendationList,
  RestaurantCandidate
} from "./dialogTypes";

type BookingTableOption = {
  id: string;
  title: string;
  capacity: number;
  status: string;
  free_after?: string | null;
};

function defaultBookingDatetimeLocal(): string {
  const d = new Date();
  d.setMinutes(0, 0, 0);
  d.setHours(d.getHours() + 1);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function isoToDatetimeLocalValue(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Время освобождения стола для подписи в списке (только часы:минуты, локальное время пользователя). */
function formatFreeAfterTimeOnly(iso: string | null | undefined): string | null {
  if (!iso || typeof iso !== "string") return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

function tableOptionText(row: BookingTableOption): string {
  const cap = `мест: ${row.capacity}`;
  if (row.status === "too_small") {
    return `${row.title} (${cap}, недостаточно мест для числа гостей)`;
  }
  if (row.status === "free") {
    return `${row.title} (${cap}, свободен)`;
  }
  const fa = formatFreeAfterTimeOnly(row.free_after ?? null);
  return `${row.title} (${cap}, занят${fa ? `; освободится ≈ ${fa}` : ""})`;
}

/** Convert `datetime-local` value to ISO 8601 with Z (UTC) — assumes local → toISOString. */
function localDatetimeToIso(local: string): string | null {
  if (!local) return null;
  const d = new Date(local);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString();
}

type DialogExtrasProps = {
  context: DialogContext | null;
  loading: boolean;
  /** Индекс карточки, по которой ушёл запрос select_booking_candidate (без глобального loading). */
  pendingBookingCandidateIndex?: number | null;
  onSelectCandidate: (index: number) => void;
  onSubmitBooking: (payload: {
    startsAt: string;
    guestCount: number;
    guestName: string;
    guestPhone: string;
    tableId: string | null;
    /** Название стола из списка формы; null при выборе «Любой» */
    tableTitle: string | null;
  }) => void;
  bookingUiHidden?: boolean;
  bookingErrorActionsVisible?: boolean;
  onEditBookingParams?: () => void;
  onNewBookingThread?: () => void;
};

function scoreLabel(c: RestaurantCandidate): string | null {
  const v = c.final_score ?? c.formal_score;
  if (v == null || typeof v !== "number" || Number.isNaN(v)) return null;
  return `${Math.round(v * 100)}%`;
}

/** Поля с разной длиной текста — фиксированная высота в 2 строки для выравнивания между карточками. */
function isTwoLineFactLabel(label: string): boolean {
  return label === "Адрес" || label === "Кухня";
}

export const DialogExtras: React.FC<DialogExtrasProps> = ({
  context,
  loading,
  pendingBookingCandidateIndex = null,
  onSelectCandidate,
  onSubmitBooking,
  bookingUiHidden,
  bookingErrorActionsVisible,
  onEditBookingParams,
  onNewBookingThread
}) => {
  const list = useMemo(() => getRecommendationList(context), [context]);
  const bookingPending = Boolean(context?.booking_pending);
  const bookingComplete = Boolean(context?.booking_complete);
  const selected = context?.booking_selected_candidate;
  const bookingReq = context?.booking_requirements;
  const reservation = context?.reservation_result;
  const hidden = Boolean(bookingUiHidden);
  const showErrorActions = hidden && Boolean(bookingErrorActionsVisible);

  const [startsLocal, setStartsLocal] = useState("");
  const [guestCountInput, setGuestCountInput] = useState("2");
  const [guestCountDirty, setGuestCountDirty] = useState(false);
  const [guestName, setGuestName] = useState("");
  const [guestPhone, setGuestPhone] = useState("");
  const [tableChoice, setTableChoice] = useState("");
  const [tableOptions, setTableOptions] = useState<BookingTableOption[]>([]);
  const [tablesLoading, setTablesLoading] = useState(false);
  const [tablesLoadError, setTablesLoadError] = useState<string | null>(null);

  const selectedUrl = selected && typeof selected.url === "string" ? selected.url : null;
  const prefGuestCount = preferredGuestCount(context);
  const hasSelectedCandidate = Boolean(
    selected &&
      ((typeof selected.url === "string" && selected.url.trim()) ||
        (typeof selected.name === "string" && selected.name.trim()))
  );
  const showBooking =
    Boolean(context) &&
    bookingPending &&
    !bookingComplete &&
    list.length > 0 &&
    hasSelectedCandidate;

  useEffect(() => {
    if (!guestCountDirty) {
      setGuestCountInput(String(prefGuestCount));
    }
  }, [prefGuestCount, guestCountDirty]);

  useEffect(() => {
    if (!showBooking) return;
    setStartsLocal(prev => {
      if (prev.trim()) return prev;
      const fromCtx = bookingReq?.starts_at;
      if (typeof fromCtx === "string" && fromCtx.trim()) {
        const loc = isoToDatetimeLocalValue(fromCtx.trim());
        if (loc) return loc;
      }
      return defaultBookingDatetimeLocal();
    });
  }, [showBooking, bookingReq?.starts_at]);

  useEffect(() => {
    if (!showBooking) return;
    const tid = bookingReq?.table_id;
    if (typeof tid === "string" && tid.trim()) {
      setTableChoice(tid.trim());
    } else {
      setTableChoice("");
    }
  }, [showBooking, bookingReq?.table_id]);

  useEffect(() => {
    if (!showBooking) return;
    const iso = localDatetimeToIso(startsLocal);
    const gc = Number.parseInt(guestCountInput.trim(), 10);
    if (!iso || Number.isNaN(gc) || gc < 1) {
      setTableOptions([]);
      return;
    }
    let cancelled = false;
    const t = window.setTimeout(() => {
      void (async () => {
        setTablesLoading(true);
        setTablesLoadError(null);
        try {
          const params = new URLSearchParams({
            starts_at: iso,
            guest_count: String(gc),
            duration_minutes: "120"
          });
          const resp = await fetch(`/api/dialog/booking-table-options?${params}`, {
            credentials: "include"
          });
          if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}`);
          }
          const data = (await resp.json()) as { tables?: BookingTableOption[] };
          const rows = Array.isArray(data.tables) ? data.tables : [];
          if (!cancelled) {
            setTableOptions(rows);
          }
        } catch {
          if (!cancelled) {
            setTableOptions([]);
            setTablesLoadError("Не удалось загрузить список столов.");
          }
        } finally {
          if (!cancelled) {
            setTablesLoading(false);
          }
        }
      })();
    }, 380);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [showBooking, startsLocal, guestCountInput]);

  if (!context) {
    return null;
  }

  const showRecommendations = list.length > 0;
  const bookingRestaurantName =
    firstNonEmpty(
      selected?.name,
      reservationString(reservation, "restaurant_name"),
      reservationString(reservation, "name")
    ) ?? "—";

  const handleBookingSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!startsLocal.trim()) return;
    const iso = localDatetimeToIso(startsLocal);
    if (!iso) return;
    const parsedGuestCount = Number.parseInt(guestCountInput.trim(), 10);
    if (Number.isNaN(parsedGuestCount) || parsedGuestCount < 1) return;
    const tid = tableChoice.trim();
    const row =
      tid.length > 0 ? tableOptions.find(t => t.id === tid) : undefined;
    const titleFromList =
      row && typeof row.title === "string" && row.title.trim()
        ? row.title.trim()
        : null;
    onSubmitBooking({
      startsAt: iso,
      guestCount: parsedGuestCount,
      guestName: guestName.trim(),
      guestPhone: guestPhone.trim(),
      tableId: tid.length > 0 ? tid : null,
      tableTitle: titleFromList
    });
  };

  return (
    <div className="dialog-extras">
      {showErrorActions && (
        <div className="chat-message chat-message-user chat-message-plan-confirm">
          <div className="chat-quick-actions chat-quick-actions--stack">
            <button
              type="button"
              className="chat-quick-action"
              onClick={onEditBookingParams}
              disabled={loading}
            >
              Изменить параметры
            </button>
            <button
              type="button"
              className="chat-quick-action"
              onClick={onNewBookingThread}
              disabled={loading}
            >
              Новый запрос
            </button>
          </div>
        </div>
      )}

      {!hidden && (
        <>
          {showRecommendations && (
            <>
              <ul className="restaurant-card-list">
                {list.map((c, i) => {
                  const active = selectedUrl != null && c.url === selectedUrl;
                  const sc = scoreLabel(c);
                  const partySize = context.recommendation_requirements?.party_size;
                  const details = buildCardDetails(c, partySize);
                  return (
                    <li key={candidateKey(c, i)} className="restaurant-card-wrap">
                      <article
                        className={`restaurant-card${
                          active ? " restaurant-card--active" : ""
                        }`}
                      >
                        <div className="restaurant-card-top">
                          <h3 className="restaurant-card-title">
                            {c.name?.trim() || "Ресторан"}
                          </h3>
                          {sc && <span className="restaurant-card-score">{sc}</span>}
                        </div>
                        {details.length > 0 && (
                          <ul className="restaurant-card-facts">
                            {details.map((item, j) => {
                              const twoLines = isTwoLineFactLabel(item.label);
                              return (
                                <li
                                  key={j}
                                  className={
                                    twoLines ? "restaurant-card-fact-row restaurant-card-fact-row--lines2" : undefined
                                  }
                                >
                                  {twoLines ? (
                                    <div className="restaurant-card-fact-multiline">
                                      <span className="restaurant-card-fact-label">{item.label}:</span>{" "}
                                      <span className="restaurant-card-fact-value">{item.value}</span>
                                    </div>
                                  ) : (
                                    <>
                                      <span className="restaurant-card-fact-label">{item.label}:</span>{" "}
                                      {item.value}
                                    </>
                                  )}
                                </li>
                              );
                            })}
                          </ul>
                        )}
                        {typeof c.url === "string" && c.url && (
                          <a
                            className="restaurant-card-link"
                            href={c.url}
                            target="_blank"
                            rel="noreferrer"
                            title="Открыть карточку ресторана на Afisha"
                          >
                            Карточка ресторана на Afisha
                          </a>
                        )}
                        {bookingPending && !bookingComplete && (
                          <button
                            type="button"
                            className="form-action-primary"
                            disabled={
                              loading ||
                              active ||
                              pendingBookingCandidateIndex === i
                            }
                            onClick={() => onSelectCandidate(i)}
                          >
                            {active ? "Выбран для брони" : "Забронировать здесь"}
                          </button>
                        )}
                      </article>
                    </li>
                  );
                })}
              </ul>
            </>
          )}

          {context.booking_errors && context.booking_errors.length > 0 && (
            <div className="dialog-extras-errors" role="alert">
              {context.booking_errors.map((err, i) => (
                <div key={i}>{err}</div>
              ))}
            </div>
          )}

          {showBooking && (
            <form className="booking-form" onSubmit={handleBookingSubmit}>
              <div className="booking-form-title">
                Бронирование «{bookingRestaurantTitle(bookingRestaurantName)}»
              </div>
              <label className="booking-field">
                <span>Дата и время</span>
                <input
                  type="datetime-local"
                  value={startsLocal}
                  onChange={e => setStartsLocal(e.target.value)}
                  required
                />
              </label>
              <label className="booking-field">
                <span>Стол</span>
                <select
                  className="booking-field-select"
                  value={tableChoice}
                  onChange={e => setTableChoice(e.target.value)}
                  disabled={loading || tablesLoading}
                  aria-busy={tablesLoading}
                >
                  <option value="">Любой</option>
                  {tableOptions.map(row => (
                    <option key={row.id} value={row.id}>
                      {tableOptionText(row)}
                    </option>
                  ))}
                </select>
                {tablesLoading && (
                  <span className="booking-form-hint" aria-live="polite">
                    Загрузка списка столов…
                  </span>
                )}
                {tablesLoadError && (
                  <span className="booking-form-hint" role="alert">
                    {tablesLoadError}
                  </span>
                )}
              </label>
              <label className="booking-field">
                <span>Гостей</span>
                <input
                  type="number"
                  min={1}
                  value={guestCountInput}
                  onChange={e => {
                    setGuestCountDirty(true);
                    setGuestCountInput(e.target.value);
                  }}
                />
              </label>
              <label className="booking-field">
                <span>Имя</span>
                <input
                  type="text"
                  value={guestName}
                  onChange={e => setGuestName(e.target.value)}
                  autoComplete="name"
                  required
                />
              </label>
              <label className="booking-field">
                <span>Телефон</span>
                <input
                  type="tel"
                  value={guestPhone}
                  onChange={e => setGuestPhone(e.target.value)}
                  autoComplete="tel"
                  required
                />
              </label>
              <button type="submit" className="form-action-primary" disabled={loading}>
                Отправить заявку
              </button>
            </form>
          )}

        </>
      )}
    </div>
  );
};

function preferredGuestCount(context: DialogContext | null): number {
  if (!context) return 2;
  const fromBooking = context.booking_requirements?.guest_count;
  if (typeof fromBooking === "number" && Number.isFinite(fromBooking) && fromBooking >= 1) {
    return Math.floor(fromBooking);
  }
  const fromRecommendation = context.recommendation_requirements?.party_size;
  if (
    typeof fromRecommendation === "number" &&
    Number.isFinite(fromRecommendation) &&
    fromRecommendation >= 1
  ) {
    return Math.floor(fromRecommendation);
  }
  return 2;
}

function bookingRestaurantTitle(name: string): string {
  const t = name.replace(/[«»]/g, "").trim();
  return t || "—";
}

function firstNonEmpty(...values: Array<string | null | undefined>): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }
  return null;
}

function reservationString(
  reservation: DialogContext["reservation_result"],
  key: string
): string | null {
  if (!reservation || typeof reservation[key] !== "string") return null;
  const value = String(reservation[key]).trim();
  return value || null;
}

function formatMaybeIso(value: string): string | null {
  const source = value.trim();
  if (!source) return null;
  const date = new Date(source);
  if (Number.isNaN(date.getTime())) return source;
  return date.toLocaleString("ru-RU");
}

function buildCardDetails(
  c: RestaurantCandidate,
  partySizeRaw: number | null | undefined
): Array<{ label: string; value: string }> {
  const avgCheck = normalizePriceRange(c.avg_check);
  const partySize =
    typeof partySizeRaw === "number" && Number.isFinite(partySizeRaw) && partySizeRaw >= 1
      ? Math.floor(partySizeRaw)
      : null;
  const companyCheck =
    avgCheck && partySize
      ? `${formatRub(avgCheck.min * partySize)} - ${formatRub(avgCheck.max * partySize)} ₽`
      : "—";
  const personCheck = avgCheck
    ? `${formatRub(avgCheck.min)} - ${formatRub(avgCheck.max)} ₽`
    : c.avg_check?.raw?.trim() || "—";
  const cuisine =
    Array.isArray(c.tags) && c.tags.length > 0
      ? c.tags.slice(0, 3).join(", ")
      : "—";
  const parking = boolLabel(c.flags?.parking);
  const banquets = boolLabel(c.flags?.banquets);
  const address = c.address?.trim() || "—";

  return [
    { label: "Адрес", value: address },
    { label: "Чек на компанию", value: companyCheck },
    { label: "Средний чек на человека", value: personCheck },
    { label: "Кухня", value: cuisine },
    { label: "Парковка", value: parking },
    { label: "Возможность банкета", value: banquets }
  ];
}

function normalizePriceRange(
  avg: RestaurantCandidate["avg_check"]
): { min: number; max: number } | null {
  if (!avg) return null;
  const min = avg.min;
  const max = avg.max;
  if (typeof min !== "number" || typeof max !== "number") return null;
  if (!Number.isFinite(min) || !Number.isFinite(max)) return null;
  return { min: Math.min(min, max), max: Math.max(min, max) };
}

function boolLabel(v: boolean | null | undefined): string {
  if (v === true) return "Есть";
  if (v === false) return "Нет";
  return "—";
}

function formatRub(v: number): string {
  return Math.round(v).toLocaleString("ru-RU");
}
