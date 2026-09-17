import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import Sidebar from "@/components/Sidebar";

export const dynamic = "force-dynamic";
export const metadata = { title: "StepToLead — управление", robots: { index: false, follow: false } };

export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  const token = (await cookies()).get("stl_session")?.value;
  if (!token) redirect("/login");
  const response = await fetch(`${process.env.BACKEND_URL || "http://backend:8000"}/api/auth/me`, {
    headers: { Cookie: `stl_session=${token}` }, cache: "no-store", signal: AbortSignal.timeout(5000),
  }).catch(() => null);
  if (!response?.ok) redirect("/login");
  return <div className="shell"><Sidebar /><main className="main">{children}</main></div>;
}
