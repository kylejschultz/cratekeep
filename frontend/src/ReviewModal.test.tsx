import type { ReactElement } from "react";
import { ToastProvider } from "./Toasts";
import { cleanup, fireEvent, render as renderComponent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReviewModal } from "./ReviewModal";

const render = (ui: ReactElement) => renderComponent(ui, { wrapper: ToastProvider });

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const richAlbum = {
  id: 1, album: "Current Album", artist: "Artist", selected_candidate_id: 2,
  tracks: [
    { path: "Artist/Album/01 Same.wav", title: "Same", artist: "Artist" },
    { path: "Artist/Album/02 Local.wav", title: "Local", artist: "Artist" },
  ],
  candidates: [{
    id: 2, provider_id: "123e4567-e89b-42d3-a456-426614174000", artist: "Artist",
    album: "Proposed Album", genre: "Rock", year: 2024, confidence: .96,
    artwork_url: "https://coverartarchive.org/release/123e4567-e89b-42d3-a456-426614174000/front-250",
    release_type: "Album", country: "GB", media: [{ format: "CD" }], matched_track_count: 1,
    unmatched_track_count: 1, recordings: [{}, {}], proposed_diff: { album: { from: "Current Album", to: "Proposed Album" } },
    track_details: [
      { status: "matched", local: "Same", proposed: "Same", local_artist: "Artist", proposed_artist: "Artist", proposed_position: 1 },
      { status: "title-mismatch", local: "Local", proposed: "Canonical", local_artist: "Artist", proposed_artist: "Featured Artist", proposed_position: 2 },
    ],
  }],
};

