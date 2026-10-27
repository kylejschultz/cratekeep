import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useInboxDiscovery } from "./useInboxDiscovery";
const status = (revision: string, mode = "instant") => ({ mode, revision, scanned_at: null, error: "", watcher_error: "" });
const response = (body: unknown) => ({ ok: true, json: async () => body });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });
async function tick() { await act(async () => { await vi.advanceTimersByTimeAsync(2000); }); }

it("updates after revisions without rescanning or losing a pending change during review", async () => {
  vi.useFakeTimers();
  let revision = "one";
  const fetch = vi.fn(async () => response(status(revision)));
  vi.stubGlobal("fetch", fetch);
  const onChange = vi.fn(async () => {});
  const { result, rerender } = renderHook(({ paused }) => useInboxDiscovery("downloads", paused, onChange), { initialProps: { paused: false } });
  await tick();
  expect(onChange).toHaveBeenCalledTimes(1);
  await tick();
  expect(onChange).toHaveBeenCalledTimes(1);
  rerender({ paused: true }); revision = "two";
  await tick();
  expect(result.current.pending).toBe(true);
  expect(onChange).toHaveBeenCalledTimes(1);
  rerender({ paused: false });
  await tick();
  expect(onChange).toHaveBeenCalledTimes(2);
  expect(result.current.pending).toBe(false);
  expect(fetch.mock.calls.every(([url]: [string?]) => url === "/api/inbox/downloads/discovery")).toBe(true);
});

it("catches up after a failed connection and cleans up when switching source", async () => {
  vi.useFakeTimers();
  const fetch = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(response(status("latest")));
  vi.stubGlobal("fetch", fetch);
  const onChange = vi.fn(async () => {});
  const { rerender, unmount } = renderHook(({ source }) => useInboxDiscovery(source, false, onChange), { initialProps: { source: "one" } });
  await tick(); expect(onChange).not.toHaveBeenCalled();
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(onChange).toHaveBeenCalledTimes(1);
  rerender({ source: "two" });
  await tick();
  expect(fetch.mock.calls.at(-1)?.[0]).toBe("/api/inbox/two/discovery");
  unmount();
  const count = fetch.mock.calls.length;
  await tick(); expect(fetch).toHaveBeenCalledTimes(count);
});

it("does not live-refresh a manual-only Inbox", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn(async () => response(status("one", "manual"))));
  const onChange = vi.fn(async () => {});
  renderHook(() => useInboxDiscovery("manual", false, onChange));
  await tick();
  expect(onChange).not.toHaveBeenCalled();
});
