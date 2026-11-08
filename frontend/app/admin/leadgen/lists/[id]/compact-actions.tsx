"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { Popover } from "./popover";
import css from "./workspace.module.css";

export type WorkspaceAction = { label: string; disabled: boolean; click: () => void; danger?: boolean };

export function CompactActions({ items }: { items: WorkspaceAction[] }) {
  const root = useRef<HTMLDivElement>(null);
  const measure = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(0);
  useLayoutEffect(() => {
    function resize() {
      const width = root.current?.clientWidth || 0;
      const widths = Array.from(measure.current?.children || [], child => child.getBoundingClientRect().width);
      let used = 82;
      let count = 0;
      if (window.innerWidth > 600) for (const item of widths) {
        if (used + item + 5 > width) break;
        used += item + 5;
        count++;
      }
      setVisible(count);
    }
    const observer = new ResizeObserver(resize);
    if (root.current) observer.observe(root.current);
    resize();
    window.addEventListener("resize", resize);
    return () => { observer.disconnect(); window.removeEventListener("resize", resize); };
  }, [items.length]);
  return <div ref={root} className={css.actions} role="group" aria-label="Действия с компаниями">
    <div ref={measure} className={css.actionMeasure} aria-hidden="true">{items.map(item =>
      <span className={css.smallButton} key={item.label}>{item.label}</span>)}</div>
    {items.slice(0, visible).map(item => <button key={item.label} type="button" disabled={item.disabled}
      className={`${css.smallButton} ${item.danger ? css.danger : ""}`} onClick={item.click}>{item.label}</button>)}
    {visible < items.length && <Popover label="Действия" trigger={<>
      <span className={css.desktopMore}>Ещё ▾</span><span className={css.mobileMore}>Действия ▾</span>
    </>} align="end">{close => <div className={css.menuItems}>{items.slice(visible).map(item =>
      <button key={item.label} type="button" disabled={item.disabled}
        className={item.danger ? css.danger : ""} onClick={() => { close(); item.click(); }}>{item.label}</button>)}</div>}
    </Popover>}
  </div>;
}
