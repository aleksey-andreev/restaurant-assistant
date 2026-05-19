import type { DialogContext, PreorderCartLine } from "../ui/dialogTypes";

export type ReceiptLine = PreorderCartLine & { line_total?: number };

export type ReceiptBookingSnapshot = {
  restaurant_name?: string | null;
  restaurant_address?: string | null;
  starts_at?: string | null;
  table_title?: string | null;
  guest_name?: string | null;
  guest_phone?: string | null;
  guest_count?: number | null;
};

export type BookingReceiptFields = {
  restaurantName: string;
  restaurantAddress: string;
  bookingTime: string;
  tableTitle: string;
  guestName: string;
  guestPhone: string;
  guestCount: number;
};

export type ReceiptPayload = {
  booking: BookingReceiptFields;
  preorderText: string;
  preorderTotal: number;
  generatedAt: Date;
};

type ChatMessage = { role: string; content: string };

function firstNonEmpty(...values: Array<string | null | undefined>): string | null {
  for (const v of values) {
    if (typeof v === "string" && v.trim()) return v.trim();
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

/** Локальная дата и время; время только часы:минуты (как в чате). */
export function formatLocalDateTimeNoSeconds(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const dateStr = d.toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric"
  });
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${dateStr}, ${hh}:${mm}`;
}

/** Единый блок полей брони (чат и PDF). */
export function formatBookingDetailBullets(p: {
  address: string;
  startsAtIso: string;
  guestCount: number;
  table: string;
  guestName: string;
  guestPhone: string;
}): string {
  const when = formatLocalDateTimeNoSeconds(p.startsAtIso);
  return [
    `- Адрес: ${p.address}`,
    `- Дата и время: ${when}`,
    `- Гостей: ${p.guestCount}`,
    `- Стол: ${p.table}`,
    `- Имя: ${p.guestName}`,
    `- Телефон: ${p.guestPhone}`
  ].join("\n");
}

function bulletValue(line: string, prefix: string): string | null {
  const p = `- ${prefix}: `;
  if (!line.startsWith(p)) return null;
  return line.slice(p.length).trim() || null;
}

/** Парсит сообщение ассистента «Бронь подтверждена…» — те же строки, что видит пользователь. */
export function parseBookingFromAssistantMessage(content: string): BookingReceiptFields | null {
  const text = (content || "").trim();
  if (!text.includes("Бронь подтверждена")) return null;
  const head = text.match(/Бронь подтверждена в ресторане «([^»]*)»/);
  if (!head) return null;
  const lines = text.split("\n");
  let guestCount = 1;
  const parsed: Partial<BookingReceiptFields> = {
    restaurantName: head[1].trim() || "—"
  };
  for (const line of lines) {
    const addr = bulletValue(line, "Адрес");
    if (addr != null) parsed.restaurantAddress = addr;
    const when = bulletValue(line, "Дата и время");
    if (when != null) parsed.bookingTime = when;
    const gc = bulletValue(line, "Гостей");
    if (gc != null) {
      const n = Number.parseInt(gc, 10);
      if (!Number.isNaN(n) && n >= 1) guestCount = n;
    }
    const table = bulletValue(line, "Стол");
    if (table != null) parsed.tableTitle = table;
    const name = bulletValue(line, "Имя");
    if (name != null) parsed.guestName = name;
    const phone = bulletValue(line, "Телефон");
    if (phone != null) parsed.guestPhone = phone;
  }
  return {
    restaurantName: parsed.restaurantName ?? "—",
    restaurantAddress: parsed.restaurantAddress ?? "—",
    bookingTime: parsed.bookingTime ?? "—",
    tableTitle: parsed.tableTitle ?? "—",
    guestName: parsed.guestName ?? "—",
    guestPhone: parsed.guestPhone ?? "—",
    guestCount
  };
}

export function findBookingAssistantMessage(messages: ChatMessage[]): string | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i];
    if (m.role === "assistant" && m.content.includes("Бронь подтверждена")) {
      return m.content;
    }
  }
  return null;
}

function fieldsFromSnapshot(snap: ReceiptBookingSnapshot): BookingReceiptFields {
  const gc =
    typeof snap.guest_count === "number" && Number.isFinite(snap.guest_count) && snap.guest_count >= 1
      ? Math.floor(snap.guest_count)
      : 1;
  const startsAt = (snap.starts_at || "").trim();
  return {
    restaurantName: (snap.restaurant_name || "").trim() || "—",
    restaurantAddress: (snap.restaurant_address || "").trim() || "—",
    bookingTime: startsAt ? formatLocalDateTimeNoSeconds(startsAt) : "—",
    tableTitle: (snap.table_title || "").trim() || "—",
    guestName: (snap.guest_name || "").trim() || "—",
    guestPhone: (snap.guest_phone || "").trim() || "—",
    guestCount: gc
  };
}

function fieldsFromContext(ctx: DialogContext): BookingReceiptFields {
  const selected = ctx.booking_selected_candidate;
  const reservation = ctx.reservation_result;
  const bookingReq = ctx.booking_requirements;
  const guestCount =
    typeof reservation?.guest_count === "number" && Number.isFinite(reservation.guest_count)
      ? Math.floor(reservation.guest_count)
      : typeof bookingReq?.guest_count === "number" && Number.isFinite(bookingReq.guest_count)
        ? Math.floor(bookingReq.guest_count)
        : typeof ctx.preorder_guest_count === "number" && Number.isFinite(ctx.preorder_guest_count)
          ? Math.floor(ctx.preorder_guest_count)
          : 1;
  const startsAt =
    firstNonEmpty(
      typeof reservation?.starts_at === "string" ? reservation.starts_at : null,
      bookingReq?.starts_at
    ) ?? "";
  return {
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
    bookingTime: startsAt ? formatLocalDateTimeNoSeconds(startsAt) : "—",
    tableTitle: firstNonEmpty(reservationString(reservation, "table_title")) ?? "—",
    guestName: firstNonEmpty(bookingReq?.guest_name, reservation?.guest_name) ?? "—",
    guestPhone: firstNonEmpty(bookingReq?.guest_phone, reservation?.guest_phone) ?? "—",
    guestCount
  };
}

export function buildBookingReceiptFields(
  ctx: DialogContext | null,
  messages?: ChatMessage[]
): BookingReceiptFields | null {
  if (!ctx?.booking_complete) return null;

  const fromChat = (() => {
    const raw = messages ? findBookingAssistantMessage(messages) : null;
    return raw ? parseBookingFromAssistantMessage(raw) : null;
  })();
  if (fromChat) return fromChat;

  const snap = ctx.receipt_booking_snapshot;
  if (snap && typeof snap === "object") {
    return fieldsFromSnapshot(snap);
  }

  return fieldsFromContext(ctx);
}

export function normalizePreorderReceiptLines(raw: unknown): ReceiptLine[] {
  if (!Array.isArray(raw)) return [];
  const out: ReceiptLine[] = [];
  for (const row of raw) {
    if (!row || typeof row !== "object") continue;
    const r = row as Record<string, unknown>;
    const mid = typeof r.menu_item_id === "string" ? r.menu_item_id : "";
    if (!mid.trim()) continue;
    let q = 1;
    if (typeof r.quantity === "number" && Number.isFinite(r.quantity)) {
      q = Math.max(1, Math.floor(r.quantity));
    }
    let price: number | null = null;
    if (typeof r.price === "number" && Number.isFinite(r.price)) price = r.price;
    const lineTotal =
      typeof r.line_total === "number" && Number.isFinite(r.line_total)
        ? r.line_total
        : price != null
          ? price * q
          : null;
    const item: ReceiptLine = {
      menu_item_id: mid,
      quantity: q,
      title: typeof r.title === "string" ? r.title : null,
      price,
      section: typeof r.section === "string" ? r.section : null
    };
    if (lineTotal != null) item.line_total = lineTotal;
    out.push(item);
  }
  return out;
}

export function preorderLinesTotal(lines: ReceiptLine[]): number {
  let t = 0;
  for (const ln of lines) {
    if (typeof ln.line_total === "number" && Number.isFinite(ln.line_total)) {
      t += ln.line_total;
      continue;
    }
    const p = typeof ln.price === "number" && Number.isFinite(ln.price) ? ln.price : 0;
    const q = typeof ln.quantity === "number" ? ln.quantity : 1;
    t += p * q;
  }
  return Math.round(t);
}

/** Текст предзаказа как в сводке чата (без таблицы). */
export function formatPreorderReceiptText(lines: ReceiptLine[], total: number): string {
  if (!lines.length) return "Предзаказ не оформлялся.";
  const parts: string[] = ["Состав предзаказа:"];
  const bySec = new Map<string, ReceiptLine[]>();
  for (const ln of lines) {
    const sec = (ln.section || "Блюда").trim() || "Блюда";
    const list = bySec.get(sec) ?? [];
    list.push(ln);
    bySec.set(sec, list);
  }
  for (const [sec, lns] of bySec) {
    parts.push("");
    parts.push(sec);
    for (const ln of lns) {
      const title = (ln.title || "—").trim();
      const q = ln.quantity ?? 1;
      const lineSum =
        typeof ln.line_total === "number" && Number.isFinite(ln.line_total)
          ? ln.line_total
          : (typeof ln.price === "number" ? ln.price : 0) * q;
      parts.push(`- ${title} × ${q} — ${Math.round(lineSum).toLocaleString("ru-RU")} ₽`);
    }
  }
  parts.push("");
  parts.push(`Итого: ${Math.round(total).toLocaleString("ru-RU")} ₽`);
  return parts.join("\n");
}

export function shouldOfferSaveReceipt(ctx: DialogContext | null | undefined): boolean {
  if (!ctx) return false;
  if (ctx.preorder_phase !== "done") return false;
  if (!ctx.save_receipt_offered) return false;
  if (ctx.save_receipt_done) return false;
  return true;
}

export function buildReceiptPayload(
  ctx: DialogContext | null,
  messages?: ChatMessage[]
): ReceiptPayload | null {
  const booking = buildBookingReceiptFields(ctx, messages);
  if (!booking) return null;
  const lines = normalizePreorderReceiptLines(ctx?.preorder_receipt_lines);
  const total =
    typeof ctx?.preorder_receipt_total === "number" && Number.isFinite(ctx.preorder_receipt_total)
      ? ctx.preorder_receipt_total
      : preorderLinesTotal(lines);
  return {
    booking,
    preorderText: formatPreorderReceiptText(lines, total),
    preorderTotal: total,
    generatedAt: new Date()
  };
}

/**
 * Сообщение в чате после успешного сохранения PDF.
 * Согласованный текст — правки только здесь.
 */
export function formatReceiptSaveSuccessMessage(filename: string): string {
  return `Готово. PDF «${filename}» в папке «Загрузки».`;
}

export function isSaveReceiptUserText(text: string): boolean {
  const t = text.trim();
  if (!t || t.length > 64) return false;
  if (/^(сохрани|сохранить|скачай|скачать|выгрузи|выгрузить)(\b|[.!?]|$)/i.test(t)) return true;
  return /^(да|давай|окей|ок|угу|ага|конечно|хорошо|согласен|согласна|подтверждаю|yes)\b/i.test(t);
}
