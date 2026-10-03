import { redirect } from "next/navigation";

type Search = { project_id?: string };

/** Old screen: its work now lives in /crm. Kept as a redirect so bookmarks keep working. */
export default async function Page({ searchParams }: { searchParams: Promise<Search> }) {
  const search = await searchParams;
  const query = new URLSearchParams({ tab: "report" });
  if (search.project_id) query.set("project_id", search.project_id);
  const suffix = query.toString();
  redirect(suffix ? `/crm?${suffix}` : "/crm");
}
