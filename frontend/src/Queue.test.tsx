import type { ReactElement } from "react";
import { ToastProvider } from "./Toasts";
import { act, cleanup, fireEvent, render as renderComponent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Queue } from "./Queue";
import { albumStatus, formatMatchPercent } from "./status";
const render = (ui: ReactElement) => renderComponent(ui, { wrapper: ToastProvider });
const response = (body: unknown, status = 200) => ({ ok: status < 400, status, json: async () => body });
const album = (id: number, title: string) => ({ id, album: title, artist: "Artist", tracks: [{ path: `Artist/${title}/Song.wav` }], source_paths: [`Artist/${title}`] });
const page = (albums: unknown[], number = 1) => ({ albums, page: number, page_count: 2, total: 26 });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
async function selectAlbum(name: string) {
  const input = await screen.findByRole("checkbox", { name });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.click(input);
}


describe("albumStatus", () => {
  it("distinguishes missing matches, low confidence and provider errors", () => {
    expect(albumStatus({ id: 1, candidate_status: "complete" }, 90).key).toBe("pending");
    expect(albumStatus({ id: 1, candidate_status: "stale", candidate_error: "Inventory refreshed" }, 90).label).toBe("Pending");
    expect(albumStatus({ id: 1 }, 90).key).toBe("pending");
    expect(albumStatus({ id: 1, candidate_status: "complete", candidates: [{ id: 2, confidence: .92 }] }, 90).score).toBe(92);
    expect(albumStatus({ id: 1, candidate_status: "complete", candidates: [{ id: 2, confidence: .89 }] }, 90).key).toBe("matched");
    expect(albumStatus({ id: 1, candidate_error: "provider unavailable" }, 90).key).toBe("pending");
  });
});
it("retains album selection across pages and select-page affects only the displayed page", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: string) => response(input.includes("page=2") ? page([album(3, "Three")], 2) : page([album(1, "One"), album(2, "Two")]))));
  render(<Queue source="library-import" threshold={90}/>);
  await selectAlbum("Select album One");
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  await screen.findByText(/Page 2 of 2/);
  fireEvent.click(screen.getByRole("checkbox", { name: "Select this page" }));
  expect(screen.getByText("2 selected")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Previous" }));
  expect(await screen.findByRole("checkbox", { name: "Select album One" })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Select album Two" })).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Select this page" })).toBePartiallyChecked();
});
it("lists albums flat and opens an inspector without approving or matching", async () => {
  const fetch = vi.fn(async () => response(page([album(1, "One"), album(2, "Two")])));
  vi.stubGlobal("fetch", fetch);
  render(<Queue source="inbox" inboxId="one" sourceName="Manual" threshold={90}/>);
  fireEvent.click(await screen.findByRole("button", { name: "Review Two" }));
  expect(screen.getByRole("heading", { name: "Two" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Expand folder/ })).not.toBeInTheDocument();
  expect(fetch.mock.calls).toHaveLength(2);
  expect(screen.getByRole("region", { name: "Two" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Search MBID" })).toBeInTheDocument();
  expect(fetch.mock.calls).toHaveLength(2);
  expect(screen.getByRole("region", { name: "Two" }).closest(".desk-workspace")).toHaveClass("is-detail-open");
  fireEvent.click(screen.getByRole("button", { name: "Back to albums" }));
  expect(screen.getByRole("button", { name: "Review Two" }).closest(".desk-workspace")).not.toHaveClass("is-detail-open");
  expect(fetch.mock.calls).toHaveLength(2);
});
it("bulk import submits explicit album IDs, excluding unchecked siblings", async () => {
  const fetch = vi.fn(async (url: string) => response(url.endsWith("/preview") ? { importable_albums: [{ id:1, album:"One" }] } : url.endsWith("/execute") ? { imported: [{ album_review_id:1 }] } : page([album(1, "One"), album(2, "Two")])));
  vi.stubGlobal("fetch", fetch); vi.stubGlobal("confirm", () => true);
  render(<Queue source="library-import" threshold={90}/>);
  await selectAlbum("Select album One");
  fireEvent.click(screen.getByRole("button", { name: "Import selected" }));
  fireEvent.click(await screen.findByRole("button", { name: "Start import" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/library-import/bulk/execute", expect.objectContaining({ body: JSON.stringify({ folders: ["Artist"], album_ids: [1], mode: "selected", confirmed: true }) })));
});
it("Refresh performs a disk rescan without provider matching", async () => {
  const fetch = vi.fn(async (url: string) => response(url.endsWith("/refresh") ? { inventory: { files: 2 }, cleanup: { removed: [] } } : page([album(1, "One")])));
  vi.stubGlobal("fetch", fetch);
  render(<Queue source="inbox" inboxId="manual" threshold={90}/>);
  await screen.findByRole("checkbox", { name: "Select album One" });
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await screen.findByText(/Inbox refreshed: 2 tracks/);
  expect(fetch).toHaveBeenCalledWith("/api/inbox/manual/scopes/refresh", expect.objectContaining({ method: "POST", body: "{}" }));
  expect(fetch.mock.calls.some(([url]) => url.includes("candidates"))).toBe(false);
});
it("preserves provider failure evidence and leaves matching retryable", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("/candidates")) return response({ errors: [{ error:"MusicBrainz returned HTTP 503", retryable:true, retry_after_seconds:3600 }] });
    return response(page([album(1, "One")]));
  }));
  render(<Queue source="inbox" inboxId="one" threshold={90}/>);
  await selectAlbum("Select album One");
  fireEvent.click(screen.getByRole("button", { name: "Match Selected" }));
  fireEvent.click(screen.getByRole("button", { name:"Start matching" }));
  await screen.findByRole("heading", { name:"Matching paused" });
  expect(screen.getByText("MusicBrainz returned HTTP 503")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name:"Close results" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Match Selected" })).toBeEnabled());
});
it("searches the whole queue and prevents an older response overwriting newer filters", async () => {
  let finishOld: ((value: unknown) => void) | undefined;
  const fetch = vi.fn(async (url: string) => {
    if (url.includes("q=old")) return new Promise(resolve => { finishOld = resolve; });
    return response(page([album(2, url.includes("q=new") ? "New result" : "Initial")]));
  });
  vi.stubGlobal("fetch", fetch);
  render(<Queue source="library-import" threshold={90}/>);
  await screen.findByRole("button", { name: "Review Initial" });
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "old" } });
  await waitFor(() => expect(finishOld).toBeDefined());
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "new" } });
  await screen.findByRole("button", { name: "Review New result" });
  finishOld?.(response(page([album(1, "Old result")])));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Review Old result" })).not.toBeInTheDocument());
});

