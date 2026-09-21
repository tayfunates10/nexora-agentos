"use client";
import { useEffect, useRef } from "react";

/**
 * Brings a section in as it approaches the viewport, once. The hidden starting state is
 * declared in CSS behind html[data-motion="on"], which only the pre-paint script sets, so
 * a reader without JavaScript — or with reduced motion asked for — always sees the
 * content rather than an empty frame waiting for an observer that never arrives.
 */
export function Reveal({ className, children }: { className?: string; children: React.ReactNode }) {
  const node = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = node.current;
    if (!element) return;
    const reveal = () => element.classList.add("is-visible");
    if (typeof IntersectionObserver === "undefined") { reveal(); return; }
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        reveal();
        observer.disconnect();
      }
    }, { threshold: 0.12 });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  return <div ref={node} className={"reveal" + (className ? " " + className : "")}>{children}</div>;
}
