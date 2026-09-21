"use client";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Icon } from "../ui/Icon.tsx";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

/**
 * The narrow-screen navigation drawer. It traps focus while open, closes on Escape, on a
 * click outside and on a route change, returns focus to the control that opened it, and
 * stops the page behind it from scrolling.
 */
export function MobileNav({ openLabel, closeLabel, title, children }: {
  openLabel: string; closeLabel: string; title: string; children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const drawer = useRef<HTMLDivElement>(null);
  const pathname = usePathname();
  const panelId = useId();

  // Closing always hands focus back to the control that opened the drawer, whether the
  // reader pressed Escape, clicked outside, or used the close button.
  const dismiss = useCallback(() => {
    setOpen(false);
    trigger.current?.focus();
  }, []);

  // A navigation inside the drawer has done its job; the drawer gets out of the way.
  useEffect(() => { setOpen(false); }, [pathname]);

  useEffect(() => {
    if (!open) return;
    document.body.classList.add("drawer-open");
    const first = drawer.current?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus();

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); dismiss(); return; }
      if (event.key !== "Tab" || !drawer.current) return;
      const items = [...drawer.current.querySelectorAll<HTMLElement>(FOCUSABLE)]
        .filter(item => item.offsetParent !== null);
      if (items.length === 0) return;
      const edge = event.shiftKey ? items[0] : items[items.length - 1];
      if (document.activeElement === edge) {
        event.preventDefault();
        (event.shiftKey ? items[items.length - 1] : items[0]).focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.classList.remove("drawer-open");
    };
  }, [open, dismiss]);

  return <>
    <button
      ref={trigger}
      type="button"
      className="icon-button"
      aria-label={openLabel}
      aria-expanded={open}
      aria-controls={panelId}
      onClick={() => setOpen(true)}
    ><Icon name="menu" size={22}/></button>
    {open && <>
      <div className="drawer-overlay" onClick={dismiss} aria-hidden="true"/>
      <div
        ref={drawer}
        id={panelId}
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="drawer-head">
          <p className="sidebar-label">{title}</p>
          <button type="button" className="icon-button" aria-label={closeLabel} onClick={dismiss}>
            <Icon name="close" size={20}/>
          </button>
        </div>
        {children}
      </div>
    </>}
  </>;
}
