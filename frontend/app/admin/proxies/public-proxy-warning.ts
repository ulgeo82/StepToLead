export const publicProxyWarning =
  "Прокси без логина и пароля — чаще всего публичные. Telegram быстро банит аккаунты с них. " +
  "Лучше купить приватные IPv4 (1 прокси = 1 аккаунт)";

export function hasPossiblyPublicProxy(value: string | readonly { username?: string | null }[]): boolean {
  // The list API exposes only the login. Its absence is a hint, not proof that there's no password.
  if (typeof value !== "string") return value.some(proxy => !proxy.username?.trim());

  // A single address without credentials is enough, including batches reusing one host.
  return value.split(/\r?\n/).some(line => {
    const entry = line.trim();
    if (!entry || entry.startsWith("#")) return false;
    if (/^(?:socks[45]|https?):\/\//i.test(entry)) {
      try {
        const url = new URL(entry);
        // URL.port drops explicit default ports (:80/:443); read the number from the authority.
        const port = Number(entry.match(/^[a-z0-9]+:\/\/[^/?#]+:(\d+)(?=[/?#]|$)/i)?.[1]);
        return !!url.hostname && port >= 1 && port <= 65535 && !url.username && !url.password;
      } catch {
        return false;
      }
    }

    const address = entry.replace(/^:@|^@|@:$|@$|^::|::$/g, "");
    const match = address.match(/^((?:[a-z0-9-]+\.)+[a-z0-9-]+|localhost):(\d+)$/i);
    const port = Number(match?.[2]);
    return !!match && port >= 1 && port <= 65535;
  });
}
