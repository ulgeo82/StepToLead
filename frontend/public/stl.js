/* StepToLead first-party site tracker. No collection before explicit consent. */
(() => {
  "use strict";
  const script = document.currentScript;
  const key = script?.getAttribute("data-stl-key");
  if (!key) return;
  const endpoint = new URL(`/api/website/collect/${encodeURIComponent(key)}`, script.src).href;
  const random = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`);
  const visitorStorage = `stl:${key}:visitor`;
  const sessionStorageKey = `stl:${key}:session`;
  const activityStorageKey = `stl:${key}:activity`;
  let visitor = null;
  let session = null;
  let newVisitor = false;
  let consent = false;
  let queue = [];
  let lastPath = "";
  let lastActivity = 0;
  const params = new URLSearchParams(location.search);
  const attribution = {};
  for (const field of ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"])
    if (params.get(field)) attribution[field] = params.get(field).slice(0, 500);
  for (const field of ["yclid", "gclid", "vkclid"]) if (params.get(field)) {
    attribution.click_id = params.get(field).slice(0, 255); attribution.click_type = field; break;
  }
  // Yandex Metrica ClientId: lets sales from the CRM go back to Metrica/Direct as offline conversions.
  const metricaClient = () => { const m = document.cookie.match(/(?:^|;\s*)_ym_uid=(\d{6,32})/); return m ? m[1] : undefined; };
  if (params.get("stl_connection_id")) attribution.connection_id = Number(params.get("stl_connection_id")) || null;
  if (params.get("stl_campaign_id")) attribution.external_campaign_id = params.get("stl_campaign_id").slice(0, 180);
  const page = () => `${location.origin}${location.pathname}`;
  const elementName = (node) => node.getAttribute("data-stl-name") || node.getAttribute("data-stl-event") || null;
  const device = () => /iPad|Tablet/i.test(navigator.userAgent) ? "tablet" : /Mobi|Android/i.test(navigator.userAgent) ? "mobile" : "desktop";
  const browser = () => /Firefox/i.test(navigator.userAgent) ? "firefox" : /Edg\//i.test(navigator.userAgent) ? "edge" : /Chrome/i.test(navigator.userAgent) ? "chrome" : /Safari/i.test(navigator.userAgent) ? "safari" : "other";
  const os = () => /Android/i.test(navigator.userAgent) ? "android" : /iPhone|iPad/i.test(navigator.userAgent) ? "ios" : /Windows/i.test(navigator.userAgent) ? "windows" : /Mac OS/i.test(navigator.userAgent) ? "macos" : /Linux/i.test(navigator.userAgent) ? "linux" : "other";
  function flush() {
    if (!consent || !queue.length) return;
    const events = queue.splice(0, 20);
    const data = JSON.stringify({ visitor, session, new_visitor: newVisitor, referrer: document.referrer || null, device: device(), browser: browser(), os: os(), ...attribution, ym_client_id: metricaClient(), events });
    const blob = new Blob([data], { type: "text/plain" });
    if (navigator.sendBeacon && navigator.sendBeacon(endpoint, blob)) return;
    fetch(endpoint, { method: "POST", mode: "cors", credentials: "omit", headers: { "Content-Type": "text/plain" }, body: data, keepalive: true }).catch(() => {});
  }
  function track(name, options = {}) {
    if (!consent) return;
    if (Date.now() - lastActivity > 30 * 60 * 1000) {
      session = random();
      try { sessionStorage.setItem(sessionStorageKey, session); } catch {}
    }
    lastActivity = Date.now();
    try { sessionStorage.setItem(activityStorageKey, String(lastActivity)); } catch {}
    const event = { key: random(), name, page: page() };
    if (options.element_id) event.element_id = String(options.element_id).slice(0, 100);
    if (options.element_name) event.element_name = String(options.element_name).slice(0, 180);
    if (options.depth) event.depth = options.depth;
    queue.push(event);
    if (queue.length >= 10) flush();
  }
  function view() {
    const current = page();
    if (current === lastPath) return;
    lastPath = current;
    track("page_view");
    observe();
  }
  function observe() {
    if (!window.IntersectionObserver || !consent) return;
    document.querySelectorAll("[data-stl-section], [data-stl-view], form[data-stl-form]").forEach(node => observer.observe(node));
  }
  const seen = new Set();
  const observer = window.IntersectionObserver ? new IntersectionObserver(entries => entries.forEach(entry => {
    if (!entry.isIntersecting || seen.has(entry.target)) return;
    seen.add(entry.target); observer.unobserve(entry.target);
    const node = entry.target;
    const selected = node.getAttribute("data-stl-event");
    const name = node.matches("form") ? "form_view" : node.hasAttribute("data-stl-section") ? "section_view" : ["cta_view", "service_view", "case_view", "pricing_view"].includes(selected) ? selected : "cta_view";
    track(name, { element_id: node.id || null, element_name: elementName(node) });
  }), { threshold: 0.25 }) : { observe() {}, unobserve() {} };
  const depths = new Set();
  let scrollQueued = false;
  addEventListener("scroll", () => {
    if (!consent || scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => {
      scrollQueued = false;
      const max = document.documentElement.scrollHeight - innerHeight;
      const depth = max > 0 ? Math.round(scrollY / max * 100) : 100;
      for (const mark of [25, 50, 75, 90, 100]) if (depth >= mark && !depths.has(mark)) { depths.add(mark); track("scroll_depth", { depth: mark }); }
    });
  }, { passive: true });
  document.addEventListener("click", event => {
    if (!consent) return;
    const node = event.target.closest("[data-stl-event], a[href^='tel:'], a[href*='t.me/'], a[href*='wa.me/']");
    if (!node) return;
    const href = node.getAttribute("href") || "";
    const declared = node.getAttribute("data-stl-event");
    const name = declared && ["cta_click", "phone_click", "messenger_click", "service_click", "case_open", "pricing_open"].includes(declared) ? declared :
      href.startsWith("tel:") ? "phone_click" : /t\.me|wa\.me/.test(href) ? "messenger_click" : "cta_click";
    track(name, { element_id: node.id || null, element_name: elementName(node) });
  });
  document.addEventListener("focusin", event => {
    const form = event.target.closest?.("form[data-stl-form]");
    if (form && !form.dataset.stlStarted) { form.dataset.stlStarted = "1"; track("form_start", { element_name: elementName(form) }); }
  });
  document.addEventListener("invalid", event => {
    const form = event.target.closest?.("form[data-stl-form]");
    if (form) track("form_error", { element_name: elementName(form) });
  }, true);
  document.addEventListener("submit", event => {
    const form = event.target.closest?.("form[data-stl-form]");
    if (form) { track("form_submit", { element_name: elementName(form) }); flush(); }
  });
  for (const method of ["pushState", "replaceState"]) {
    const original = history[method];
    history[method] = function (...args) { const result = original.apply(this, args); queueMicrotask(view); return result; };
  }
  addEventListener("popstate", view);
  addEventListener("pagehide", flush);
  setInterval(flush, 5000);
  window.StepToLead = {
    consent(granted) {
      consent = granted === true;
      if (!consent) { queue = []; visitor = null; session = null; try { localStorage.removeItem(visitorStorage); sessionStorage.removeItem(sessionStorageKey); sessionStorage.removeItem(activityStorageKey); } catch {} return; }
      try {
        visitor = localStorage.getItem(visitorStorage);
        newVisitor = !visitor;
        if (!visitor) { visitor = random(); localStorage.setItem(visitorStorage, visitor); }
        const previousActivity = Number(sessionStorage.getItem(activityStorageKey)) || 0;
        session = Date.now() - previousActivity < 30 * 60 * 1000 ? sessionStorage.getItem(sessionStorageKey) || random() : random();
        sessionStorage.setItem(sessionStorageKey, session);
      } catch { visitor = random(); session = random(); newVisitor = true; }
      lastActivity = Date.now(); view();
    },
    track(name, options) { if (["service_view", "case_view", "pricing_view", "service_click", "case_open", "pricing_open", "cta_view", "cta_click", "form_success"].includes(name)) track(name, options); },
    context() { return consent ? { website_session_key: session, ...attribution, landing_url: page() } : null; },
    flush,
  };
})();
