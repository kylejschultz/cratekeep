import { useEffect, useState } from "react";

export const PROGRESS_DELAY_MS = 3000;

// A job already running elsewhere should not restart the buffer when opened.
export function useProgressDelay(running: boolean, key: string, createdAt?: number) {
  const [readyKey, setReadyKey] = useState<string | null>(null);
  const token = `${key}:${createdAt ?? ""}`;
  useEffect(() => {
    if (!running) return;
    const delay = createdAt ? Math.max(0, PROGRESS_DELAY_MS - (Date.now() - createdAt * 1000)) : PROGRESS_DELAY_MS;
    const timer = setTimeout(() => setReadyKey(token), delay);
    return () => clearTimeout(timer);
  }, [running, token, createdAt]);
  return running && readyKey === token;
}
