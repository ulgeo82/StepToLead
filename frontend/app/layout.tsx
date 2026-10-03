import type { Metadata, Viewport } from "next";
import { RegisterServiceWorker } from "@/components/pwa";
import "./globals.css";

export const metadata: Metadata = {
  title: "StepToLead — понятная экономика роста",
  description: "Оцените экономику бизнеса, запас прочности и готовность к тестированию маркетинга.",
  manifest: "/manifest.webmanifest",
  appleWebApp: { capable: true, title: "StepToLead", statusBarStyle: "default" },
  icons: { icon: "/icon-192.png", apple: "/apple-touch-icon.png" },
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
