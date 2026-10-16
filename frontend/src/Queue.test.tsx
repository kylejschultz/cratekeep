import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Queue } from "./Queue";
import { albumStatus } from "./status";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("albumStatus", () => {
  it("uses the persisted threshold without conflating candidate absence and failure", () => {
    expect(albumStatus({ id: 1 }, 90)).toMatchObject({ key: "pending", score: null });
    expect(albumStatus({ id: 1, candidates: [{ id: 2, confidence: .92 }] }, 90)).toMatchObject({ key: "matched", score: 92 });
    expect(albumStatus({ id: 1, candidates: [{ id: 2, confidence: .89 }] }, 90)).toMatchObject({ key: "threshold", score: 89 });
    expect(albumStatus({ id: 1, candidate_status: "error", candidate_error: "provider unavailable" }, 90)).toMatchObject({ key: "error", score: null });
  });
});

describe("Queue match workflow", () => {
  it("paginates scopes, retains selection, and exposes album-level mixed selection", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/library-import/folders") return { ok: true, json: async () => ({ folders: [{ path: "First", name: "First", album_count: 2 }], page: 1, page_count: 2, total: 26 }) };
      if (url === "/api/library-import/folders?page=2") return { ok: true, json: async () => ({ folders: [{ path: "Last", name: "Last", album_count: 1 }], page: 2, page_count: 2, total: 26 }) };
      if (url.includes("folders/albums?folder=First")) return { ok: true, json: async () => ({ albums: [{ id: 1, album: "One" }, { id: 2, album: "Two" }] }) };
      if (url === "/api/library-import/bulk/exceptions" && init?.method === "POST") return { ok: true, json: async () => ({ albums: [] }) };
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Queue source="library-import" threshold={90}/>);

    fireEvent.click(await screen.findByRole("checkbox", { name: "Select folder First" }));
    fireEvent.click(screen.getByRole("button", { name: "Expand folder First" }));
    const albumTwo = await screen.findByRole("checkbox", { name: "Select album Two" });
    expect(albumTwo).toBeChecked();
    fireEvent.click(albumTwo);
    expect(screen.getByRole("checkbox", { name: "Select folder First" })).toBePartiallyChecked();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Page 2 of 2")).toBeInTheDocument();
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/library-import/bulk/exceptions", expect.objectContaining({ body: JSON.stringify({ folders: ["First"], excluded_album_ids: [2] }) })));
  });

  it("posts Inbox scopes, exposes busy rows, refreshes albums, and uses Match wording", async () => {
    let albumLoads = 0;
    let releaseMatch: (() => void) | undefined;
    let matchCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/scopes") && !init?.method) return { ok: true, json: async () => ({ scopes: [{ path: "Album", name: "Album", album_count: 1, file_count: 1 }] }) };
      if (url.includes("/scopes/albums")) {
        albumLoads += 1;
        return { ok: true, json: async () => ({ albums: [{ id: 7, album: "Matched Album", artist: "Artist", candidates: [{ id: 8, confidence: .98 }] }] }) };
      }
      if (url.endsWith("/scopes/scan")) {
        matchCalls += 1;
        await new Promise<void>(resolve => { releaseMatch = resolve; });
        return { ok: true, json: async () => ({ inventory: { scopes: ["Album"] } }) };
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Queue source="inbox" threshold={90} inboxId="inbox-one"/>);

    fireEvent.click(await screen.findByRole("button", { name: "Expand folder Album" }));
    const album = await screen.findByText("Matched Album");
    expect(screen.getByRole("button", { name: "Match Selected" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Review" })).toBeDisabled();
    expect(within(album.closest(".album-node") as HTMLElement).queryByRole("button", { name: /^Import/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox", { name: "Select folder Album" }));
    fireEvent.click(screen.getByRole("button", { name: "Match Selected" }));
    expect(screen.getByRole("button", { name: "Matching…" })).toBeDisabled();
    expect(screen.getByText("Album").closest(".folder-node")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();
    releaseMatch?.();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/inbox/inbox-one/scopes/scan", expect.objectContaining({ method: "POST", body: JSON.stringify({ scopes: ["Album"] }) })));
    await waitFor(() => expect(albumLoads).toBeGreaterThanOrEqual(2));
    expect(await screen.findByText("1 folder matched.")).toBeInTheDocument();

    const row = screen.getByText("Matched Album").closest(".album-node") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Actions for Matched Album" }));
    expect(within(row).getAllByRole("menuitem").map(item => item.textContent)).toEqual(["Match", "Import", "Review"]);
    expect(within(row).getByRole("menuitem", { name: "Import" })).toBeEnabled();
    fireEvent.click(within(row).getByRole("menuitem", { name: "Match" }));
    expect(within(row).getByRole("menuitem", { name: "Matching…" })).toBeDisabled();
    expect(row).toHaveAttribute("aria-busy", "true");
    releaseMatch?.();
    await waitFor(() => expect(matchCalls).toBe(2));
    await waitFor(() => expect(albumLoads).toBeGreaterThanOrEqual(3));
    expect(screen.queryByText("Scan")).not.toBeInTheDocument();
  });

  it("reports match errors and restores actions for retry", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/folders")) return { ok: true, json: async () => ({ folders: [{ path: "Failure", name: "Failure" }] }) };
      return { ok: false, status: 503, json: async () => ({ error: "provider unavailable" }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Queue source="library-import" threshold={90}/>);

    fireEvent.click(await screen.findByRole("checkbox", { name: "Select folder Failure" }));
    fireEvent.click(screen.getByRole("button", { name: "Match Selected" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Match failed: provider unavailable");
    expect(screen.getByRole("button", { name: "Match Selected" })).toBeEnabled();
  });
});
