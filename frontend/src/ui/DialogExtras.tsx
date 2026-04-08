import React, { useMemo, useState } from "react";
import {
  candidateKey,
  DialogContext,
  getRecommendationList,
  RestaurantCandidate
} from "./dialogTypes";

type DialogExtrasProps = {
  context: DialogContext | null;
  loading: boolean;
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
  onSelectCandidate,
  onSubmitBooking
}) => {
  const list = useMemo(() => getRecommendationList(context), [context]);
  const bookingPending = Boolean(context?.booking_pending);
  const bookingComplete = Boolean(context?.booking_complete);
  const selected = context?.booking_selected_candidate;
  const missing = context?.booking_missing_fields ?? [];

  const [startsLocal, setStartsLocal] = useState("");
  const [guestCount, setGuestCount] = useState(2);
  const [guestName, setGuestName] = useState("");
  const [guestPhone, setGuestPhone] = useState("");

  const selectedUrl = selected && typeof selected.url === "string" ? selected.url : null;

  if (!context || list.length === 0) {
    return null;
  }

  const showBooking =
    bookingPending && !bookingComplete && list.length > 0;

  const handleBookingSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!startsLocal.trim()) return;
    const iso = localDatetimeToIso(startsLocal);
    if (!iso) return;
    onSubmitBooking({
      startsAt: iso,
      guestCount: Math.max(1, guestCount),
      guestName: guestName.trim(),
      guestPhone: guestPhone.trim()
    });
  };

  return (
    <div className="dialog-extras">
      <div className="dialog-extras-heading">Варианты</div>
      <ul className="restaurant-card-list">
        {list.map((c, i) => {
          const active = selectedUrl != null && c.url === selectedUrl;
          const expl = Array.isArray(c.explanation) ? c.explanation : [];
          const sc = scoreLabel(c);
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
                {expl.length > 0 && (
                  <ul className="restaurant-card-why">
                    {expl.slice(0, 4).map((line, j) => (
                      <li key={j}>{line}</li>
                    ))}
                  </ul>
                )}
                {typeof c.url === "string" && c.url && (
                  <a
                    className="restaurant-card-link"
                    href={c.url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Сайт / страница
                  </a>
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
              value={guestCount}
              onChange={e => setGuestCount(Number(e.target.value))}
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
