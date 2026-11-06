import { useEffect, useRef, useState } from "react";
import { api } from "./api";

const dismissalKey = "cratekeep-analytics-dismissed";
function wasDismissed() {
  try { return localStorage.getItem(dismissalKey) === "1"; }
  catch { return false; }
}

export function AnalyticsBanner() {
  const [dismissed, setDismissed] = useState(wasDismissed);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);
  useEffect(() => {
    const sync = () => setDismissed(wasDismissed());
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);
  function dismiss() {
    try { localStorage.setItem(dismissalKey, "1"); } catch { /* Still dismiss this page when storage is unavailable. */ }
    setDismissed(true);
  }
  async function enable() {
    if (pending.current) return;
    pending.current = true; setSaving(true); setError("");
    try {
      await api("/api/analytics/enable", { method: "POST", body: "{}" });
      dismiss();
    } catch {
      setError("Couldn’t enable analytics. Please try again.");
    } finally { pending.current = false; setSaving(false); }
  }
  if (dismissed) return null;
  return <aside className="analytics-banner" aria-labelledby="analytics-invitation-title">
    <div className="analytics-banner-copy">
      <strong id="analytics-invitation-title">Help improve Cratekeep</strong>
      <p>Share anonymous install details and library totals with me through Beacon. No music names, paths or listening history are sent.</p>
      {error && <p role="alert">{error}</p>}
    </div>
    <div className="analytics-banner-actions">
      <a href="/settings#about">What’s shared</a>
      <button type="button" disabled={saving} onClick={dismiss}>No thanks</button>
      <button type="button" disabled={saving} onClick={() => void enable()}>{saving ? "Enabling…" : "Share analytics"}</button>
    </div>
  </aside>;
}
