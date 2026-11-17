export type Channel = "email" | "telegram";
export type Summary = { companies: number; people: number; sent: number; replied: number; hot: number };
export type Contacted = {
  company: string | null;
  contact: string | null;
  channel: Channel;
  campaign: string | null;
  first_contact_at: string | null;
  status: "sent" | "replied" | "interested" | "refused";
};
export type Thread = {
  key: string;
  channel: Channel;
  campaign: string | null;
  contact: { name: string | null; address: string; company: string | null };
  sender: string | null;
  last: { text: string; at: string | null; direction: "in" | "out" };
  label: string | null;
  hot: boolean;
};
export type Conversation = Thread & { messages: {
  id: string; text: string; at: string | null; direction: "in" | "out"; author: "Лид" | "Агентство";
}[] };
export type Listing<T> = { total: number; items: T[] };