it.each(["match", "import"])("keeps bulk %s progress on its own button", async operation => {
  let finish: (() => void) | undefined;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("/candidates") || url.endsWith("/preview")) {
      await new Promise<void>(resolve => { finish = resolve; });
      return response(url.endsWith("/preview") ? { ready_album_count: 0 } : { album: { id:1, candidate_status:"complete", candidates:[{id:2}] } });
    }
    return response(page([album(1,"One")]));
  }));
  render(<Queue source="inbox" inboxId="one" threshold={90}/>);
  await selectAlbum("Select album One");
  fireEvent.click(screen.getByRole("button", { name: operation === "match" ? "Match Selected" : "Import selected" }));
  if (operation === "match") fireEvent.click(screen.getByRole("button", { name:"Start matching" }));
  const active = screen.getByRole("button", { name: operation === "match" ? "Matching…" : "Importing…" });
  const idle = screen.getByRole("button", { name: operation === "match" ? "Import selected" : "Match Selected" });
  expect(active).toHaveAttribute("aria-busy","true");
  expect(idle).toHaveAttribute("aria-busy","false");
  expect(idle.querySelector(".spinner")).toBeNull();
  finish?.();
  if (operation === "match") fireEvent.click(await screen.findByRole("button", { name:"Close results" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Match Selected" })).toBeEnabled());
});

it.each([[99.6, "99.6%"], [99.96, "99.9%"], [100, "100%"], [88, "88%"]])("formats %s without a false perfect score", (value, expected) => {
  expect(formatMatchPercent(value as number)).toBe(expected);
});

it("filters perfect matches and duplicates across the library queue", async () => {
  const fetch = vi.fn(async () => response(page([album(1, "One")])));
  vi.stubGlobal("fetch", fetch);
  render(<Queue source="library-import" threshold={90}/>);
  await screen.findByRole("checkbox", { name: "Select album One" });
  fireEvent.click(screen.getByRole("button", { name: "Perfect match" }));
  await waitFor(() => expect(fetch).toHaveBeenLastCalledWith(expect.stringContaining("status=perfect"), expect.anything()));
  fireEvent.click(screen.getByRole("button", { name: "Duplicates" }));
  await waitFor(() => expect(fetch).toHaveBeenLastCalledWith(expect.stringContaining("status=duplicates"), expect.anything()));
});

it.each(["inbox", "library-import"] as const)("%s uses selected imports and only Pending/Matched review statuses", async source => {
  const fetch = vi.fn(async (url: string) => response(url.endsWith("/preview") ? { importable_albums: [{ id:1, album:"One" }] } : page([{ ...album(1,"One"), candidate_status:"complete", candidates:[{id:2,confidence:.2}] }])));
  vi.stubGlobal("fetch",fetch);
  render(<Queue source={source} inboxId="manual" threshold={90}/>);
  fireEvent.click(await screen.findByRole("button",{name:"Review One"}));
  expect(screen.queryByRole("button",{name:"Review import"})).not.toBeInTheDocument();
  expect(screen.getByText("Matched",{selector:".desk-decision"})).toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox",{name:"Select album One"}));
  expect(screen.queryByRole("button",{name:"Review selected"})).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"Import selected"}));
  await screen.findByRole("button",{name:"Start import"});
  const call=fetch.mock.calls.find(([url])=>url.endsWith("/preview"));
  expect(call?.[0]).toBe(source==="inbox" ? "/api/inbox/manual/bulk/preview" : "/api/library-import/bulk/preview");
});

