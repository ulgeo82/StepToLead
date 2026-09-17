import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "StepToLead — понятная экономика роста",
  description: "Оцените экономику бизнеса, запас прочности и готовность к тестированию маркетинга.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ru">
      <body>
        {children}
      </body>
    </html>
  );
}
