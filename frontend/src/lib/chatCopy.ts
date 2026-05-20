/** Shown at thread start; kept in UI history, not sent to the dialog API. */
export const CHAT_WELCOME_LINES = [
  "Это сервис для подбора ресторанов, бронирования столиков и предзаказа блюд.",
  "Могу помочь с выбором по вашим пожеланиям или сразу забронировать столик в любимом ресторане."
] as const;

export const CHAT_WELCOME_CONTENT = CHAT_WELCOME_LINES.join("\n\n");

export type ChatWelcomeMessage = {
  role: "assistant";
  content: string;
  welcome: true;
};

export function createWelcomeMessage(): ChatWelcomeMessage {
  return { role: "assistant", content: CHAT_WELCOME_CONTENT, welcome: true };
}

export function isWelcomeMessage(m: { welcome?: boolean }): boolean {
  return m.welcome === true;
}

export function dialogApiMessages<T extends { role: string; content: string; welcome?: boolean }>(
  messages: T[]
): Array<{ role: string; content: string }> {
  return messages
    .filter(m => !isWelcomeMessage(m))
    .map(m => ({ role: m.role, content: m.content }));
}
