import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ActivityMonitor, ActivityInline } from "./Activity";
import { configureActivity, api, startActivity } from "./api";

const job = { id: "job-1", label: "Refreshing files", scope: "Library", href: "/library-import", status: "running", stage: "Finding files", files_found: 12, stage_done: 12, stage_total: null, stage_unit: "files", processed: 0, total: 1, rows: [] };
afterEach(() => { cleanup(); configureActivity(false); vi.unstubAllGlobals(); vi.useRealTimers(); });

it("shows the same live job in the header and relevant page without inventing a total", async () => {
  configureActivity(true);
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ jobs: [job] }) })));
  render(<><ActivityMonitor/><ActivityInline scope="library"/></>);
  const activity = await screen.findByRole("button", { name: "Activity (1)" });
  expect(activity).toHaveClass("is-running");
  expect(activity).toHaveAttribute("title", "Activity: 1 running");
  fireEvent.click(activity);
  expect(screen.getAllByText(/12 tracks found/)).toHaveLength(2);
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
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ jobs: [{ ...job, id: "completed-compact", status: "complete", stage: "Finished", files_found: 42 }] }) })));
  render(<ActivityMonitor/>);
  fireEvent.click(await screen.findByRole("button", { name: "Activity" }));
  expect(screen.getByRole("button", { name: "Activity" })).not.toHaveClass("is-running");
  expect(await screen.findByText("complete · 42 tracks found")).toBeInTheDocument();
  expect(screen.queryByText("Finished")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Dismiss Refreshing files" }).closest("article")).toBe(screen.getByText("complete · 42 tracks found").closest("article"));
  fireEvent.click(screen.getByRole("button", { name: /Refreshing files Library complete/ }));
  expect(await screen.findByRole("dialog", { name: "Refreshing files" })).toBeInTheDocument();
  expect(screen.getByText("Finished")).toBeInTheDocument();
});