it("Imported rows expose confirmed Undo and removal, without matching or reimport", async () => {
  const imported={...album(1,"One"),imported:{job_id:7,undoable:true,originals:["Artist/One/Song.wav"]}};
  const fetch=vi.fn(async(url:string)=>response(url.endsWith("/preview") ? {preview_id:"preview",fingerprint:"hash",count:1,files:[{location:"Library",path:"Artist/One/Song.wav"}]} : url.endsWith("/apply") ? {status:"complete",message:"Undone"} : page([imported])));
  vi.stubGlobal("fetch",fetch);
  render(<Queue source="inbox" inboxId="manual" threshold={90}/>);
  fireEvent.click(await screen.findByRole("button",{name:"Review One"}));
  fireEvent.click(screen.getByRole("checkbox",{name:"Select album One"}));
  expect(screen.getByRole("button",{name:"Import selected"})).toBeDisabled();
  expect(screen.getByRole("button",{name:"Match Selected"})).toBeDisabled();
  fireEvent.click(screen.getByRole("button",{name:"Undo import"}));
  const confirm=await screen.findByRole("button",{name:"Remove Library copies and undo"});
  await waitFor(()=>expect(confirm).toBeEnabled());
  expect(fetch.mock.calls.some(([url])=>url.endsWith("/apply"))).toBe(false);
  fireEvent.click(confirm);
  await waitFor(()=>expect(fetch).toHaveBeenCalledWith("/api/inbox/manual/actions/apply",expect.objectContaining({body:JSON.stringify({preview_id:"preview",expected_fingerprint:"hash",confirmed:true})})));
});

it("refreshes an already-open metadata pane after bulk matching", async () => {
  let matched=false;
  vi.stubGlobal("fetch",vi.fn(async (url:string)=> {
    if(url.endsWith("/candidates")) { matched=true;return response({album:{id:1,candidate_status:"complete",candidates:[{id:2}]}}); }
    return response(page([{...album(1,"One"),candidate_status:matched ? "complete" : "stale",candidates:matched ? [{id:2,artist:"Matched artist",album:"Matched release",confidence:.98}] : []}]));
  }));
  render(<Queue source="inbox" inboxId="manual" threshold={90}/>);
  fireEvent.click(await screen.findByRole("button",{name:"Review One"}));
  fireEvent.click(screen.getByRole("checkbox",{name:"Select album One"}));
  fireEvent.click(screen.getByRole("button",{name:"Match Selected"}));
  fireEvent.click(screen.getByRole("button",{name:"Start matching"}));
  fireEvent.click(await screen.findByRole("button",{name:"Close results"}));
  expect(await screen.findByText("Matched artist — Matched release")).toBeInTheDocument();
  expect(screen.getByRole("button",{name:"Import to Library"})).toBeEnabled();
});


