import React, { useCallback, useMemo, useState } from "react";
import { DialogExtras } from "./DialogExtras";
import {
  DialogContext,
  PreorderCartLine,
  shouldShowSearchPlanConfirmButton
} from "./dialogTypes";
import {
  buildReceiptPayload,
  formatBookingDetailBullets,
  formatLocalDateTimeNoSeconds,
  formatReceiptSaveSuccessMessage,
  isSaveReceiptUserText,
  shouldOfferSaveReceipt
} from "../lib/receiptData";
import { generateBookingReceiptPdf } from "../lib/generateBookingReceiptPdf";
import { MenuPreorderCard } from "./MenuPreorderCard";

type Message = {
  role: "user" | "assistant";
  content: string;
};

type DialogResponse = {
  reply: string;
  session_id: string;
  state?: { context?: DialogContext; current_node?: string };
};

type ClientAction =
  | { type: "confirm_search_plan" }
  | { type: "select_booking_candidate"; index: number }
  | {
      type: "submit_booking";
      starts_at: string;
      guest_count: number;
      guest_name: string;
      guest_phone: string;
      table_id?: string;
    }
  | { type: "confirm_preorder_offer" }
  | { type: "preorder_decline_offer" }
  | { type: "preorder_choose_manual" }
  | { type: "preorder_llm_pick"; preferences_text?: string | null }
  | { type: "preorder_submit_cart"; lines: PreorderCartLine[] }
  | { type: "preorder_confirm_order" }
  | { type: "preorder_amend" }
  | { type: "save_receipt" };

type ChatProps = {
  sessionId: string | null;
  onSessionChange: (id: string) => void;
};

function lastAssistantMessageIndex(messages: Message[]): number {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === "assistant") return i;
  }
  return -1;
}

function browserTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    return undefined;
  }
}