describe("ReviewModal matching and evidence", () => {
  it("opens only the selected candidate and keeps As Is last", () => {
    const album = { ...richAlbum, candidates: [
      { ...richAlbum.candidates[0], id: 3, album: "Lower candidate", confidence: .75 },
      richAlbum.candidates[0],
    ] };
    render(<ReviewModal source="library-import" onClose={() => undefined} onComplete={() => undefined} album={album}/>);
    expect(document.querySelectorAll(".candidate-expanded")).toHaveLength(1);
    expect(document.querySelector(".candidate-list article:last-child")).toHaveClass("as-is");
    fireEvent.click(screen.getByRole("button", { name: /Lower candidate/ }));
    expect(document.querySelectorAll(".candidate-expanded")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /Lower candidate/ })).toHaveAttribute("aria-expanded", "true");
  });

  it("removes the current panel and shows As Is plus rich, expandable candidate evidence", () => {
    render(<ReviewModal source="library-import" onClose={() => undefined} onComplete={() => undefined} album={richAlbum}/>);

    expect(screen.queryByRole("heading", { name: "Current metadata" })).not.toBeInTheDocument();
    expect(screen.getByText(/Retain incoming\/local metadata unchanged/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Match" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Search MBID" })).toBeInTheDocument();
    expect(document.querySelector(".candidate-artwork img")).toHaveAttribute("src", expect.stringContaining("coverartarchive.org"));
    expect(screen.getByText("96% match · 2024")).toBeInTheDocument();
    expect(screen.getByText("123e4567-e89b-42d3-a456-426614174000")).toBeInTheDocument();
    expect(screen.getByText("Album · CD")).toBeInTheDocument();
    expect(screen.getByText("GB")).toBeInTheDocument();
    expect(screen.getAllByLabelText("Album changed").length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText("Artist unchanged").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText("Compare metadata"));
    expect(screen.getByText("Current Album → Proposed Album")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Compare tracks (2)"));
    expect(screen.getByText("01 Same.wav")).toBeInTheDocument();
    expect(screen.getByText("02 Local.wav")).toBeInTheDocument();
    expect(screen.getByText("Canonical")).toBeInTheDocument();
    expect(screen.getByLabelText("Metadata unchanged")).toBeInTheDocument();
    expect(screen.getByLabelText("Metadata changed")).toBeInTheDocument();
  });

  it("matches once, exposes busy state, refreshes the album, and reports completion", async () => {
    let resolveMatch: (() => void) | undefined;
    const refreshed = { ...richAlbum, candidates: [{ ...richAlbum.candidates[0], album: "Refreshed Album" }] };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/library-import/candidates") {
        await new Promise<void>(resolve => { resolveMatch = resolve; });
        return { ok: true, json: async () => ({ generated: 1 }) };
      }
      if (url === "/api/library-import/albums/1" && !init?.method) return { ok: true, json: async () => refreshed };
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewModal source="library-import" onClose={() => undefined} onComplete={() => undefined} album={richAlbum}/>);

    fireEvent.click(screen.getByRole("button", { name: "Match" }));
    expect(screen.getByRole("button", { name: "Match" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Match" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("button", { name: "Import to Library" })).toHaveAttribute("aria-busy", "false");
    expect(screen.getByRole("button", { name: "Import to Library" }).querySelector(".spinner")).toBeNull();
    expect(screen.getByText("Matching with MusicBrainz…")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Match" }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    resolveMatch?.();
    expect(await screen.findByText("Refreshed Album")).toBeInTheDocument();
    expect(screen.getByText("Match complete. Review the refreshed candidates.")).toBeInTheDocument();
  });

  it("validates a canonical MBID and keeps request errors inside the modal", async () => {
    const fetchMock = vi.fn(async () => ({ ok: false, status: 503, json: async () => ({ error: "MusicBrainz unavailable" }) }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewModal source="inbox" onClose={() => undefined} onComplete={() => undefined} album={richAlbum}/>);

    fireEvent.click(screen.getByRole("button", { name: "Search MBID" }));
    const input = screen.getByLabelText("MusicBrainz release UUID");
    fireEvent.change(input, { target: { value: "not-a-uuid" } });
    fireEvent.click(screen.getByRole("button", { name: "Search exact release" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a canonical MusicBrainz release UUID.");
    expect(fetchMock).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: "123E4567-E89B-42D3-A456-426614174000" } });
    fireEvent.click(screen.getByRole("button", { name: "Search exact release" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/inbox/albums/1/rematch", expect.objectContaining({ method: "POST", body: JSON.stringify({ musicbrainz_id: "123e4567-e89b-42d3-a456-426614174000" }) })));
    expect(await screen.findByRole("alert")).toHaveTextContent("MusicBrainz unavailable");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("renders a successful direct MBID album response instead of its album-title string", async () => {
    const rematched = { ...richAlbum, album: "Exact Edition", candidates: [{ ...richAlbum.candidates[0], album: "Exact Edition" }] };
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => rematched }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewModal source="library-import" onClose={() => undefined} onComplete={() => undefined} album={richAlbum}/>);

    fireEvent.click(screen.getByRole("button", { name: "Search MBID" }));
    fireEvent.change(screen.getByLabelText("MusicBrainz release UUID"), { target: { value: "123e4567-e89b-42d3-a456-426614174000" } });
    fireEvent.click(screen.getByRole("button", { name: "Search exact release" }));
    expect(await screen.findByRole("heading", { name: "Exact Edition" })).toBeInTheDocument();
    expect(screen.getByText("Exact MusicBrainz release loaded. Review it before importing.")).toBeInTheDocument();
  });

  it("omits duplicate decisions for a normal import and keeps a real backend failure retryable", async () => {
    const requests: Array<{ url: string; body: any }> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body));
      requests.push({ url: String(input), body });
      if (body.dry_run) return { ok: true, json: async () => ({ status: "preview" }) };
      return { ok: false, status: 502, json: async () => ({ error: "Tag writer temporarily unavailable" }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewModal source="library-import" onClose={() => undefined} onComplete={() => undefined} album={{ ...richAlbum, duplicate_preflight: { status: "clear" }, candidates: [{ ...richAlbum.candidates[0], duplicate_preflight: { status: "clear" } }] }}/>);

    expect(screen.queryByLabelText("Duplicate decision")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Import to Library" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Tag writer temporarily unavailable Retry is available.");
    expect(screen.getByRole("button", { name: "Retry import" })).toBeEnabled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(requests).toEqual([
      { url: "/api/library-import/albums/1/execute", body: { candidate_id: 2, dry_run: true } },
      { url: "/api/library-import/albums/1/execute", body: { candidate_id: 2 } },
    ]);
  });

  it("shows and requires a duplicate decision only for a suspected duplicate", async () => {
    const onComplete = vi.fn();
    const duplicateAlbum = {
      ...richAlbum,
      candidates: [{ ...richAlbum.candidates[0], duplicate_preflight: { status: "possible" as const, message: "One incoming file may already be managed." } }],
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => ({
      ok: true,
      json: async () => init?.method === "PATCH" ? { ...duplicateAlbum, duplicate_action: "keep-both" } : JSON.parse(String(init?.body)).dry_run ? { status: "preview" } : { message: "Imported" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewModal source="library-import" onClose={() => undefined} onComplete={onComplete} album={duplicateAlbum}/>);

    const decision = screen.getByLabelText("Duplicate decision");
    expect(decision).toBeRequired();
    fireEvent.click(screen.getByRole("button", { name: "Import to Library" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Choose a duplicate decision");
    expect(fetchMock).not.toHaveBeenCalled();

    fireEvent.change(decision, { target: { value: "keep-both" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Import to Library" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Import to Library" }));
    await waitFor(() => expect(onComplete).toHaveBeenCalledWith("Imported"));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/library-import/albums/1/execute", expect.objectContaining({ body: JSON.stringify({ candidate_id: 2, duplicate_action: "keep-both", dry_run: true }) }));
    expect(fetchMock).toHaveBeenNthCalledWith(3, "/api/library-import/albums/1/execute", expect.objectContaining({ body: JSON.stringify({ candidate_id: 2, duplicate_action: "keep-both" }) }));
  });

  it("blocks ambiguous managed identity without offering an inapplicable decision", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const ambiguous = { ...richAlbum, candidates: [{ ...richAlbum.candidates[0], duplicate_preflight: { status: "ambiguous" as const } }] };
    render(<ReviewModal source="library-import" onClose={() => undefined} onComplete={() => undefined} album={ambiguous}/>);

    expect(screen.queryByLabelText("Duplicate decision")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Import to Library" }));
    expect(screen.getByRole("alert")).toHaveTextContent("managed recording identity is ambiguous");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps Inbox selection persistence separate from its execution-only payload", async () => {
    const requests: Array<{ url: string; body: any }> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body));
      requests.push({ url: String(input), body });
      if (init?.method === "PATCH") return { ok: true, json: async () => ({ ...richAlbum, decision: "approved" }) };
      return { ok: true, json: async () => body.dry_run ? { status: "preview" } : { message: "Inbox imported" } };
    });
    vi.stubGlobal("fetch", fetchMock);
    const onComplete = vi.fn();
    render(<ReviewModal source="inbox" onClose={() => undefined} onComplete={onComplete} album={richAlbum}/>);

    fireEvent.click(screen.getByRole("button", { name: "Import to Library" }));
    await waitFor(() => expect(onComplete).toHaveBeenCalledWith("Inbox imported"));
    expect(requests).toEqual([
      { url: "/api/inbox/albums/1", body: { decision: "approved", candidate_id: 2 } },
      { url: "/api/inbox/albums/1/execute", body: { dry_run: true } },
      { url: "/api/inbox/albums/1/execute", body: {} },
    ]);
  });
});

it("preselects the highest match locally without approving or importing on open", () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  render(<ReviewModal source="inbox" onClose={() => undefined} onComplete={() => undefined} album={{ ...richAlbum, selected_candidate_id: null, decision: "pending", candidates: [
    { ...richAlbum.candidates[0], id: 3, album: "Lower", confidence: .6 },
    { ...richAlbum.candidates[0], album: "Best", confidence: .88 },
  ] }}/>);
  expect(screen.getByRole("radio", { name: "Select Best" })).toBeChecked();
  expect(screen.getByRole("button", { name: "Import to Library" })).toBeEnabled();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("retains an explicit As Is choice instead of defaulting to the highest candidate", () => {
  render(<ReviewModal source="library-import" onClose={() => undefined} onComplete={() => undefined} album={{ ...richAlbum, selected_candidate_id: null, decision: "approved", selection_mode: "as-is", candidate_status: "error" }}/>);
  expect(screen.getByRole("radio", { name: "Select As Is" })).toBeChecked();
  expect(screen.getByRole("button", { name: "Import to Library" })).toBeEnabled();
});

it("requires refreshed evidence to be reviewed before retrying a stale import", async () => {
  const requests: string[] = [];
  const refreshed = { ...richAlbum, candidate_status: "complete", candidates: [{ ...richAlbum.candidates[0], album: "Changed evidence" }] };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); requests.push(url);
    if (init?.method === "PATCH") return { ok: true, json: async () => richAlbum };
    if (url.endsWith("/execute")) return { ok: false, status: 409, json: async () => ({ error: "Refresh and approve current evidence.", code: "candidate_stale" }) };
    if (url.endsWith("/candidates")) return { ok: true, json: async () => ({}) };
    return { ok: true, json: async () => refreshed };
  }));
  render(<ReviewModal source="inbox" onClose={() => undefined} onComplete={() => undefined} album={richAlbum}/>);
  fireEvent.click(screen.getByRole("button", { name: "Import to Library" }));
  await screen.findByRole("button", { name: "Refresh matches" });
  expect(screen.queryByRole("button", { name: "Retry import" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Refresh matches" }));
  expect(await screen.findByText("Changed evidence")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Import to Library" })).toBeEnabled();
  expect(requests.filter(url => url.endsWith("/execute"))).toHaveLength(1);
  expect(requests).toContain("/api/inbox/albums/1/rematch");
  expect(requests).not.toContain("/api/inbox/candidates");
});

it("continues without fetched artwork only after an explicit recovery action", async () => {
  const requests: any[] = [];
  const onComplete = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)); requests.push(body);
    if (body.skip_artwork || body.dry_run) return { ok: true, json: async () => ({ status: body.dry_run ? "preview" : "complete" }) };
    return { ok: false, status: 502, json: async () => ({ error: "artwork exceeds 10 MB limit", code: "artwork_fetch_failed" }) };
  }));
  render(<ReviewModal source="library-import" onClose={() => undefined} onComplete={onComplete} album={richAlbum}/>);
  fireEvent.click(screen.getByRole("button", { name: "Import to Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "Continue without new artwork" }));
  await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
  expect(requests).toEqual([
    { candidate_id: 2, dry_run: true }, { candidate_id: 2 },
    { candidate_id: 2, skip_artwork: true, dry_run: true }, { candidate_id: 2, skip_artwork: true },
  ]);
});

it("recovers pending artwork without resaving approval or blocking on the already-imported duplicate", async () => {
  const requests: Array<{ url: string; method: string | undefined; body: any }> = [];
  const onComplete = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)); requests.push({ url: String(input), method: init?.method, body });
    return { ok: true, json: async () => body.dry_run ? { status: "preview" } : { status: "complete" } };
  }));
  render(<ReviewModal source="inbox" onClose={() => undefined} onComplete={onComplete} album={{ ...richAlbum, execution_status: "artwork-pending", candidates: [{ ...richAlbum.candidates[0], duplicate_preflight: { status: "possible" } }] }}/>);
  expect(screen.getByRole("radio", { name: "Select As Is" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Match" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Retry artwork finalization" }));
  await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
  expect(requests).toEqual([
    { url: "/api/inbox/albums/1/execute", method: "POST", body: { dry_run: true } },
    { url: "/api/inbox/albums/1/execute", method: "POST", body: {} },
  ]);
});

