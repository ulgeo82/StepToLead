/* StepToLead site widget: «call me back» form + messenger buttons. Install:
   <script async src="https://portal.steptolead.ru/stl-widget.js" data-stl-key="SITE_KEY"></script>
   Works with or without stl.js; when the visitor agreed to analytics, the lead is linked to the visit. */
(() => {
  "use strict";
  const script = document.currentScript;
  const key = script?.getAttribute("data-stl-key");
  if (!key || window.__stlWidget) return;
  window.__stlWidget = true;
  const base = new URL(`/api/website/widget/${encodeURIComponent(key)}`, script.src).href;
  const params = new URLSearchParams(location.search);
  const memo = "stl:widget:attr";
  // First-touch attribution of this tab: UTM and click ids survive navigation inside the site.
  let attr = {};
  try { attr = JSON.parse(sessionStorage.getItem(memo) || "{}"); } catch {}
  if (params.get("utm_source") || params.get("yclid") || params.get("gclid") || params.get("vkclid")) {
    attr = {};
    for (const f of ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"]) if (params.get(f)) attr[f] = params.get(f).slice(0, 255);
    for (const f of ["yclid", "gclid", "vkclid"]) if (params.get(f)) { attr.click_id = params.get(f).slice(0, 255); attr.click_type = f; break; }
    try { sessionStorage.setItem(memo, JSON.stringify(attr)); } catch {}
  }
  const esc = (s) => String(s || "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const track = (name) => { try { window.StepToLead?.track("cta_click", { element_name: `Виджет: ${name}` }); } catch {} };

  fetch(base, { credentials: "omit" }).then(r => r.ok ? r.json() : null).then(conf => { if (conf) setTimeout(() => render(conf), (conf.delay_sec || 0) * 1000); }).catch(() => {});

  function render(c) {
    const host = document.createElement("div");
    host.style.cssText = "position:fixed;z-index:2147483000;bottom:0;" + (c.position === "left" ? "left:0" : "right:0");
    const root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;
    const side = c.position === "left" ? "left" : "right";
    const links = [
      c.whatsapp && { name: "WhatsApp", href: `https://wa.me/${c.whatsapp}`, bg: "#25D366", icon: "M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 4v-4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zm3 6.5a1.5 1.5 0 1 0 0-.01zm5 0a1.5 1.5 0 1 0 0-.01zm5 0a1.5 1.5 0 1 0 0-.01z" },
      c.telegram && { name: "Telegram", href: `https://t.me/${c.telegram}`, bg: "#229ED9", icon: "M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 4v-4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zm3 6.5a1.5 1.5 0 1 0 0-.01zm5 0a1.5 1.5 0 1 0 0-.01zm5 0a1.5 1.5 0 1 0 0-.01z" },
      c.max && { name: "MAX", href: c.max, bg: "#6E46F5", icon: "M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 4v-4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zm3 6.5a1.5 1.5 0 1 0 0-.01zm5 0a1.5 1.5 0 1 0 0-.01zm5 0a1.5 1.5 0 1 0 0-.01z" },
      c.phone && { name: "Позвонить", href: `tel:${c.phone}`, bg: "#0F2140", icon: "M6.6 10.8a15 15 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1z" },
    ].filter(Boolean);
    const policy = c.privacy_url ? `<a href="${esc(c.privacy_url)}" target="_blank" rel="noopener">обработку персональных данных</a>` : "обработку персональных данных";
    root.innerHTML = `<style>
      :host{all:initial}*{box-sizing:border-box;font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
      .fab{position:fixed;bottom:20px;${side}:20px;width:60px;height:60px;border-radius:50%;border:0;background:${c.color};color:#fff;cursor:pointer;box-shadow:0 10px 30px rgba(15,33,64,.28);display:grid;place-items:center;animation:pulse 2.4s infinite}
      .fab svg{width:28px;height:28px;fill:#fff}
      @keyframes pulse{0%{box-shadow:0 0 0 0 ${c.color}66}70%{box-shadow:0 0 0 16px ${c.color}00}100%{box-shadow:0 0 0 0 ${c.color}00}}
      .panel{position:fixed;bottom:92px;${side}:20px;width:330px;max-width:calc(100vw - 32px);background:#fff;color:#14233c;border-radius:16px;box-shadow:0 18px 50px rgba(15,33,64,.25);padding:18px;display:none}
      .panel.open{display:block}
      h3{margin:0 24px 4px 0;font-size:17px;line-height:1.3}p{margin:0 0 12px;font-size:13px;line-height:1.45;color:#5d6f8a}
      .x{position:absolute;top:10px;right:10px;width:28px;height:28px;border:0;border-radius:8px;background:#f1f4f9;color:#5d6f8a;font-size:18px;cursor:pointer}
      input{width:100%;height:44px;margin:0 0 8px;padding:0 12px;border:1px solid #d5deea;border-radius:10px;font-size:15px;color:#14233c;background:#fff}
      input:focus{outline:2px solid ${c.color}55;border-color:${c.color}}
      .hp{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}
      label.ok{display:flex;gap:8px;align-items:flex-start;font-size:11px;line-height:1.4;color:#5d6f8a;margin:2px 0 10px}
      label.ok input{width:16px;height:16px;margin:1px 0 0;flex:none}label.ok a{color:${c.color}}
      .send{width:100%;height:46px;border:0;border-radius:10px;background:${c.color};color:#fff;font-size:15px;font-weight:600;cursor:pointer}
      .send:disabled{opacity:.6}
      .err{color:#c8344b;font-size:12px;margin:0 0 8px;min-height:0}
      .done{padding:18px 0 8px;text-align:center;font-size:15px;line-height:1.45}
      .links{display:flex;gap:8px;flex-wrap:wrap;margin-top:${c.callback ? "12px" : "0"}}
      .links a{flex:1 1 40%;display:flex;align-items:center;justify-content:center;gap:6px;height:40px;border-radius:10px;color:#fff;text-decoration:none;font-size:13px;font-weight:600}
      .links svg{width:18px;height:18px;fill:#fff}
      .or{font-size:11px;color:#9aa8bb;text-align:center;margin-top:12px;display:${c.callback && links.length ? "block" : "none"}}
      @media(max-width:480px){.panel{bottom:86px;${side}:12px}.fab{bottom:14px;${side}:14px}}
    </style>
    <button class="fab" aria-label="${esc(c.title)}"><svg viewBox="0 0 24 24"><path d="M6.6 10.8a15 15 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1z"/></svg></button>
    <div class="panel" role="dialog" aria-label="${esc(c.title)}"><button class="x" aria-label="Закрыть">×</button>
      <h3>${esc(c.title)}</h3>${c.text ? `<p>${esc(c.text)}</p>` : ""}
      ${c.callback ? `<form novalidate><input name="name" autocomplete="name" placeholder="Имя" maxlength="120">
        <input name="phone" type="tel" autocomplete="tel" inputmode="tel" placeholder="+7 (___) ___-__-__" required maxlength="32">
        <input class="hp" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">
        <label class="ok"><input type="checkbox" name="consent"> <span>Согласен на ${policy}</span></label>
        <div class="err" aria-live="polite"></div><button class="send">${esc(c.button)}</button></form>` : ""}
      <div class="or">или напишите нам</div>
      <div class="links">${links.map(l => `<a href="${esc(l.href)}" target="_blank" rel="noopener" data-n="${esc(l.name)}" style="background:${l.bg}"><svg viewBox="0 0 24 24"><path d="${l.icon}"/></svg>${esc(l.name)}</a>`).join("")}</div>
    </div>`;
    document.body.appendChild(host);
    const panel = root.querySelector(".panel");
    const toggle = (open) => { panel.classList.toggle("open", open); if (open) { track("открыт"); root.querySelector("input[name=phone]")?.focus(); } };
    root.querySelector(".fab").addEventListener("click", () => toggle(!panel.classList.contains("open")));
    root.querySelector(".x").addEventListener("click", () => toggle(false));
    root.querySelectorAll(".links a").forEach(a => a.addEventListener("click", () => track(a.dataset.n)));
    const form = root.querySelector("form");
    if (!form) return;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const err = form.querySelector(".err"), send = form.querySelector(".send");
      const f = new FormData(form);
      const phone = String(f.get("phone") || "");
      if (phone.replace(/\D/g, "").length < 10) { err.textContent = "Введите номер телефона"; return; }
      if (!f.get("consent")) { err.textContent = "Отметьте согласие на обработку данных"; return; }
      err.textContent = ""; send.disabled = true;
      let ctx = null; try { ctx = window.StepToLead?.context?.() || null; } catch {}
      const body = { ...attr, name: String(f.get("name") || ""), phone, consent: true, website: String(f.get("website") || ""),
        page: location.href.split("#")[0].slice(0, 1500), website_session_key: ctx?.website_session_key || undefined };
      try {
        const r = await fetch(`${base}/lead`, { method: "POST", credentials: "omit", headers: { "Content-Type": "text/plain" }, body: JSON.stringify(body) });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail && typeof data.detail === "string" ? data.detail : "Не получилось отправить. Позвоните нам или напишите.");
        try { window.StepToLead?.track("form_success", { element_name: "Виджет: обратный звонок" }); } catch {}
        form.outerHTML = `<div class="done">✓ ${esc(data.message || c.success)}</div>`;
      } catch (e) { err.textContent = e.message; send.disabled = false; }
    });
  }
})();
