import { redirect } from "next/navigation";

// Старый адрес входа: ведём на /login, сохраняя ?next=.
export default async function OldPortalLogin({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const next = (await searchParams).next;
  redirect(typeof next === "string" && next ? `/login?next=${encodeURIComponent(next)}` : "/login");
}