it.each(["inbox", "library-import"] as const)("refreshes the selected %s release directly and retains it across a 503", async source => {
  const onComplete = vi.fn();
  const requests: Array<{ url: string; body: any }> = [];
  const refreshed = { ...richAlbum, selected_candidate_id: null, decision: "pending", candidate_status: "complete", candidates: [
    { ...richAlbum.candidates[0], id: 9, album: "Recovered release", confidence: .88 },
    { ...richAlbum.candidates[0], id: 10, provider_id: "123e4567-e89b-42d3-a456-426614174009", album: "Other release", confidence: 1 },
  ] };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    requests.push({ url: String(input), body: JSON.parse(String(init?.body)) });
    if (requests.length === 1) return { ok: false, status: 503, json: async () => ({ error: "MusicBrainz returned HTTP 503", retryable: true }) };
    return { ok: true, json: async () => refreshed };
  }));
  render(<ReviewModal source={source} onClose={() => undefined} onComplete={onComplete} album={{ ...richAlbum, candidate_status: "stale" }}/>);
  expect(screen.getByRole("button", { name: "Refresh matches before importing" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Refresh matches" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("try again later. Your selection has been kept.");
  expect(screen.getByRole("radio", { name: "Select Proposed Album" })).toBeChecked();
  expect(screen.getByRole("button", { name: "Refresh matches before importing" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Refresh matches" }));
  expect(await screen.findByRole("radio", { name: "Select Recovered release" })).toBeChecked();
  expect(screen.getByRole("button", { name: "Import to Library" })).toBeEnabled();
  expect(onComplete).not.toHaveBeenCalled();
  expect(requests).toEqual([1, 2].map(() => ({ url: `/api/${source}/albums/1/rematch`, body: { musicbrainz_id: richAlbum.candidates[0].provider_id } })));
});

it("offers actual Inbox duplicate policies and resumes cleanup without reapproving", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: "complete" }) });
  vi.stubGlobal("fetch", fetchMock);
  const onComplete = vi.fn();
  const album = { ...richAlbum, candidates: [{ ...richAlbum.candidates[0], duplicate_preflight: { status: "possible" as const } }] };
  const view = render(<ReviewModal source="inbox" onClose={() => undefined} onComplete={onComplete} album={album}/>);
  expect(screen.getByRole("option", { name: "Keep existing; retain Inbox originals" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "Replace with incoming files" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "Merge" })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "Skip duplicate" })).not.toBeInTheDocument();
  view.unmount();
  render(<ReviewModal source="inbox" onClose={() => undefined} onComplete={onComplete} album={{ ...album, execution_status: "cleanup-pending", candidate_status: "stale" }}/>);
  fireEvent.click(screen.getByRole("button", { name: "Finish Inbox cleanup" }));
  await waitFor(() => expect(onComplete).toHaveBeenCalled());
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(fetchMock.mock.calls.every(([url]) => url.endsWith("/execute"))).toBe(true);
});

