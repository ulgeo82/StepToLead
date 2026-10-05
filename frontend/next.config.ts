import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // Запись встречи: STT нескольких аудиофрагментов и разбор брифа могут занять минуты.
  // Обычные запросы по-прежнему ограничены таймаутом api(), загрузка записи — 30 минут.
  experimental: { proxyTimeout: 1_800_000 },
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
      // Commercial offers / invoices opened by clients: short public link.
      { source: "/d/:token", destination: `${process.env.BACKEND_URL || "http://backend:8000"}/api/public/documents/:token` },
      {
        source: "/api/:path*",
        destination: `${process.env.BACKEND_URL || "http://backend:8000"}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