it("ends a stalled queue request with a retryable error instead of an endless loading state", async () => {
  vi.useFakeTimers();
  try {
    vi.stubGlobal("fetch",vi.fn((_url:string,init?:RequestInit)=>new Promise((_resolve,reject)=>{
      init?.signal?.addEventListener("abort",()=>reject(new DOMException("Aborted","AbortError")));
    })));
    render(<Queue source="inbox" inboxId="manual" threshold={90}/>);
    await act(async()=>{ await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByText("Scanning Inbox…")).toBeInTheDocument();
    await act(async()=>{ await vi.advanceTimersByTimeAsync(30000); });
    expect(screen.getByRole("alert")).toHaveTextContent("Album loading timed out");
    expect(screen.queryByText("Scanning Inbox…")).not.toBeInTheDocument();
    expect(screen.getByRole("button",{name:"Refresh"})).toBeEnabled();
  } finally { vi.useRealTimers(); }
});

it("scans an Inbox on open before reading its saved album queue", async () => {
  let finishScan!: (value: ReturnType<typeof response>) => void;
  const fetch = vi.fn((url: string) => url.endsWith("/queue/refresh")
    ? new Promise<ReturnType<typeof response>>(resolve => { finishScan = resolve; })
    : Promise.resolve(response(page([album(1, "New download")]))));
  vi.stubGlobal("fetch", fetch);
  render(<Queue source="inbox" inboxId="manual" threshold={90}/>);
  expect(await screen.findByText("Scanning Inbox…")).toBeInTheDocument();
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(fetch.mock.calls[0][0]).toBe("/api/inbox/manual/queue/refresh");
  await act(async () => finishScan(response({ inventory: { files: 1 } })));
  expect(await screen.findByRole("button", { name: "Review New download" })).toBeInTheDocument();
  expect(fetch.mock.calls[1][0]).toContain("/api/inbox/manual/queue?");
  fireEvent.click(screen.getByRole("button", { name: "Matched" }));
  await waitFor(() => expect(fetch.mock.calls.filter(([url]) => url.endsWith("/queue/refresh"))).toHaveLength(1));
});

it("shows scan errors instead of claiming the Inbox is empty", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response({ error: "Unable to inventory inbox: permission denied" }, 409)));
  render(<Queue source="inbox" inboxId="manual" threshold={90}/>);
  expect(await screen.findByText(/Unable to inventory inbox: permission denied/)).toBeInTheDocument();
  expect(screen.queryByText("Nothing waiting here")).not.toBeInTheDocument();
  expect(screen.queryByText("Scanning Inbox…")).not.toBeInTheDocument();
});

it("defers live arrivals during selection then updates without a disk rescan", async () => {
  vi.useFakeTimers();
  try {
    let changed = false;
    const fetch = vi.fn(async (url: string) => response(url.endsWith("/discovery")
      ? { mode: "instant", revision: changed ? "two" : "one", scanned_at: null, error: "", watcher_error: "" }
      : page(changed ? [album(1, "One"), album(2, "New arrival")] : [album(1, "One")])));
    vi.stubGlobal("fetch", fetch);
    render(<Queue source="inbox" inboxId="one" threshold={90}/>);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    fireEvent.click(screen.getByRole("checkbox", { name: "Select album One" }));
    changed = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByRole("checkbox", { name: "Select album One" })).toBeChecked();
    expect(screen.queryByRole("checkbox", { name: "Select album New arrival" })).not.toBeInTheDocument();
    expect(screen.getByText(/Inbox updates waiting/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByRole("checkbox", { name: "Select album New arrival" })).toBeInTheDocument();
    expect(fetch.mock.calls.filter(([url]) => url.endsWith("/refresh"))).toHaveLength(1);
  } finally { vi.useRealTimers(); }
});
