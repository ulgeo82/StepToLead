import type { Conversation, ConversationKey, InboxLabel } from "./inbox-types";

export const inboxRoot = "/admin/outreach";
export const inboxPage = `${inboxRoot}/inbox`;
export const countChangedEvent = "stl:inbox-counter";
export const inboxLabels: Record<InboxLabel, string> = {
  interested: "Интересно",
  question: "Вопрос",
  later: "Позже",
  not_interested: "Не интересно",
  unsubscribe: "Отписка",
  auto: "Автоответ",
  other: "Другое",
};
export const labelKeys = ["interested", "question", "later", "not_interested"] as const;
export const channelNames = { email: "✉ Email", telegram: "✈ Telegram" };

export function conversationKey(key: string | null, touch: string | null): ConversationKey | null {
  if (key && /^(email|tg):[1-9]\d*$/.test(key)) return key as ConversationKey;
  return touch && /^[1-9]\d*$/.test(touch) ? `email:${Number(touch)}` : null;
}

export function nextConversation(items: Conversation[], selected: ConversationKey | null, offset: 1 | -1) {
  const index = items.findIndex(item => item.key === selected);
  return items[index + offset]?.key ?? null;
}

export function renderReplyTemplate(text: string, conversation: Conversation) {
  const context: Record<string, string> = {
    name: conversation.contact.name ?? "",
    company: conversation.contact.company ?? "",
    sender_name: conversation.sender.label,
  };
  return text.replace(/\{\{\s*(name|company|sender_name)\s*\}\}/g, (_, key: string) => context[key]);
}

export function activeLock(lockedBy: Conversation["locked_by"], now = Date.now()) {
  return lockedBy && Date.parse(lockedBy.until) > now ? lockedBy : null;
}
