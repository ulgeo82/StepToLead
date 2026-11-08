export type QueryPurpose = "direct" | "search" | "jobs";
export type QuerySuggestions = { queries: string[]; note: string | null };

export function queryLines(value: unknown): string[] {
  return (Array.isArray(value) ? value : String(value ?? "").split(/\r?\n/))
    .filter((item): item is string => typeof item === "string")
    .map(item => item.trim()).filter(Boolean);
}

export function mergeQueries(current: string[], added: string[]): string[] {
  const seen = new Set<string>();
  return queryLines([...current, ...added]).filter(item => {
    const key = item.replace(/\s+/g, " ").toLocaleLowerCase("ru-RU");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function aiQueryBody(purpose: QueryPurpose, niche: string, brief: string, region: string) {
  return {
    purpose, niche: niche.trim(),
    ...(brief.trim() ? { brief: brief.trim() } : {}),
    ...(region.trim() ? { region: region.trim() } : {}),
  };
}