export const Chat: React.FC<ChatProps> = ({ sessionId, onSessionChange }) => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [dialogContext, setDialogContext] = useState<DialogContext | null>(null);
  /** Last persisted graph node from `/api/dialog` — same source as `current_node` on the server. */
  const [dialogCurrentNode, setDialogCurrentNode] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [networkError, setNetworkError] = useState<string | null>(null);
  const [bookingUiHidden, setBookingUiHidden] = useState(false);
  const [bookingErrorActionsVisible, setBookingErrorActionsVisible] = useState(false);
  /** Только выбор карточки брони: без глобального loading, чтобы карточки не «мигали». */
  const [pendingBookingCandidateIndex, setPendingBookingCandidateIndex] = useState<number | null>(null);

  const applyDialogResponse = useCallback(
    (
      data: DialogResponse,
      userMessages: Message[],
      options?: { suppressAssistantReply?: boolean }
    ) => {
      if (data.session_id && data.session_id !== sessionId) {
        onSessionChange(data.session_id);
      }
      const ctx = data.state?.context ?? null;
      setDialogContext(ctx);
      const node =
        data.state && typeof data.state.current_node === "string"
          ? data.state.current_node
          : null;
      setDialogCurrentNode(node);
      if (options?.suppressAssistantReply) {
        setMessages(userMessages);
        return;
      }
      setMessages([...userMessages, { role: "assistant", content: data.reply }]);
    },
    [onSessionChange, sessionId]
  );

  const postDialog = useCallback(
    async (
      nextMessages: Message[],
      clientAction?: ClientAction,
      options?: { suppressAssistantReply?: boolean }
    ) => {
      const body: Record<string, unknown> = {
        messages: nextMessages.map(m => ({ role: m.role, content: m.content }))
      };
      if (clientAction) {
        body.client_action = clientAction;
      }
      const tz = browserTimeZone();
      if (tz) {
        body.client_time_zone = tz;
      }
      const resp = await fetch("/api/dialog", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        credentials: "include",
        body: JSON.stringify(body)
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = (await resp.json()) as DialogResponse;
      applyDialogResponse(data, nextMessages, options);
      return data;
    },
    [applyDialogResponse]
  );

  const startNewThread = async () => {
    if (loading || resetting) return;
    setResetting(true);
    setNetworkError(null);
    setBookingUiHidden(false);
    setBookingErrorActionsVisible(false);
    setPendingBookingCandidateIndex(null);
    try {
      const resp = await fetch("/api/dialog/session/new", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_time_zone: browserTimeZone() })
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = (await resp.json()) as { session_id: string };
      setMessages([]);
      setDialogContext(null);
      setDialogCurrentNode(null);
      setInput("");
      onSessionChange(data.session_id);
    } catch {
      setNetworkError("Не удалось создать новую сессию. Попробуйте ещё раз.");
    } finally {
      setResetting(false);
    }
  };

  const saveReceiptFlow = useCallback(
    async (userText: string) => {
      if (loading || !shouldOfferSaveReceipt(dialogContext)) return;
      const payload = buildReceiptPayload(dialogContext, messages);
      if (!payload) {
        setNetworkError("Нет данных для сохранения PDF.");
        return;
      }
      const trimmed = userText.trim() || "Сохрани";
      const newMessages: Message[] = [...messages, { role: "user", content: trimmed }];
      setMessages(newMessages);
      setLoading(true);
      setNetworkError(null);
      try {
        const filename = await generateBookingReceiptPdf(payload);
        const data = await postDialog(newMessages, { type: "save_receipt" }, {
          suppressAssistantReply: true
        });
        if (data.session_id && data.session_id !== sessionId) {
          onSessionChange(data.session_id);
        }
        const ctx = data.state?.context ?? null;
        setDialogContext(ctx);
        const successText = formatReceiptSaveSuccessMessage(filename);
        setMessages([...newMessages, { role: "assistant", content: successText }]);
      } catch {
        setNetworkError("Не удалось сформировать PDF. Попробуйте ещё раз.");
      } finally {
        setLoading(false);
      }
    },
    [dialogContext, loading, messages, onSessionChange, postDialog, sessionId]
  );

  const send = async () => {
    if (!input.trim()) return;
    const text = input.trim();
    if (shouldOfferSaveReceipt(dialogContext) && isSaveReceiptUserText(text)) {
      setInput("");
      await saveReceiptFlow(text);
      return;
    }
    const newMessages = [...messages, { role: "user" as const, content: text }];
    setMessages(newMessages);
    setInput("");
    setLoading(true);
    setNetworkError(null);
    try {
      await postDialog(newMessages);
    } catch {
      setNetworkError("Сервер временно недоступен. Проверьте подключение и повторите попытку.");
    } finally {
      setLoading(false);
    }
  };

  const selectCandidate = async (index: number) => {
    if (loading || pendingBookingCandidateIndex !== null) return;
    setPendingBookingCandidateIndex(index);
    setNetworkError(null);
    try {
      await postDialog(messages, {
        type: "select_booking_candidate",
        index
      }, { suppressAssistantReply: true });
    } catch {
      setNetworkError("Не удалось выбрать ресторан для бронирования.");
    } finally {
      setPendingBookingCandidateIndex(null);
    }
  };

  const confirmSearchPlan = async () => {
    if (loading) return;
    const prevMessages = messages;
    const withConfirm: Message[] = [
      ...messages,
      { role: "user", content: "Подтверждаю" }
    ];
    setMessages(withConfirm);
    setLoading(true);
    setNetworkError(null);
    try {
      await postDialog(withConfirm, { type: "confirm_search_plan" });
    } catch {
      setMessages(prevMessages);
      setNetworkError("Не удалось подтвердить параметры поиска.");
    } finally {
      setLoading(false);
    }
  };

  const preorderAction = useCallback(
    async (action: ClientAction, syntheticUser?: string) => {
      if (loading) return;
      const extra = (syntheticUser || "").trim();
      const base: Message[] = extra
        ? [...messages, { role: "user" as const, content: extra }]
        : messages;
      if (extra) setMessages(base);
      setLoading(true);
      setNetworkError(null);
      try {
        await postDialog(base, action);
      } catch {
        setNetworkError("Сервер временно недоступен. Проверьте подключение и повторите попытку.");
      } finally {
        setLoading(false);
      }
    },
    [loading, messages, postDialog]
  );

  const lastAssistantIndex = useMemo(() => lastAssistantMessageIndex(messages), [messages]);

  const submitBooking = async (payload: {
    startsAt: string;
    guestCount: number;
    guestName: string;
    guestPhone: string;
    tableId: string | null;
    tableTitle: string | null;
  }) => {
    if (loading) return;
    setLoading(true);
    setNetworkError(null);
    setBookingUiHidden(true);
    setBookingErrorActionsVisible(false);

    const restaurantName =
      dialogContext?.booking_selected_candidate?.name?.trim() ||
      "Ресторан";
    const restaurantAddress =
      dialogContext?.booking_selected_candidate?.address?.trim() || "—";
    const tableUserLine = (() => {
      if (!payload.tableId) return "любой";
      const t = payload.tableTitle?.trim();
      if (t) return t;
      return `стол (id: ${payload.tableId})`;
    })();
    const userBookingMessage = [
      `Запрос на бронь в ресторане «${restaurantName}».`,
      formatBookingDetailBullets({
        address: restaurantAddress,
        startsAtIso: payload.startsAt,
        guestCount: payload.guestCount,
        table: tableUserLine,
        guestName: payload.guestName,
        guestPhone: payload.guestPhone
      })
    ].join("\n");

    const newMessages = [
      ...messages,
      {
        role: "user" as const,
        content: userBookingMessage
      }
    ];
    setMessages(newMessages);
    try {
      const submitAction: ClientAction = {
        type: "submit_booking",
        starts_at: payload.startsAt,
        guest_count: payload.guestCount,
        guest_name: payload.guestName,
        guest_phone: payload.guestPhone
      };
      if (payload.tableId && payload.tableId.trim()) {
        submitAction.table_id = payload.tableId.trim();
      }
      const data = await postDialog(newMessages, submitAction, { suppressAssistantReply: true });
      const ctx = data.state?.context ?? null;
      const bookingComplete = Boolean(ctx?.booking_complete);

      if (bookingComplete) {
        setBookingUiHidden(true);
        setBookingErrorActionsVisible(false);
        const node =
          data.state && typeof data.state.current_node === "string"
            ? data.state.current_node
            : null;
        setDialogContext(ctx);
        setDialogCurrentNode(node);

        const res = ctx?.reservation_result ?? null;
        const req = ctx?.booking_requirements ?? {};
        const selected = ctx?.booking_selected_candidate ?? null;

        const rName =
          firstNonEmptyStr(selected?.name) ??
          firstNonEmptyStr(res?.restaurant_name, res?.name) ??
          "—";
        const rAddress =
          firstNonEmptyStr(selected?.address) ??
          firstNonEmptyStr(res?.restaurant_address, res?.address) ??
          "—";
        const timeRaw =
          (typeof res?.starts_at === "string" ? res?.starts_at : null) ??
          (typeof req?.starts_at === "string" ? req.starts_at : null) ??
          payload.startsAt;
        const gName =
          firstNonEmptyStr(res?.guest_name) ?? firstNonEmptyStr(req?.guest_name) ?? payload.guestName;
        const gPhone =
          firstNonEmptyStr(res?.guest_phone) ?? firstNonEmptyStr(req?.guest_phone) ?? payload.guestPhone;
        const tableTitle =
          firstNonEmptyStr(
            typeof res?.table_title === "string" ? res.table_title : undefined
          ) ?? null;
        const gcConfirm =
          typeof res?.guest_count === "number" && Number.isFinite(res.guest_count)
            ? Math.floor(res.guest_count)
            : typeof req?.guest_count === "number" && Number.isFinite(req.guest_count)
              ? Math.floor(req.guest_count)
              : payload.guestCount;
        const tableLineConfirm = tableTitle ?? "—";
        const assistantBooking = [
          `Бронь подтверждена в ресторане «${rName}».`,
          formatBookingDetailBullets({
            address: rAddress,
            startsAtIso: timeRaw,
            guestCount: gcConfirm,
            table: tableLineConfirm,
            guestName: gName || "—",
            guestPhone: gPhone || "—"
          })
        ].join("\n");

        const preorderAvail =
          Boolean(ctx?.preorder_menu_available) && ctx?.preorder_phase === "offer";
        const preorderPrompt =
          "Хотите оформить предзаказ к этому столу? Напишите «да», «ок» — или нажмите кнопку «Оформить предзаказ».";

        if (preorderAvail) {
          setMessages([
            ...newMessages,
            { role: "assistant", content: assistantBooking },
            { role: "assistant", content: preorderPrompt }
          ]);
        } else {
          setMessages([...newMessages, { role: "assistant", content: assistantBooking }]);
        }
      } else {
        const errs = ctx?.booking_errors;
        const hasFormErrors = Array.isArray(errs) && errs.length > 0;
        if (hasFormErrors) {
          setBookingUiHidden(false);
          setBookingErrorActionsVisible(false);
        } else {
          setBookingUiHidden(true);
          setBookingErrorActionsVisible(true);
        }
        const assistantMessage = data.reply || "Не удалось создать бронирование.";
        setMessages([...newMessages, { role: "assistant", content: assistantMessage }]);
      }
    } catch {
      // Падение запроса — это техническая проблема со стороны сети/сервера:
      // показываем общее сообщение и даём кнопки, т.к. пользователь не получил подтверждения.
      setBookingUiHidden(true);
      setBookingErrorActionsVisible(true);
      setMessages([
        ...newMessages,
        {
          role: "assistant",
          content: "Произошла техническая ошибка. Попробуйте позже."
        }
      ]);
      setNetworkError("Сервер временно недоступен. Проверьте подключение и повторите попытку.");
    } finally {
      setLoading(false);
    }
  };

  /** Только UI: снова показать карточки и форму, без запроса к API (LLM не вызывается). */
  const editBookingParams = () => {
    if (loading) return;
    setBookingUiHidden(false);
    setBookingErrorActionsVisible(false);
    setNetworkError(null);
  };

  return (
    <section className="chat-root">
      <div className="chat-toolbar">
        <button
          type="button"
          className="chat-new-thread"
          onClick={startNewThread}
          disabled={loading || resetting}
          title="Новая сессия: очистить чат и сбросить контекст на сервере"
        >
          <span className="chat-new-thread-icon" aria-hidden="true">
            ↻
          </span>
          <span>{resetting ? "Сброс…" : "Новый запрос"}</span>
        </button>
      </div>
      <div className="chat-scroll-body">
        <div className="chat-messages">
          {networkError && (
            <div className="chat-error" role="alert">
              {networkError}
            </div>
          )}
          {messages.map((m, idx) => {
            const showPlanConfirm =
              m.role === "assistant" &&
              idx === lastAssistantIndex &&
              shouldShowSearchPlanConfirmButton(dialogContext, dialogCurrentNode) &&
              !loading;
            const showSaveReceipt =
              m.role === "assistant" &&
              idx === lastAssistantIndex &&
              shouldOfferSaveReceipt(dialogContext) &&
              !loading;
            if (m.role === "assistant" && showPlanConfirm) {
              return (
                <React.Fragment key={idx}>
                  <div className="chat-message chat-message-assistant">
                    <div className="chat-assistant-stack">
                      <div className="chat-bubble">{m.content}</div>
                    </div>
                  </div>
                  <div className="chat-message chat-message-user chat-message-plan-confirm">
                    <div className="chat-quick-actions">
                      <button
                        type="button"
                        className="chat-quick-action"
                        disabled={loading}
                        onClick={() => void confirmSearchPlan()}
                        aria-label="Подтвердить параметры поиска"
                      >
                        Подтвердить
                      </button>
                    </div>
                  </div>
                </React.Fragment>
              );
            }
            if (m.role === "assistant" && showSaveReceipt) {
              return (
                <React.Fragment key={idx}>
                  <div className="chat-message chat-message-assistant">
                    <div className="chat-assistant-stack">
                      <div className="chat-bubble">{m.content}</div>
                    </div>
                  </div>
                  <div className="chat-message chat-message-user chat-message-plan-confirm">
                    <div className="chat-quick-actions">
                      <button
                        type="button"
                        className="chat-quick-action"
                        disabled={loading}
                        onClick={() => void saveReceiptFlow("Сохрани")}
                        aria-label="Сохранить бронь и предзаказ в PDF"
                      >
                        Сохранить
                      </button>
                    </div>
                  </div>
                </React.Fragment>
              );
            }
            return (
              <div
                key={idx}
                className={`chat-message chat-message-${m.role}`}
              >
                {m.role === "assistant" ? (
                  <div className="chat-assistant-stack">
                    <div className="chat-bubble">{m.content}</div>
                  </div>
                ) : (
                  <div className="chat-bubble">{m.content}</div>
                )}
              </div>
            );
          })}
          {loading && (
            <div className="chat-message chat-message-assistant">
              <div className="chat-bubble chat-bubble-loading">
                Думаю над вашим запросом...
              </div>
            </div>
          )}

          {!loading && dialogContext?.preorder_phase === "offer" && dialogContext.preorder_menu_available && (
            <div className="chat-message chat-message-user chat-message-plan-confirm">
              <div className="chat-quick-actions chat-quick-actions--stack">
                <button
                  type="button"
                  className="chat-quick-action"
                  onClick={() => void preorderAction({ type: "confirm_preorder_offer" }, "Да")}
                >
                  Оформить предзаказ
                </button>
                <button
                  type="button"
                  className="chat-quick-action"
                  onClick={() => void preorderAction({ type: "preorder_decline_offer" })}
                >
                  Не сейчас
                </button>
              </div>
            </div>
          )}
          {!loading && dialogContext?.preorder_phase === "mode_choice" && dialogContext.preorder_menu_available && (
            <div className="chat-message chat-message-user chat-message-plan-confirm">
              <div className="chat-quick-actions">
                <button
                  type="button"
                  className="chat-quick-action"
                  onClick={() =>
                    void preorderAction({ type: "preorder_choose_manual" }, "Выберу сам из меню")
                  }
                >
                  Выберу сам из меню
                </button>
              </div>
            </div>
          )}
          {!loading &&
            dialogContext?.preorder_phase === "browsing" &&
            dialogContext.preorder_menu_available &&
            typeof dialogContext.preorder_organization_id === "string" &&
            dialogContext.preorder_organization_id.trim() &&
            typeof dialogContext.preorder_store_id === "string" &&
            dialogContext.preorder_store_id.trim() && (
              <MenuPreorderCard
                organizationId={dialogContext.preorder_organization_id.trim()}
                storeId={dialogContext.preorder_store_id.trim()}
                initialLines={
                  Array.isArray(dialogContext.preorder_cart_lines)
                    ? (dialogContext.preorder_cart_lines as PreorderCartLine[])
                    : []
                }
                loading={loading}
                onSubmitCart={lines => void preorderAction({ type: "preorder_submit_cart", lines })}
              />
            )}
          {!loading && dialogContext?.preorder_phase === "summary" && (
            <div className="chat-message chat-message-user chat-message-plan-confirm">
              <div className="chat-quick-actions chat-quick-actions--stack">
                <button
                  type="button"
                  className="chat-quick-action"
                  onClick={() => void preorderAction({ type: "preorder_confirm_order" }, "Подтверждаю")}
                >
                  Подтвердить
                </button>
                <button
                  type="button"
                  className="chat-quick-action"
                  onClick={() => void preorderAction({ type: "preorder_amend" })}
                >
                  Внести изменения
                </button>
              </div>
            </div>
          )}

          <DialogExtras
            context={dialogContext}
            loading={loading}
            pendingBookingCandidateIndex={pendingBookingCandidateIndex}
            onSelectCandidate={selectCandidate}
            onSubmitBooking={submitBooking}
            bookingUiHidden={bookingUiHidden}
            bookingErrorActionsVisible={bookingErrorActionsVisible}
            onEditBookingParams={editBookingParams}
            onNewBookingThread={() => void startNewThread()}
          />
        </div>
      </div>

      <div className="chat-input-row">
        <textarea
          className="chat-input"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder="Опишите ваши предпочтения: кухня, бюджет, район, дата и время..."
          rows={4}
        />
        <button
          className="chat-send"
          onClick={send}
          disabled={loading}
          aria-label="Отправить сообщение"
          title="Отправить сообщение"
        >
          <span className="chat-send-icon" aria-hidden="true">➤</span>
        </button>
      </div>
    </section>
  );
};

function firstNonEmptyStr(...values: Array<string | null | undefined>): string | null {
  for (const v of values) {
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return null;
}

