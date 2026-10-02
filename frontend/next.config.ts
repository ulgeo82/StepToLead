import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // Импорт tdata может проверять Telegram через прокси дольше обычного API-запроса.
  experimental: { proxyTimeout: 120_000 },
  async redirects() {
    return [
      { source: "/telegram-accounts/:path*", destination: "/admin/accounts/:path*", permanent: false },
      { source: "/proxies/:path*", destination: "/admin/proxies/:path*", permanent: false },
      { source: "/growth-calculator", destination: "/growth", permanent: false },
      { source: "/leads", destination: "/crm", permanent: false },
      { source: "/portal/crm", destination: "/crm", permanent: false },
    ];
  },
  async headers() {
    return [{ source: "/:path*", headers: [
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "X-Frame-Options", value: "DENY" },
      { key: "Referrer-Policy", value: "same-origin" },
    ] }];
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.BACKEND_URL || "http://backend:8000"}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