it("distinguishes a completed empty match from pending and upstream failure", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => ({ ok: true, json: async () =>
    String(input).endsWith("/candidates") ? {} : { id: 1, album: "Empty", candidate_status: "complete", candidates: [] } }));
  vi.stubGlobal("fetch", fetchMock);
  render(<ReviewModal source="inbox" album={{ id: 1, album: "Empty", candidate_status: "not-run", candidates: [] }} onClose={() => undefined} onComplete={() => undefined}/>);
  expect(screen.queryByText(/No matches found/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Match" }));
  expect(await screen.findByText(/No matches found/)).toHaveTextContent("Search MBID or choose As Is");
  expect(screen.queryByText("Match complete. Review the refreshed candidates.")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Import to Library" })).toBeDisabled();
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 503, json: async () => ({ error: "MusicBrainz unavailable" }) })));
  fireEvent.click(screen.getByRole("button", { name: "Match" }));
  expect(await screen.findByText(/MusicBrainz is temporarily unavailable/)).toBeInTheDocument();
  expect(screen.queryByText(/No matches found/)).not.toBeInTheDocument();
});

it("uses identical preflight and execution from the embedded inspector", async () => {
  const complete = vi.fn(), busy = vi.fn();
  const requests: any[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    requests.push({ url, body: JSON.parse(String(init?.body)) });
    return { ok: true, json: async () => ({ message: "Imported from inspector." }) };
  }));
  render(<ReviewModal embedded source="library-import" album={richAlbum} onClose={() => undefined} onComplete={complete} onBusyChange={busy}/>);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Current Album" })).not.toHaveAttribute("aria-modal");
  fireEvent.click(screen.getByRole("button", { name: "Import to Library" }));
  await waitFor(() => expect(complete).toHaveBeenCalledWith("Imported from inspector."));
  expect(requests).toEqual([
    { url: "/api/library-import/albums/1/execute", body: { candidate_id: 2, dry_run: true } },
    { url: "/api/library-import/albums/1/execute", body: { candidate_id: 2 } },
  ]);
  expect(busy).toHaveBeenCalledWith(true);
});

