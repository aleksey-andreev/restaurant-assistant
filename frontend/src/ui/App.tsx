import React, { useState } from "react";
import { Chat } from "./Chat";

export const App: React.FC = () => {
  const [sessionId, setSessionId] = useState<string | null>(null);

  return (
    <div className="app-root">
      <header className="app-header">
        <h1>Restaurant Assistant</h1>
        <p>Подбор ресторана, бронь и предзаказ с помощью ИИ</p>
      </header>
      <main className="app-main">
        <Chat sessionId={sessionId} onSessionChange={setSessionId} />
      </main>
    </div>
  );
};

