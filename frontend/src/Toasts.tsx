import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { ToastContext, type Kind } from "./toastContext";
type Message = { id: number; text: string; kind: Kind };

function Toast({ message, dismiss, durationMs }: { message: Message; dismiss: (id: number) => void; durationMs: number }) {
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    if (paused) return;
    const timer = window.setTimeout(() => dismiss(message.id), durationMs);
    return () => window.clearTimeout(timer);
  }, [message.id, paused, dismiss, durationMs]);
  return <div className={`toast ${message.kind}`} role="status" aria-atomic="true"
    onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)}
    onFocus={() => setPaused(true)} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setPaused(false); }}>
    <span>{message.text}</span><button type="button" className="secondary" aria-label="Dismiss notification" onClick={() => dismiss(message.id)}>×</button>
  </div>;
}

export function ToastProvider({ children, durationSeconds = 5 }: { children: ReactNode; durationSeconds?: number }) {
  const durationMs = (Number.isFinite(durationSeconds) && durationSeconds >= 1 && durationSeconds <= 300 ? durationSeconds : 5) * 1000;
  const [messages, setMessages] = useState<Message[]>([]);
  const sequence = useRef(0);
  const notify = useCallback((text: string, kind: Kind = "success") => {
    const message = { id: ++sequence.current, text, kind };
    setMessages(current => [...current.slice(-2), message]);
  }, []);
  const dismiss = useCallback((id: number) => setMessages(current => current.filter(message => message.id !== id)), []);
  return <ToastContext.Provider value={notify}>{children}{createPortal(
    <section className="toast-region" aria-label="Notifications">{messages.map(message => <Toast key={message.id} message={message} dismiss={dismiss} durationMs={durationMs}/>)}</section>, document.body,
  )}</ToastContext.Provider>;
}
