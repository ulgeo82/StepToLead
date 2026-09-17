export type Campaign = {
  id: number;
  name: string;
  description: string | null;
  leads_count: number;
  created_at: string;
};

export type LeadStatus =
  | "new"
  | "queued"
  | "sent"
  | "replied"
  | "interested"
  | "not_interested"
  | "handoff"
  | "failed";

export type Lead = {
  id: number;
  campaign_id: number;
  username: string;
  first_message: string;
  status: LeadStatus;
  created_at: string;
};

export type Proxy = {
  id: number;
  scheme: string;
  host: string;
  port: number;
  username: string | null;
  label: string | null;
  status: string;
  last_error: string | null;
  last_checked_at: string | null;
  created_at: string;
  assigned_accounts: number;
};

export type TelegramAccount = {
  id: number;
  phone: string;
  telegram_user_id: number | null;
  first_name: string | null;
  last_name: string | null;
  username: string | null;
  bio: string | null;
  avatar_version: number;
  status: string;
  proxy_id: number | null;
  requires_2fa: boolean;
  last_error: string | null;
  flood_wait_until: string | null;
  last_seen_at: string | null;
  created_at: string;
};

export type ClientWorkspace = { id: number; name: string; status: string; created_at: string };
export type AdConnection = {
  id: number; workspace_id: number; workspace_name?: string; platform: "meta" | "yandex";
  platform_name: string; name: string; external_account_id: string; status: string;
  currency: string | null; last_error: string | null; last_checked_at: string | null;
  last_synced_at: string | null; created_at: string;
};
export type MarketingSummary = { spend: number; impressions: number; clicks: number; applications: number; cpc: number | null; conversion_rate: number | null; cost_per_application: number | null; connections: number; workspaces: number; first_date?: string | null; last_date?: string | null; days?: number };
export type PortalRole = "client_owner" | "sales_head" | "sales_manager" | "client_marketer" | "viewer";
export type PortalUser = {
  id: number; workspace_id: number; workspace_name?: string; username: string; display_name: string;
  role: PortalRole; role_name: string; active: boolean; must_change_password: boolean; created_at: string;
};
export type CrmLeadStatus = "new" | "contacted" | "qualified" | "proposal" | "won" | "lost";
export type CrmLead = {
  id: number; workspace_id: number; assigned_to_id: number | null; assigned_to_name: string | null;
  full_name: string; phone: string | null; email: string | null; source: string; status: CrmLeadStatus;
  status_name: string; value: number; notes: string | null; next_action_at: string | null;
  created_at: string; updated_at: string;
};
export type LeadInboundSource = {
  id: number; workspace_id: number; name: string; token_prefix: string; active: boolean;
  auto_assign: boolean; created_at: string;
};

// Запросы идут через Next.js на том же origin, а затем проксируются в backend.
// Это исключает CORS-проблемы и разницу между localhost и 127.0.0.1.
export const API_URL = "";
export const WS_URL = API_URL.replace(/^http/, "ws");

type ApiOptions = RequestInit & { timeoutMs?: number; absoluteUrl?: string };

export async function api<T>(path: string, options?: ApiOptions): Promise<T> {
  const controller = new AbortController();
  const timeoutMs = options?.timeoutMs ?? 30_000;
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const { timeoutMs: _timeoutMs, absoluteUrl, ...requestOptions } = options || {};

  try {
    const response = await fetch(absoluteUrl || `${API_URL}/api${path}`, {
      cache: "no-store",
      ...requestOptions,
      signal: requestOptions.signal || controller.signal,
    });
    if (!response.ok) {
      if (response.status === 401 && typeof window !== "undefined" && window.location.pathname.startsWith("/admin")) {
        window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
      }
      if (response.status === 401 && typeof window !== "undefined" && window.location.pathname.startsWith("/portal")) {
        window.location.assign(`/portal/login?next=${encodeURIComponent(window.location.pathname)}`);
      }
      const body = await response.json().catch(() => null);
      const detail = body?.detail;
      const message = Array.isArray(detail)
        ? detail.map((item: { msg?: string; loc?: (string | number)[] }) => `${item.loc?.slice(1).join(" · ") || "Поле"}: ${item.msg || "некорректное значение"}`).join("; ")
        : typeof detail === "string" ? detail : `Не удалось выполнить запрос (${response.status})`;
      throw new Error(message);
    }
    if (response.status === 204) return undefined as T;
    return response.json();
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("Сервер слишком долго не отвечает. Попробуйте ещё раз.");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}
