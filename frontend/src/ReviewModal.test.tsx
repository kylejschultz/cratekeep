import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReviewModal } from "./ReviewModal";

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
    expect(screen.getByLabelText("Album changed")).toBeInTheDocument();
    expect(screen.getByLabelText("Artist unchanged")).toBeInTheDocument();
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
});
