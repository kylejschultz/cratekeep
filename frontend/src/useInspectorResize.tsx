import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";

const storageKey = "cratekeep-inspector-width";
const defaultWidth = 420;
const minimum = 320;

export function useInspectorResize() {
  const workspace = useRef<HTMLDivElement>(null);
  const [maximum, setMaximum] = useState(720);
  const [preferred, setPreferred] = useState(() => {
    try { const saved = Number(localStorage.getItem(storageKey)); return Number.isFinite(saved) && saved >= minimum ? saved : defaultWidth; }
    catch { return defaultWidth; }
  });
  const width = Math.max(minimum, Math.min(preferred, maximum));
  useEffect(() => {
    const node = workspace.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => setMaximum(Math.max(minimum, Math.min(720, node.clientWidth - 404))));
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  const update = (value: number) => {
    const next = Math.max(minimum, Math.min(value, maximum));
    setPreferred(next);
    try { localStorage.setItem(storageKey, String(next)); } catch { /* Resizing still works without browser storage. */ }
  };
  const divider = <div className="inspector-divider" role="separator" tabIndex={0} aria-label="Resize album details" aria-orientation="vertical" aria-valuemin={minimum} aria-valuemax={maximum} aria-valuenow={Math.round(width)} aria-valuetext={`${Math.round(width)} pixels wide`} aria-description="Use left and right arrows to resize. Home or double-click resets the width." title="Drag to resize · Double-click to reset"
    onPointerDown={event => { if (event.button === 0) { event.preventDefault(); event.currentTarget.focus(); event.currentTarget.setPointerCapture(event.pointerId); } }}
    onPointerMove={event => { if (event.currentTarget.hasPointerCapture(event.pointerId)) { const right = workspace.current?.getBoundingClientRect().right; if (right != null) update(right - event.clientX); } }}
    onPointerUp={event => { if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); }}
    onDoubleClick={() => update(defaultWidth)}
    onKeyDown={event => { if (["ArrowLeft", "ArrowRight", "Home"].includes(event.key)) { event.preventDefault(); update(event.key === "Home" ? defaultWidth : width + (event.key === "ArrowLeft" ? 1 : -1) * (event.shiftKey ? 50 : 10)); } }}/>;
  return { workspace, divider, style: { "--inspector-width": `${width}px` } as CSSProperties };
}
