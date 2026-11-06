import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Inbox } from "./pages";
import { InboxActionDialog } from "./InboxImportActions";
import { ToastProvider } from "./Toasts";
const inboxes = [{ id: "left", name: "Lidarr" }, { id: "right", name: "Manual" }];
const albums = inboxes.map((inbox, index) => ({ id: index + 1, album: "Same album", artist: "Artist", source_id: inbox.id, inbox_name: inbox.name, source_paths: ["Shared"], tracks: [{ path: "Shared/song.wav" }] }));
const response = (body: unknown) => ({ ok: true, json: async () => body });
const page = { albums, total: 2, page: 1, page_count: 1 };
afterEach(() => { cleanup(); vi.unstubAllGlobals(); history.replaceState(null, "", "/"); });

it("adds a combined tab with source pills while preserving the default and named tabs", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => response(url.endsWith("/refresh") ? { inventory: { files: 2 }, cleanup: { removed: [] } } : url.includes("/all/queue") ? page : { ...page, albums: [albums[0]], total: 1 })));
  history.replaceState(null, "", "/inbox");
  render(<ToastProvider><Inbox data={{ inboxes }} threshold={90}/></ToastProvider>);
  expect(screen.getByRole("tab", { name: "Lidarr" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("tab", { name: "All Inboxes" }));
  expect(await screen.findByRole("button", { name: "Review Same album from Lidarr" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Review Same album from Manual" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "All Inboxes" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("button", { name: "Review Same album from Manual" }).querySelector(".inbox-source-pill")).toHaveTextContent("Manual");
  expect(location.search).toBe("?source=all");
  fireEvent.click(screen.getByRole("checkbox", { name: "Select this page" }));
  expect(screen.getByText("2 selected")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "Lidarr" }));
  await screen.findByRole("button", { name: "Review Same album" });
  expect(screen.queryByText("2 selected")).not.toBeInTheDocument();
});

it("routes a mixed import and its imported cleanup to real Inbox IDs", async () => {
  history.replaceState(null, "", "/inbox?source=all");
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    const id = url.includes("/left/") ? 1 : 2;
    if (url.endsWith("/bulk/preview")) { expect(body.album_ids).toEqual([id]); return response({ importable_albums: [{ id, album: "Same album" }] }); }
    if (url.endsWith("/bulk/execute")) return response({ imported: [{ album_review_id: id, job_id: id + 10 }] });
    if (url.endsWith("/actions/preview")) return response({ preview_id: `p${id}`, fingerprint: "fp", files: [{ path: "Shared/song.wav", location: "Inbox" }], count: 1 });
    if (url.endsWith("/actions/apply")) return response({ status: "complete", message: "Deleted" });
    if (url.endsWith("/refresh")) return response({ inventory: { files: 1 }, cleanup: { removed: [] } });
    return response(page);
  });
  vi.stubGlobal("fetch", fetch);
  render(<ToastProvider><Inbox data={{ inboxes }} threshold={90}/></ToastProvider>);
  await screen.findByRole("button", { name: "Review Same album from Manual" });
  fireEvent.click(screen.getByRole("checkbox", { name: "Select this page" }));
  fireEvent.click(screen.getByRole("button", { name: "Import selected" }));
  fireEvent.click(await screen.findByRole("button", { name: "Start import" }));
  await screen.findByText("2 of 2 albums imported.");
  fireEvent.click(screen.getByRole("button", { name: "Clear imported from Inbox" }));
  const dialog = await screen.findByRole("dialog", { name: "Remove imported originals" });
  expect(await within(dialog).findByText("Lidarr · Shared")).toBeInTheDocument();
  expect(within(dialog).getByText("Manual · Shared")).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "Permanently remove Inbox originals" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  const imports = fetch.mock.calls.filter(([url]) => url.endsWith("/bulk/execute"));
  expect(imports.map(([url]) => url)).toEqual(["/api/inbox/left/bulk/execute", "/api/inbox/right/bulk/execute"]);
  expect(imports.map(([, init]) => JSON.parse(String(init?.body)).album_ids)).toEqual([[1], [2]]);
  expect(fetch.mock.calls.filter(([url]) => /\/all\/.+(preview|apply|execute|refresh)$/.test(url))).toHaveLength(0);
});

it("keeps source-aware partial deletion results open and never repeats a lost apply", async () => {
  const fetch = vi.fn(async (url: string) => {
    if (url.endsWith("/preview")) return response({ preview_id: "p", fingerprint: "fp", files: [{ path: "Shared/song.wav", location: "Inbox" }], count: 1 });
    if (url.endsWith("/apply")) throw new TypeError("Lost response");
    return response({ status: url.includes("/left/") ? "complete" : "running", result: { message: "Saved" } });
  });
  vi.stubGlobal("fetch", fetch); const onClose = vi.fn();
  render(<ToastProvider><InboxActionDialog inboxId="all" kind="delete-albums" groups={inboxes.map((inbox, index) => ({ kind: "delete-albums", inboxId: inbox.id, inboxName: inbox.name, albumIds: [index + 1] }))} onClose={onClose} onDone={vi.fn()}/></ToastProvider>);
  const button = screen.getByRole("button", { name: "Delete permanently" });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  expect(await screen.findByRole("alert")).toHaveTextContent("Manual");
  expect(onClose).not.toHaveBeenCalled();
  expect(button).toBeDisabled();
  expect(fetch.mock.calls.filter(([url]) => url.endsWith("/apply"))).toHaveLength(2);
});
