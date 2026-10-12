"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ProjectSidebar } from "@/components/reporting";
import { PushCard } from "@/components/pwa";
import { TelegramCard } from "@/components/telegram-card";
import "../result/result.css";
import "../settings/settings.css";

type Me = { display_name?: string; role?: string; role_name?: string };

// «Мои уведомления»: любой сотрудник сам подключает Telegram и пуши. Какие события ему приходят — настраивает
// собственник в «Настройки → Уведомления», а тихие часы и личные отключения — сам сотрудник в боте (/settings).
export default function ProfilePage() {
  const [me, setMe] = useState<Me | null>(null);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [telegramConfigured, setTelegramConfigured] = useState(false);
  useEffect(() => {
    setProjectId(Number(new URLSearchParams(window.location.search).get("project_id")) || null);
    api<Me>("/portal/auth/me").then(setMe).catch(() => setMe(null));
    api<{ configured: boolean }>("/portal/telegram").then(s => setTelegramConfigured(s.configured)).catch(() => setTelegramConfigured(false));
  }, []);
  return <div className="resultShell"><ProjectSidebar projectId={projectId} active="profile"/>
    <main className="resultMain settingsMain"><header className="resultHeader"><div><h1>Мои уведомления</h1>
      <p>{me?.display_name ? `${me.display_name}${me.role_name ? ` · ${me.role_name}` : ""}. ` : ""}Подключите Telegram, чтобы получать новые заявки, задачи и напоминания. Какие события приходят, настраивает руководитель; тихие часы и личные отключения — в боте, команда /settings.</p></div></header>
      <PushCard/><TelegramCard configured={telegramConfigured} onChanged={() => undefined}/>
    </main></div>;
}
