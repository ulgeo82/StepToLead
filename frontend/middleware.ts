import { NextRequest, NextResponse } from "next/server";

export async function middleware(request: NextRequest) {
  const isPortal = request.nextUrl.pathname.startsWith("/portal");
  if (request.nextUrl.pathname === "/portal/login") return NextResponse.next();
  const cookieName = isPortal ? "stl_portal_session" : "stl_session";
  const mePath = isPortal ? "/api/portal/auth/me" : "/api/auth/me";
  const loginPath = isPortal ? "/portal/login" : "/login";
  const token = request.cookies.get(cookieName)?.value;
  let authorized = false;
  if (token) {
    const response = await fetch(`${process.env.BACKEND_URL || "http://backend:8000"}${mePath}`, {
      headers: { Cookie: `${cookieName}=${token}` }, cache: "no-store", signal: AbortSignal.timeout(5000),
    }).catch(() => null);
    authorized = response?.ok === true;
  }
  if (!authorized) {
    const url = new URL(loginPath, request.url);
    url.searchParams.set("next", request.nextUrl.pathname);
    return NextResponse.redirect(url);
  }
  const response = NextResponse.next();
  response.headers.set("Cache-Control", "private, no-store");
  return response;
}
export const config = { matcher: ["/admin/:path*", "/portal/:path*"] };
