import React, { useCallback, useState } from "react";
import { DialogExtras } from "./DialogExtras";
import { DialogContext } from "./dialogTypes";

type Message = {
  role: "user" | "assistant";
  content: string;
};

type DialogResponse = {
  reply: string;
  session_id: string;
  state?: { context?: DialogContext };
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
    };

type ChatProps = {
  sessionId: string | null;
  onSessionChange: (id: string) => void;
};

export const Chat: React.FC<ChatProps> = ({ sessionId, onSessionChange }) => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [dialogContext, setDialogContext] = useState<DialogContext | null>(null);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [networkError, setNetworkError] = useState<string | null>(null);

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
    },
    [applyDialogResponse]
  );

  const startNewThread = async () => {
    if (loading || resetting) return;
    setResetting(true);
    setNetworkError(null);
    try {
      const resp = await fetch("/api/dialog/session/new", {
        method: "POST",
        credentials: "include"
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = (await resp.json()) as { session_id: string };
      setMessages([]);
      setDialogContext(null);
      setInput("");
      onSessionChange(data.session_id);
    } catch {
      setNetworkError("Не удалось создать новую сессию. Попробуйте ещё раз.");
    } finally {
      setResetting(false);
    }
  };

  const send = async () => {
    if (!input.trim()) return;
    const newMessages = [...messages, { role: "user" as const, content: input }];
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
    if (loading) return;
    setLoading(true);
    setNetworkError(null);
    try {
      await postDialog(messages, {
        type: "select_booking_candidate",
        index
      }, { suppressAssistantReply: true });
    } catch {
      setNetworkError("Не удалось выбрать ресторан для бронирования.");
    } finally {
      setLoading(false);
    }
  };

  const confirmSearchPlan = async () => {
    if (loading) return;
    setLoading(true);
    setNetworkError(null);
    try {
      await postDialog(messages, { type: "confirm_search_plan" });
    } catch {
      setNetworkError("Не удалось подтвердить параметры поиска.");
    } finally {
      setLoading(false);
    }
  };

  const submitBooking = async (payload: {
    startsAt: string;
    guestCount: number;
    guestName: string;
    guestPhone: string;
  }) => {
    if (loading) return;
    setLoading(true);
    setNetworkError(null);
    try {
      await postDialog(messages, {
        type: "submit_booking",
        starts_at: payload.startsAt,
        guest_count: payload.guestCount,
        guest_name: payload.guestName,
        guest_phone: payload.guestPhone
      }, { suppressAssistantReply: true });
    } catch {
      setNetworkError("Не удалось отправить заявку на бронирование.");
    } finally {
      setLoading(false);
    }
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
          {messages.map((m, idx) => (
            <div
              key={idx}
              className={`chat-message chat-message-${m.role}`}
            >
              <div className="chat-bubble">{m.content}</div>
            </div>
          ))}
          {loading && (
            <div className="chat-message chat-message-assistant">
              <div className="chat-bubble chat-bubble-loading">
                Думаю над вашим запросом...
              </div>
            </div>
          )}
        </div>

        <DialogExtras
          context={dialogContext}
          loading={loading}
          onConfirmSearchPlan={confirmSearchPlan}
          onSelectCandidate={selectCandidate}
          onSubmitBooking={submitBooking}
        />
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
