import { redirect } from "next/navigation";

type Search = { project_id?: string; platform?: string; account?: string };

export default async function CampaignsPage({ searchParams }: { searchParams: Promise<Search> }) {
  const search = await searchParams;
  const query = new URLSearchParams({ tab: "campaigns" });
  if (search.project_id) query.set("project_id", search.project_id);
  if (search.platform) query.set("platform", search.platform);
  if (search.account) query.set("account", search.account);
  redirect(`/analytics?${query}`);
}
