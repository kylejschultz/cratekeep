import { useSyncExternalStore } from "react";
import { api, activityEnabled, activityRunning, type ActivityJob } from "./api";

let state: { jobs: ActivityJob[]; error: string } = { jobs: [], error: "" };
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setTimeout> | undefined;
let polling = false;
async function poll() {
  if (!activityEnabled || polling || !listeners.size) return;
  polling = true;
  try {
    const result = await api<{ jobs: ActivityJob[] }>("/api/activity");
    const ended = result.jobs.filter(job => !activityRunning(job) && state.jobs.some(previous => previous.id === job.id && activityRunning(previous)));
    state = { jobs: result.jobs, error: "" };
    ended.forEach(job => window.dispatchEvent(new CustomEvent("cratekeep:activity-finished", { detail: job })));
  } catch { state = { ...state, error: "Activity unavailable. Reconnecting…" }; }
  finally {
    polling = false; listeners.forEach(listener => listener());
    if (listeners.size) timer = setTimeout(() => void poll(), 1500);
  }
}
function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) void poll();
  return () => { listeners.delete(listener); if (!listeners.size) clearTimeout(timer); };
}
export function useActivity() { return useSyncExternalStore(subscribe, () => state); }
