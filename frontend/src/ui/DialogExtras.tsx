import React, { useEffect, useMemo, useState } from "react";
import {
  candidateKey,
  DialogContext,
  getRecommendationList,
  needsSearchPlanConfirm,
  RestaurantCandidate
} from "./dialogTypes";

type DialogExtrasProps = {
  context: DialogContext | null;
  loading: boolean;
  onConfirmSearchPlan: () => void;
  onSelectCandidate: (index: number) => void;
  onSubmitBooking: (payload: {
    startsAt: string;
    guestCount: number;
    guestName: string;
    guestPhone: string;
  }) => void;
};

function scoreLabel(c: RestaurantCandidate): string | null {
  const v = c.final_score ?? c.formal_score;
  if (v == null || typeof v !== "number" || Number.isNaN(v)) return null;
  return `${Math.round(v * 100)}%`;
}

export const DialogExtras: React.FC<DialogExtrasProps> = ({
  context,
  loading,
  onConfirmSearchPlan,
  onSelectCandidate,
  onSubmitBooking
}) => {
  const list = useMemo(() => getRecommendationList(context), [context]);
  const bookingPending = Boolean(context?.booking_pending);
  const bookingComplete = Boolean(context?.booking_complete);
  const selected = context?.booking_selected_candidate;
  const missing = context?.booking_missing_fields ?? [];
  const bookingReq = context?.booking_requirements;
  const reservation = context?.reservation_result;

  const [startsLocal, setStartsLocal] = useState("");
  const [guestCountInput, setGuestCountInput] = useState("2");
  const [guestCountDirty, setGuestCountDirty] = useState(false);
  const [guestName, setGuestName] = useState("");
  const [guestPhone, setGuestPhone] = useState("");

  const selectedUrl = selected && typeof selected.url === "string" ? selected.url : null;
  const prefGuestCount = preferredGuestCount(context);

  useEffect(() => {
    if (!guestCountDirty) {
      setGuestCountInput(String(prefGuestCount));
    }
  }, [prefGuestCount, guestCountDirty]);

  if (!context) {
    return null;
  }

  const showSearchPlanConfirm = needsSearchPlanConfirm(context);

  const showBooking =
    bookingPending && !bookingComplete && list.length > 0;
  const showRecommendations = list.length > 0;
  const confirmation = {
    restaurantName:
      firstNonEmpty(
        selected?.name,
        reservationString(reservation, "restaurant_name"),
        reservationString(reservation, "name")
      ) ?? "—",
    restaurantAddress:
      firstNonEmpty(
        selected?.address,
        reservationString(reservation, "restaurant_address"),
        reservationString(reservation, "address")
      ) ?? "—",
    bookingTime:
      formatMaybeIso(
        firstNonEmpty(bookingReq?.starts_at, reservation?.starts_at) ?? ""
      ) || "—",
    guestName:
      firstNonEmpty(bookingReq?.guest_name, reservation?.guest_name) ?? "—",
    guestPhone:
      firstNonEmpty(bookingReq?.guest_phone, reservation?.guest_phone) ?? "—"
  };

  const handleBookingSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!startsLocal.trim()) return;
    const iso = localDatetimeToIso(startsLocal);
    if (!iso) return;
    const parsedGuestCount = Number.parseInt(guestCountInput.trim(), 10);
    if (Number.isNaN(parsedGuestCount) || parsedGuestCount < 1) return;
    onSubmitBooking({
      startsAt: iso,
      guestCount: parsedGuestCount,
      guestName: guestName.trim(),
      guestPhone: guestPhone.trim()
    });
  };

  return (
    <div className="dialog-extras">
      {showSearchPlanConfirm && (
        <div className="search-plan-panel" role="region" aria-label="Подтверждение параметров поиска">
          <div className="search-plan-panel-title">Параметры поиска</div>
          <p className="search-plan-panel-text">
            Проверьте сводку в последнем сообщении ассистента. Если всё верно — запустите поиск.
          </p>
          <div className="search-plan-panel-actions">
            <button
              type="button"
              className="search-plan-confirm"
              disabled={loading}
              onClick={() => onConfirmSearchPlan()}
            >
              Подтвердить поиск
            </button>
          </div>
        </div>
      )}

      {showRecommendations && (
        <>
          <div className="dialog-extras-heading">Варианты</div>
          <ul className="restaurant-card-list">
            {list.map((c, i) => {
              const active = selectedUrl != null && c.url === selectedUrl;
              const sc = scoreLabel(c);
              const partySize = context.recommendation_requirements?.party_size;
              const details = buildCardDetails(c, partySize);
              return (
                <li key={candidateKey(c, i)} className="restaurant-card-wrap">
                  <article
                    className={`restaurant-card${active ? " restaurant-card--active" : ""}`}
                  >
                    <div className="restaurant-card-top">
                      <h3 className="restaurant-card-title">
                        {c.name?.trim() || "Ресторан"}
                      </h3>
                      {sc && <span className="restaurant-card-score">{sc}</span>}
                    </div>
                    {details.length > 0 && (
                      <ul className="restaurant-card-facts">
                        {details.map((item, j) => (
                          <li key={j}>
                            <span className="restaurant-card-fact-label">{item.label}:</span> {item.value}
                          </li>
                        ))}
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
                    {typeof c.toka_capacity_message === "string" &&
                      c.toka_capacity_message.trim() && (
                        <p className="restaurant-card-toka-hint" role="note">
                          {c.toka_capacity_message}
                        </p>
                      )}
                    {bookingPending && !bookingComplete && (
                      <button
                        type="button"
                        className="restaurant-card-select"
                        disabled={loading || active}
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
          <div className="booking-form-title">Бронирование</div>
          {missing.length > 0 && (
            <p className="booking-form-hint">
              Нужно: {missing.join(", ")}
            </p>
          )}
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
          <button type="submit" className="booking-form-submit" disabled={loading}>
            Отправить заявку
          </button>
        </form>
      )}

      {bookingComplete && (
        <div className="booking-confirmation" role="status" aria-live="polite">
          <div className="booking-confirmation-title">Подтверждение бронирования</div>
          <div className="booking-confirmation-row">
            <span>Ресторан</span>
            <strong>{confirmation.restaurantName}</strong>
          </div>
          <div className="booking-confirmation-row">
            <span>Адрес</span>
            <strong>{confirmation.restaurantAddress}</strong>
          </div>
          <div className="booking-confirmation-row">
            <span>Время</span>
            <strong>{confirmation.bookingTime}</strong>
          </div>
          <div className="booking-confirmation-row">
            <span>Имя</span>
            <strong>{confirmation.guestName}</strong>
          </div>
          <div className="booking-confirmation-row">
            <span>Телефон</span>
            <strong>{confirmation.guestPhone}</strong>
          </div>
        </div>
      )}
    </div>
  );
};

/** Convert `datetime-local` value to ISO 8601 with Z (UTC) — assumes local → toISOString. */
function localDatetimeToIso(local: string): string | null {
  if (!local) return null;
  const d = new Date(local);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString();
}

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
