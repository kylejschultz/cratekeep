import { useSyncExternalStore } from "react";
import { ApiError, api, activityEnabled, activityRunning, type ActivityJob } from "./api";

let state: { jobs: ActivityJob[]; error: string } = { jobs: [], error: "" };
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setTimeout> | undefined;
let polling = false;
async function poll() {
  if (!activityEnabled || polling || !listeners.size) return;
  polling = true;
  try {
    const result = await api<{ jobs: ActivityJob[] }>("/api/activity");
    // A busy interval can move a completed run beyond the bounded recent feed.
    // Recover only previously active IDs; history itself is loaded separately.
    const missing = state.jobs.filter(job => activityRunning(job) && !result.jobs.some(next => next.id === job.id));
    const recovered = await Promise.all(missing.map(async job => {
      try { return await api<ActivityJob>(`/api/activity/${job.id}`); }
      catch (reason) {
        // Only automatic successful no-arrival scans are removed by compaction.
        // Recovering their completion still refreshes the queue and clears busy UI.
        if (job.automatic_scan && reason instanceof ApiError && reason.status === 404) return { ...job, status: "coalesced" };
        throw reason;
      }
    }));
    const jobs = [...result.jobs, ...recovered];
    const ended = jobs.filter(job => !activityRunning(job) && state.jobs.some(previous => previous.id === job.id && activityRunning(previous)));
    state = { jobs, error: "" };
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
