import type { Metadata, Viewport } from "next";
import { RegisterServiceWorker } from "@/components/pwa";
import "./globals.css";

export const metadata: Metadata = {
  title: "StepToLead — понятная экономика роста",
  description: "Оцените экономику бизнеса, запас прочности и готовность к тестированию маркетинга.",
  manifest: "/manifest.webmanifest",
  appleWebApp: { capable: true, title: "StepToLead", statusBarStyle: "default" },
  icons: { icon: [{ url: "/favicon.ico?v=4", sizes: "any" }, { url: "/favicon-32.png?v=4", type: "image/png", sizes: "32x32" }, { url: "/favicon-16.png?v=4", type: "image/png", sizes: "16x16" }, { url: "/favicon-stl.png?v=4", type: "image/png", sizes: "192x192" }], shortcut: "/favicon.ico?v=4", apple: "/apple-touch-icon.png?v=4" },
};

export const viewport: Viewport = { themeColor: "#006bfd", width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ru">
      <body>
        <RegisterServiceWorker/>
        {children}
      </body>
    </html>
  );
}
