import React, { useState } from "react";
import { Chat } from "./Chat";

export const App: React.FC = () => {
  const [sessionId, setSessionId] = useState<string | null>(null);

  return (
    <div className="app-root">
      <header className="app-header">
        <div className="app-header-inner">
          <h1>Reserved</h1>
          <p>Подбор ресторана, бронь и предзаказ с помощью ИИ</p>
        </div>
      </header>
      <main className="app-main">
        <Chat sessionId={sessionId} onSessionChange={setSessionId} />
      </main>
    </div>
  );
};

