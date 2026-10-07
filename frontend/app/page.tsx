import { cookies } from "next/headers";
import { redirect } from "next/navigation";

// Корень ведёт на вход; если сессия уже есть — сразу в кабинет (middleware проверит её и вернёт на /login, если она недействительна).
export default async function Home() {
  const session = (await cookies()).get("stl_session")?.value;
  redirect(session ? "/admin" : "/login");
}
