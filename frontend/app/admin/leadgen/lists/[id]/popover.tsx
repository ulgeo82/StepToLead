"use client";

import { ReactNode, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import css from "./workspace.module.css";

export function Popover({ label, trigger, children, className = "", buttonClass = "", align = "start" }: {
  label: string; trigger: ReactNode; children: ReactNode | ((close: () => void) => ReactNode);
  className?: string; buttonClass?: string; align?: "start" | "end";
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 8, maxHeight: 360, width: 290 });
  const button = useRef<HTMLButtonElement>(null);
  const popup = useRef<HTMLDivElement>(null);
  const id = useId();
  function close() { setOpen(false); button.current?.focus(); }
  useLayoutEffect(() => {
    if (!open) return;
    function place() {
      if (!button.current) return;
      const rect = button.current.getBoundingClientRect();
      const width = Math.min(290, window.innerWidth - 16);
      const below = window.innerHeight - rect.bottom - 12;
      const above = rect.top - 12;
      const useAbove = below < 200 && above > below;
      const maxHeight = Math.min(360, useAbove ? above : below);
      const height = Math.min(popup.current?.scrollHeight || 360, maxHeight);
      setPosition({ width, maxHeight: Math.max(80, maxHeight),
        top: useAbove ? Math.max(8, rect.top - height - 6) : rect.bottom + 6,
        left: Math.max(8, Math.min(window.innerWidth - width - 8,
          align === "end" ? rect.right - width : rect.left)) });
    }
    place();
    const observer = new ResizeObserver(place);
    if (popup.current) observer.observe(popup.current);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, align]);
  useEffect(() => {
    if (!open) return;
    const first = popup.current?.querySelector<HTMLElement>("input:not([disabled]), select:not([disabled])")
      || popup.current?.querySelector<HTMLElement>("button:not([disabled])");
    first?.focus();
    function outside(event: MouseEvent) {
      const target = event.target as Node;
      if (!button.current?.contains(target) && !popup.current?.contains(target)) setOpen(false);
    }
    function key(event: KeyboardEvent) { if (event.key === "Escape") { event.preventDefault(); close(); } }
    function focus(event: FocusEvent) {
      const target = event.target as Node;
      if (!button.current?.contains(target) && !popup.current?.contains(target)) setOpen(false);
    }
    document.addEventListener("mousedown", outside);
    document.addEventListener("keydown", key);
    document.addEventListener("focusin", focus);
    return () => {
      document.removeEventListener("mousedown", outside);
      document.removeEventListener("keydown", key);
      document.removeEventListener("focusin", focus);
    };
  }, [open]);
  return <span className={className}>
    <button ref={button} type="button" className={`${css.smallButton} ${buttonClass}`} aria-label={label}
      aria-expanded={open} aria-controls={open ? id : undefined} aria-haspopup="dialog"
      onClick={() => setOpen(value => !value)}>{trigger}</button>
    {open && createPortal(<div ref={popup} id={id} role="dialog" aria-label={label}
      className={css.popover} style={position}>
      <div className={css.popoverHeading}><b>{label}</b><button type="button" aria-label={`Закрыть: ${label}`}
        onClick={close}>×</button></div>
      {typeof children === "function" ? children(close) : children}
    </div>, document.body)}
  </span>;
}
