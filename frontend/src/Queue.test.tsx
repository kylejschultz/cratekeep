import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Queue } from "./Queue";
import { albumStatus } from "./status";

afterEach(() => vi.unstubAllGlobals());

describe("albumStatus", () => {
  it("uses the persisted threshold without conflating candidate absence and failure", () => {
    expect(albumStatus({ id: 1 }, 90)).toMatchObject({ key: "pending", score: null });
    expect(albumStatus({ id: 1, candidates: [{ id: 2, confidence: .92 }] }, 90)).toMatchObject({ key: "matched", score: 92 });
    expect(albumStatus({ id: 1, candidates: [{ id: 2, confidence: .89 }] }, 90)).toMatchObject({ key: "threshold", score: 89 });
    expect(albumStatus({ id: 1, candidate_status: "error", candidate_error: "provider unavailable" }, 90)).toMatchObject({ key: "error", score: null });
  });
});

describe("Queue scan workflow", () => {
  it("posts Inbox scopes, refreshes expanded albums, and keeps album actions in the menu", async () => {
    let albumLoads = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/scopes") && !init?.method) return { ok: true, json: async () => ({ scopes: [{ path: "Album", name: "Album", album_count: 1, file_count: 1 }] }) };
      if (url.includes("/scopes/albums")) {
        albumLoads += 1;
        return { ok: true, json: async () => ({ albums: [{ id: 7, album: "Matched Album", artist: "Artist", candidates: [{ id: 8, confidence: .98 }] }] }) };
      }
      if (url.endsWith("/scopes/scan")) return { ok: true, json: async () => ({ inventory: { scopes: ["Album"] } }) };
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
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/inbox/inbox-one/scopes/scan", expect.objectContaining({ method: "POST", body: JSON.stringify({ scopes: ["Album"] }) })));
    await waitFor(() => expect(albumLoads).toBeGreaterThanOrEqual(2));

    const row = screen.getByText("Matched Album").closest(".album-node") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Actions for Matched Album" }));
    expect(within(row).getAllByRole("menuitem").map(item => item.textContent)).toEqual(["Scan", "Import", "Review"]);
    expect(within(row).getByRole("menuitem", { name: "Import" })).toBeEnabled();
    fireEvent.click(within(row).getByRole("menuitem", { name: "Scan" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/scopes/scan"))).toHaveLength(2));
    await waitFor(() => expect(albumLoads).toBeGreaterThanOrEqual(3));
  });
});
