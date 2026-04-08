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

  const applyDialogResponse = useCallback(
    (data: DialogResponse, userMessages: Message[]) => {
      if (data.session_id && data.session_id !== sessionId) {
        onSessionChange(data.session_id);
      }
      const ctx = data.state?.context ?? null;
      setDialogContext(ctx);
      setMessages([...userMessages, { role: "assistant", content: data.reply }]);
    },
    [onSessionChange, sessionId]
  );

  const postDialog = useCallback(
    async (nextMessages: Message[], clientAction?: { type: string; index: number }) => {
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
      applyDialogResponse(data, nextMessages);
    },
    [applyDialogResponse]
  );

  const startNewThread = async () => {
    if (loading || resetting) return;
    setResetting(true);
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
    try {
      await postDialog(newMessages);
    } finally {
      setLoading(false);
    }
  };

  const selectCandidate = async (index: number) => {
    if (loading) return;
    setLoading(true);
    try {
      await postDialog(messages, {
        type: "select_booking_candidate",
        index
      });
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
    const text = [
      "Данные для брони:",
      `Дата и время: ${payload.startsAt}`,
      `Гостей: ${payload.guestCount}`,
      `Имя: ${payload.guestName}`,
      `Телефон: ${payload.guestPhone}`
    ].join("\n");
    const newMessages = [...messages, { role: "user" as const, content: text }];
    setMessages(newMessages);
    setLoading(true);
    try {
      await postDialog(newMessages);
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
          {resetting ? "Сброс…" : "Новый запрос"}
        </button>
      </div>
      <div className="chat-messages">
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
        onSelectCandidate={selectCandidate}
        onSubmitBooking={submitBooking}
      />

      <div className="chat-input-row">
        <textarea
          className="chat-input"
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="Опишите ваши предпочтения: кухня, бюджет, район, дата и время..."
          rows={2}
        />
        <button
          className="chat-send"
          onClick={send}
          disabled={loading}
        >
          Отправить
        </button>
      </div>
    </section>
  );
};
