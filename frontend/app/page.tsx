import { cookies } from "next/headers";
import { redirect } from "next/navigation";

// Корень: агентство — в админку, клиент — в кабинет, иначе — вход клиента (/login). Сессию проверит middleware/страница.
export default async function Home() {
  const jar = await cookies();
  if (jar.get("stl_session")?.value) redirect("/admin");
  redirect(jar.get("stl_portal_session")?.value ? "/result" : "/login");
}
