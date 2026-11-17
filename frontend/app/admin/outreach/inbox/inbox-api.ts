import { api } from "@/lib/api";
import { SPACE_HEADER } from "@/lib/space";

export function inboxRequest<T>(path: string, workspace: number | null, init?: RequestInit) {
  const headers = new Headers(init?.headers);
  if (workspace != null) headers.set(SPACE_HEADER, String(workspace));
  return api<T>(path, { ...init, headers, noSpace: true });
}

export function inboxMutation<T>(path: string, workspace: number | null, method = "POST", data?: object) {
  return inboxRequest<T>(path, workspace, data === undefined ? { method } : {
    method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
}
