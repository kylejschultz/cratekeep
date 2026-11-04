import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ActivityProgress } from "./ActivityProgress";
import { ActivityMonitor, ActivityInline } from "./Activity";
import { configureActivity, api, startActivity } from "./api";

const job = { created_at: 1770000000, id: "job-1", label: "Refreshing files", scope: "Library", href: "/library-import", status: "running", stage: "Finding files", files_found: 12, stage_done: 12, stage_total: null, stage_unit: "files", processed: 0, total: 1, rows: [] };
function mockHistory(current = job, count = 1, cleared = false) {
  const group = { key: "refresh-group", label: current.label, scope: current.scope, count, active: current.status === "running" ? 1 : 0, attention: 0, finished: current.status === "running" ? count - 1 : count, timestamp: current.created_at, latest: current };
  return { groups: cleared ? [] : [group], clearable: group.finished, snapshot: 30, next_before: null };
}
function activityFetch(current = job) {
  return async (url: string) => ({ ok: true, json: async () => url.startsWith("/api/activity/history") ? mockHistory(current) : url === "/api/activity" ? { jobs: [current] } : current });
}
afterEach(() => { cleanup(); configureActivity(false); vi.unstubAllGlobals(); vi.useRealTimers(); });

it("shows the same live job in the header and relevant page without inventing a total", async () => {
  configureActivity(true);
  vi.stubGlobal("fetch", vi.fn(activityFetch()));
  render(<><ActivityMonitor/><ActivityInline scope="library"/></>);
  const activity = await screen.findByRole("button", { name: "Activity (1)" });
  expect(activity).toHaveClass("is-running");
  expect(activity).toHaveAttribute("title", "Activity: 1 running");
  fireEvent.click(activity);
  await waitFor(() => expect(screen.getAllByText(/12 tracks found/)).toHaveLength(2));
  expect(screen.getAllByRole("progressbar").every(bar => !bar.hasAttribute("value"))).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "View activity" }));
  expect(await screen.findByRole("dialog", { name: "Refreshing files" })).toBeInTheDocument();
});

it("submits work once and polls server results instead of repeating a mutation", async () => {
  configureActivity(true); vi.useFakeTimers();
  let reads = 0;
  const fetch = vi.fn(async (url: string, init?: RequestInit) => ({ ok: true, json: async () => init?.method === "POST" ? job : { ...job, status: ++reads > 1 ? "complete" : "running", http_status: 200, result: { files: 12 } } }));
  vi.stubGlobal("fetch", fetch);
  const operation = api<any>("/api/library-import/queue/refresh", { method: "POST", body: "{}" });
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(await operation).toEqual({ files: 12 });
  expect(fetch.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
  expect(fetch.mock.calls[0][0]).toBe("/api/activity");
});

it("sends a complete confirmed batch so its remaining albums are server-owned", async () => {
  const fetch = vi.fn(async () => ({ ok: true, json: async () => job }));
  vi.stubGlobal("fetch", fetch);
  const steps = [1, 2].map(id => ({ path: "/api/library-import/bulk/execute", id, body: { album_ids: [id], confirmed: true } }));
  await startActivity(steps, "import");
  expect(JSON.parse(String((fetch.mock.calls[0] as unknown as [string, RequestInit])[1].body))).toMatchObject({ mode: "import", steps });
});

it("keeps completed activity compact with an inline dismiss action and opens full details", async () => {
  configureActivity(true);
  vi.stubGlobal("fetch", vi.fn(activityFetch({ ...job, id: "completed-compact", status: "complete", stage: "Finished", files_found: 42 })));
  render(<ActivityMonitor/>);
  fireEvent.click(await screen.findByRole("button", { name: "Activity" }));
  expect(screen.getByRole("button", { name: "Activity" })).not.toHaveClass("is-running");
  expect(await screen.findByText("complete · 42 tracks found")).toBeInTheDocument();
  expect(screen.queryByText("Finished")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Clear Refreshing files" }).closest("article")).toBe(screen.getByText("complete · 42 tracks found").closest("article"));
  fireEvent.click(screen.getByRole("button", { name: /Refreshing files Library complete/ }));
  expect(await screen.findByRole("dialog", { name: "Refreshing files" })).toBeInTheDocument();
  expect(screen.getByText("Finished")).toBeInTheDocument();
});

it("groups repeated operations with timestamps and clears all finished history beyond the loaded page", async () => {
  configureActivity(true);
  const scan = { ...job, id: "scan-1", label: "Matching albums", href: "/library-import", scope: "Library", status: "complete", stage: "Finished" };
  let cleared = false;
  const fetch = vi.fn(async (url: string, init?: RequestInit) => ({ ok: true, json: async () => {
    if (url === "/api/activity/history/clear" && init?.method === "POST") { cleared = true; return { cleared: 120 }; }
    if (url.startsWith("/api/activity/history?")) return { groups: cleared ? [] : [{ key: "matching-group", label: "Matching albums", scope: "Library", count: 120, active: 0, attention: 1, finished: 120, timestamp: scan.created_at, latest: scan }], clearable: cleared ? 0 : 120, snapshot: 120, next_before: 100 };
    return url === "/api/activity" ? { jobs: [scan] } : scan;
  } }));
  vi.stubGlobal("fetch", fetch);
  render(<ActivityMonitor/>);
  fireEvent.click(await screen.findByRole("button", { name: "Activity" }));
  expect(await screen.findByText("×120")).toBeInTheDocument();
  expect(screen.getByText(/1 need attention/)).toBeInTheDocument();
  expect(document.querySelector("time")).toHaveAttribute("datetime", new Date(scan.created_at * 1000).toISOString());
  fireEvent.click(screen.getByRole("button", { name: "Clear all" }));
  expect(await screen.findByText("No recent activity.")).toBeInTheDocument();
  const clearCall = fetch.mock.calls.find(([url]) => url === "/api/activity/history/clear")!;
  expect(JSON.parse(String(clearCall[1]?.body))).toEqual({});
});

it("opens grouped history, pages older runs and preserves full run details", async () => {
  configureActivity(true);
  const scan = { ...job, label: "Matching albums", scope: "Library", status: "complete", stage: "Finished" };
  const fetch = vi.fn(async (url: string) => ({ ok: true, json: async () => {
    if (url.includes("group=matching-group")) return { jobs: [{ ...scan, id: url.includes("before=") ? "old-run" : "new-run" }], snapshot: 30, next_before: url.includes("before=") ? null : 10 };
    if (url.startsWith("/api/activity/history")) return { groups: [{ key: "matching-group", label: "Matching albums", scope: "Library", count: 30, active: 0, attention: 0, finished: 30, timestamp: scan.created_at, latest: scan }], clearable: 30, snapshot: 30, next_before: null };
    return url === "/api/activity" ? { jobs: [scan] } : { ...scan, id: "old-run", rows: [{ album: "Saved album", status: "imported", jobId: 42 }] };
  } }));
  vi.stubGlobal("fetch", fetch);
  render(<ActivityMonitor/>);
  fireEvent.click(await screen.findByRole("button", { name: "Activity" }));
  fireEvent.click(await screen.findByRole("button", { name: /^Matching albums/ }));
  expect(await screen.findByRole("dialog", { name: "Matching albums history" })).toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "Older" }));
  fireEvent.click(await screen.findByRole("button", { name: /Matching albums Library/ }));
  expect(await screen.findByText("Saved album")).toBeInTheDocument();
  expect(fetch.mock.calls.some(([url]) => url.includes("before=10") && url.includes("snapshot=30"))).toBe(true);
});