it("shows import progress only on Import while Match stays idle", async () => {
  let finish: (() => void) | undefined;
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (JSON.parse(String(init?.body)).dry_run) await new Promise<void>(resolve => { finish = resolve; });
    return { ok: true, json: async () => ({ message: "Imported" }) };
  }));
  render(<ReviewModal source="library-import" album={richAlbum} onClose={() => undefined} onComplete={() => undefined}/>);
  fireEvent.click(screen.getByRole("button", { name: "Import to Library" }));
  expect(screen.getByRole("button", { name: "Import to Library" })).toHaveAttribute("aria-busy", "true");
  expect(screen.getByRole("button", { name: "Match" })).toHaveAttribute("aria-busy", "false");
  expect(screen.getByRole("button", { name: "Match" }).querySelector(".spinner")).toBeNull();
  finish?.();
});

it("reports failed optional refresh while keeping the saved match usable", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => ({ ok:true, json:async () => url.endsWith("/candidates")
    ? { errors:[{ error:"MusicBrainz 503" }] }
    : { ...richAlbum, candidate_status:"complete", candidate_error:"" } })));
  render(<ReviewModal source="library-import" album={richAlbum} onClose={() => undefined} onComplete={() => undefined}/>);
  fireEvent.click(screen.getByRole("button", { name:"Match" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Your saved match is still available");
  expect(screen.getByRole("button", { name:"Retry import" })).toBeEnabled();
});

it("offers only non-destructive library duplicate choices", () => {
  render(<ReviewModal source="library-import" album={{ ...richAlbum, candidates:[{ ...richAlbum.candidates[0], duplicate_preflight:{status:"possible"} }] }} onClose={() => undefined} onComplete={() => undefined}/>);
  expect(screen.getByRole("option", { name:"Keep existing; leave duplicate files untouched" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name:"Keep both" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name:"Replace" })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name:"Merge" })).not.toBeInTheDocument();
});

