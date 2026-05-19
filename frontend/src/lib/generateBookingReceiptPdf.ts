import { jsPDF } from "jspdf";
import type { BookingReceiptFields, ReceiptPayload } from "./receiptData";

const MARGIN_MM = 14;
const LINE_MM = 5.2;
const FONT = "NotoSans";

let fontBase64: string | null = null;

async function loadNotoSansBase64(): Promise<string> {
  if (fontBase64) return fontBase64;
  const resp = await fetch("/fonts/NotoSans-Regular.ttf");
  if (!resp.ok) {
    throw new Error("Не удалось загрузить шрифт для PDF");
  }
  const buf = await resp.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  fontBase64 = btoa(binary);
  return fontBase64;
}

function registerFont(doc: jsPDF, base64: string): void {
  doc.addFileToVFS("NotoSans-Regular.ttf", base64);
  doc.addFont("NotoSans-Regular.ttf", FONT, "normal");
  doc.setFont(FONT, "normal");
}

const SESSION_USED_NAMES_KEY = "receipt_pdf_used_filenames";

function receiptFilenameBase(at: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `bron-i-predzakaz-${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;
}

/** Уникальное имя в рамках сессии вкладки (браузер не даёт читать папку «Загрузки»). */
export function allocateReceiptFilename(at: Date): string {
  const base = receiptFilenameBase(at);
  let used: string[] = [];
  try {
    const raw = sessionStorage.getItem(SESSION_USED_NAMES_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as unknown;
      if (Array.isArray(parsed)) {
        used = parsed.filter((x): x is string => typeof x === "string");
      }
    }
  } catch {
    used = [];
  }
  let name = `${base}.pdf`;
  let n = 2;
  while (used.includes(name)) {
    name = `${base}-${n}.pdf`;
    n += 1;
  }
  used.push(name);
  try {
    sessionStorage.setItem(SESSION_USED_NAMES_KEY, JSON.stringify(used));
  } catch {
    /* private mode / quota */
  }
  return name;
}

function formatBookingBlock(b: BookingReceiptFields): string {
  return [
    `Бронь подтверждена в ресторане «${b.restaurantName}».`,
    `- Адрес: ${b.restaurantAddress}`,
    `- Дата и время: ${b.bookingTime}`,
    `- Гостей: ${b.guestCount}`,
    `- Стол: ${b.tableTitle}`,
    `- Имя: ${b.guestName}`,
    `- Телефон: ${b.guestPhone}`
  ].join("\n");
}

function writeParagraph(
  doc: jsPDF,
  text: string,
  x: number,
  y: number,
  maxWidth: number,
  fontSize: number,
  lineHeight: number
): number {
  doc.setFontSize(fontSize);
  const lines = doc.splitTextToSize(text, maxWidth);
  const pageH = doc.internal.pageSize.getHeight();
  const bottom = pageH - MARGIN_MM;
  for (const line of lines) {
    if (y + lineHeight > bottom) {
      doc.addPage();
      y = MARGIN_MM;
    }
    doc.text(line, x, y);
    y += lineHeight;
  }
  return y;
}

export async function generateBookingReceiptPdf(payload: ReceiptPayload): Promise<string> {
  const b64 = await loadNotoSansBase64();
  const doc = new jsPDF({ unit: "mm", format: "a4", orientation: "portrait" });
  registerFont(doc, b64);

  const pageW = doc.internal.pageSize.getWidth();
  const contentW = pageW - MARGIN_MM * 2;
  let y = MARGIN_MM;

  y = writeParagraph(doc, "Reserved", MARGIN_MM, y, contentW, 18, 7);
  y += 2;
  doc.setTextColor(255, 79, 18);
  y = writeParagraph(doc, "Бронь и предзаказ", MARGIN_MM, y, contentW, 14, 6);
  doc.setTextColor(79, 79, 79);
  y = writeParagraph(
    doc,
    `Сформировано: ${payload.generatedAt.toLocaleString("ru-RU")}`,
    MARGIN_MM,
    y,
    contentW,
    9,
    4.5
  );
  y += 3;
  doc.setTextColor(20, 20, 20);

  const bookingBlock = formatBookingBlock(payload.booking);
  y = writeParagraph(doc, bookingBlock, MARGIN_MM, y, contentW, 11, LINE_MM);
  y += 4;
  y = writeParagraph(doc, payload.preorderText, MARGIN_MM, y, contentW, 11, LINE_MM);

  const filename = allocateReceiptFilename(payload.generatedAt);
  doc.save(filename);
  return filename;
}