it("migrates known old dismissals to persisted clearing before removing the local preference", async () => {
  configureActivity(true);
  localStorage.setItem("cratekeep-dismissed-activity", JSON.stringify(["previously-dismissed"]));
  const fetch = vi.fn(async (url: string, init?: RequestInit) => ({ ok: true, json: async () => url.endsWith("/clear") && init?.method === "POST" ? { cleared: 1 } : url.startsWith("/api/activity/history") ? mockHistory(job) : { jobs: [job] } }));
  vi.stubGlobal("fetch", fetch);
  render(<ActivityMonitor/>);
  await waitFor(() => expect(localStorage.getItem("cratekeep-dismissed-activity")).toBeNull());
  const migrated = fetch.mock.calls.find(([url]) => url === "/api/activity/history/clear")!;
  expect(JSON.parse(String(migrated[1]?.body))).toEqual({ ids: ["previously-dismissed"] });
});

it("shows a quiet latest success without repeat or empty results, with arrivals and failures separate", async () => {
  configureActivity(true);
  const latest = { ...job, id: "inbox-scan-status", label: "Inbox scans", status: "complete", is_inbox_status: true, files_found: 0 };
  const groups = [
    { key: "inbox-scans", label: "Inbox scans", scope: "All Inboxes", count: 109, active: 0, attention: 0, finished: 109, timestamp: job.created_at, latest },
    { key: "arrival-1", label: "Inbox arrivals", scope: "Lidarr", count: 1, active: 0, attention: 0, finished: 1, timestamp: job.created_at, latest: { ...job, id: "arrival", label: "Scanning Inbox", status: "complete", scan_added: 2 } },
    { key: "failed-1", label: "Scanning Inbox", scope: "Manual", count: 1, active: 0, attention: 1, finished: 1, timestamp: job.created_at, latest: { ...job, id: "failure", status: "failed" } },
  ];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => ({ ok: true, json: async () => url.startsWith("/api/activity/history") ? { groups, snapshot: 110, clearable: 111, next_before: null } : { jobs: [] } })));
  render(<ActivityMonitor/>);
  fireEvent.click(await screen.findByRole("button", { name: "Activity" }));
  expect(await screen.findByText("Success")).toBeInTheDocument();
  expect(screen.queryByText("×109")).not.toBeInTheDocument();
  expect(screen.queryByText(/0 tracks found/)).not.toBeInTheDocument();
  expect(screen.getByText(/2 new tracks/)).toBeInTheDocument();
  expect(screen.getByText(/^failed/)).toBeInTheDocument();
});