it("uses semantic icons with neutral unchanged fields and highlighted changes", () => {
  render(<ReviewModal album={richAlbum} source="inbox" onClose={() => {}} onComplete={() => {}}/>);
  const artist = screen.getAllByRole("img", { name: "Artist unchanged" });
  const album = screen.getAllByRole("img", { name: "Album changed" });
  expect(artist[0]).toHaveClass("unchanged");
  expect(album[0]).toHaveClass("changed");
  expect(artist[0].querySelector("path")?.getAttribute("d")).not.toBe(album[0].querySelector("path")?.getAttribute("d"));
  expect(screen.getAllByRole("img", { name: "Release region unchanged" }).length).toBeGreaterThan(0);
});

it("highlights incomplete online coverage even when every local track is matched", () => {
  const album = { ...richAlbum, candidates: [{ ...richAlbum.candidates[0], matched_track_count: 2, unmatched_track_count: 0, recordings: [{}, {}, {}] }] };
  render(<ReviewModal album={album} source="library-import" onClose={() => {}} onComplete={() => {}}/>);
  expect(screen.getAllByRole("img", { name: "Local tracks found online unchanged" }).length).toBeGreaterThan(0);
  expect(screen.getAllByRole("img", { name: "Online tracks present locally changed" }).length).toBeGreaterThan(0);
});


it.each(["inbox", "library-import"] as const)("links track changes to aligned evidence in %s", source => {
  const album = { ...richAlbum, candidates: [{ ...richAlbum.candidates[0], proposed_diff: { tracks: { from: ["Same", "Local"], to: ["Same", "Canonical"] } } }] };
  render(<ReviewModal source={source} embedded onClose={() => undefined} onComplete={() => undefined} album={album}/>);
  const summary = screen.getByRole("region", { name: "Proposed changes" });
  expect(summary).not.toHaveTextContent("Same, Local");
  fireEvent.click(screen.getByRole("button", { name: /1 track title changed — Compare tracks/ }));
  expect(document.querySelector(".track-comparison")).toHaveAttribute("open");
  const table = screen.getByRole("table", { name: "Current and proposed track metadata" });
  expect(table).toHaveTextContent("Current title");
  expect(table).toHaveTextContent("Proposed title");
  expect(table.querySelectorAll("tbody tr")).toHaveLength(2);
  expect(table.querySelector("tbody tr:last-child td:last-child strong")).toHaveTextContent("Canonical");
  expect(table.querySelector("tbody tr:last-child td:last-child strong")).toHaveClass("track-changed");
  expect(document.querySelector("[data-review-execute]")?.closest("footer")).not.toBeNull();
  expect(document.querySelector(".dialog-body footer")).toBeNull();
});
