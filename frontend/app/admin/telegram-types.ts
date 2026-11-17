import type { Proxy, TelegramAccount } from "@/lib/api";

export type HealthLevel = "ok" | "warn" | "dead" | "unknown";
export type Health = { score: number; level: HealthLevel; reasons: string[] };
export type Pool = Proxy["pool"];
export type ProxyScheme = "auto" | "socks5" | "http";
export type InfraProxy = Proxy & {
  exit_ip: string | null;
  latency_ms: number | null;
  rotating: boolean;
  duplicate_of: number | null;
  health: Health & { uptime_24h: number | null; fails_in_row: number };
  accounts: { id: number; phone: string; name: string | null }[];
};
export type InfraAccount = TelegramAccount & {
  proxy: Pick<Proxy, "id" | "host" | "port" | "scheme" | "status"> & { health_level: HealthLevel } | null;
  health: Health & {
    spamblock: { state: "none" | "limited" | "unknown"; until: string | null; checked_at: string | null };
    sent_today: number;
    sent_24h: number;
    replies_7d: number;
    errors_24h: number;
    age_days: number | null;
    profile: { photo: boolean; username: boolean; bio: boolean };
  };
};
export type HealthSummary = {
  accounts: Record<"total" | "ok" | "warn" | "dead" | "waiting_proxy" | "spamblock", number>;
  proxies: Record<"total" | "ok" | "warn" | "dead" | "free", number>;
};
export type Batch = { batch_id: number };
export type ProxyBulkResult = { imported: number; duplicates: number; errors: string[]; batch_id: number | null };
export type ProxyDeleteResult = { deleted: number; skipped: { id: number; reason: string }[] };
export type ImportMode = "auto" | "order";
export type ImportBatch = Batch & {
  queued: number;
  rejected: { filename: string; error: string }[];
  without_proxy: number;
};
export type ImportResult = {
  state: string;
  items: {
    filename: string;
    status: "imported" | "waiting_proxy" | "skipped" | "error";
    account_id: number | null;
    phone: string | null;
    proxy: string | null;
    error: string | null;
  }[];
};
export type AvatarStrength = "low" | "medium";
export type AvatarPreview = { variants: string[] };