it("clears busy UI when an automatic success was coalesced before the next poll", async () => {
  configureActivity(true); vi.useFakeTimers();
  const automatic = { ...job, id: "automatic", label: "Scanning Inbox", automatic_scan: true };
  let finished = false;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url === "/api/activity/automatic" ? { ok: false, status: 404, json: async () => ({ error: "Activity not found" }) } : { ok: true, json: async () => ({ jobs: finished ? [] : [automatic] }) }));
  render(<ActivityMonitor/>);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByRole("button", { name: "Activity (1)" })).toBeInTheDocument();
  finished = true;
  await act(async () => { await vi.advanceTimersByTimeAsync(1600); });
  expect(screen.getByRole("button", { name: "Activity" })).not.toHaveClass("is-running");
});


it("loads completed match details before notifying a queue from the compact feed", async () => {
  configureActivity(true); vi.useFakeTimers();
  let finished = false;
  const matching = { ...job, id: "summary-match", mode: "matching", label: "Matching albums", href: "/inbox", rows: [] };
  const fetch = vi.fn(async (url: string) => ({ ok: true, json: async () => url === "/api/activity"
    ? { jobs: [{ ...matching, status: finished ? "complete" : "running" }] }
    : { ...matching, status: "complete", rows: [{ id: 42, status: "matched" }] } }));
  vi.stubGlobal("fetch", fetch);
  const received: any[] = [];
  const listener = (event: Event) => { const completed = (event as CustomEvent).detail; if (completed.id === matching.id) received.push(completed); };
  window.addEventListener("cratekeep:activity-finished", listener);
  try {
    render(<ActivityMonitor/>);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    finished = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(received).toHaveLength(1);
    expect(received[0].rows).toEqual([{ id: 42, status: "matched" }]);
    expect(fetch.mock.calls.some(([url]) => url === "/api/activity/summary-match")).toBe(true);
  } finally { window.removeEventListener("cratekeep:activity-finished", listener); }
});


it("buffers detailed progress for three seconds without delaying fast completion", async () => {
  vi.useFakeTimers();
  const current = { ...job, id: "buffered", created_at: Date.now() / 1000 };
  const view = render(<ActivityProgress job={current}/>);
  expect(screen.getByRole("status")).toHaveTextContent("Loading…");
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(2999); });
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByRole("progressbar")).toBeInTheDocument();
  view.rerender(<ActivityProgress job={{ ...current, id: "fast", created_at: Date.now() / 1000 }}/>);
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  view.rerender(<ActivityProgress job={{ ...current, id: "fast", status: "complete", stage: "Finished" }}/>);
  expect(screen.getByText("Finished")).toBeInTheDocument();
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
});


it("dismisses the popover on outside pointer or Escape, while inside clicks keep it open", async () => {
  configureActivity(true);
  vi.stubGlobal("fetch", vi.fn(activityFetch()));
  render(<><ActivityMonitor/><button>Outside</button></>);
  const toggle = await screen.findByRole("button", { name: "Activity (1)" });
  fireEvent.click(toggle);
  const panel = await screen.findByRole("region", { name: "Activity monitor" });
  expect(await screen.findByRole("region", { name: "Recent activity" })).toHaveAttribute("tabindex", "0");
  fireEvent.pointerDown(panel);
  expect(panel).toBeInTheDocument();
  fireEvent.pointerDown(screen.getByRole("button", { name: "Outside" }));
  expect(screen.queryByRole("region", { name: "Activity monitor" })).not.toBeInTheDocument();
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(toggle);
  fireEvent.keyDown(document, { key: "Escape" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
});

it("excludes only the inspector-owned job from page progress and restores it on release", async () => {
  configureActivity(true);
  const other = { ...job, id: "other-job", label: "Matching albums" };
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ jobs: [job, other] }) })));
  const view = render(<><ActivityMonitor/><ActivityInline scope="library" excludeId={job.id}/></>);
  expect(await screen.findByRole("button", { name: "Activity (2)" })).toBeInTheDocument();
  expect(await screen.findByText("Matching albums · Library")).toBeInTheDocument();
  expect(screen.queryByText("Refreshing files · Library")).not.toBeInTheDocument();
  view.rerender(<><ActivityMonitor/><ActivityInline scope="library"/></>);
  expect(await screen.findByText("Refreshing files · Library")).toBeInTheDocument();
});
