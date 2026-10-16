import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Page } from "./pages";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("settings and managed album presentation", () => {
  it("labels settings concepts with accessible help and preserves configured inbox controls", () => {
    render(<Page boot={{ page: "settings", buildSha: "abc", threshold: 88, data: { settings: { library_path: "/music" }, inboxes: [{ id: "one", name: "Downloads", path: "/inbox", auto_match: true }], ftintitle: { enabled: true } } }}/>);
    const help = screen.getByRole("button", { name: "Help: Inboxes" });
    expect(help).toHaveAttribute("title", expect.stringContaining("source folders"));
    fireEvent.click(help);
    expect(screen.getByRole("note")).toHaveTextContent("source folders");
    expect(screen.getByLabelText("Library path")).toHaveValue("/music");
    fireEvent.click(screen.getByRole("tab", { name: "Matching" }));
    expect(screen.getByRole("heading", { name: /Automatic matching/ })).toBeInTheDocument();
    expect(screen.getByLabelText("Automatically find matches for Downloads")).toBeChecked();
    fireEvent.click(screen.getByRole("tab", { name: "Metadata" }));
    expect(screen.getByRole("heading", { name: /Feature in Title/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Advanced" }));
    expect(screen.getByLabelText("Beets configuration")).toBeInTheDocument();
  });

  it("opens album details read-only and gates metadata and artwork changes behind Edit", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ id: 7, album: "Album", albumartist: "Artist", genre: "Rock", year: 2024, fingerprint: "fp", artwork_url: "/art.jpg", tracks: [{ id: 1, track: 1, title: "Song", artist: "Artist" }] }) })));
    render(<Page boot={{ page: "library", buildSha: "abc", threshold: 90, data: { summary: {}, albums: [{ id: 7, album: "Album", albumartist: "Artist", year: 2024 }] } }}/>);
    fireEvent.click(screen.getByRole("button", { name: /Album/ }));
    expect(await screen.findByAltText("Artwork for Album")).toHaveAttribute("src", "/art.jpg");
    expect(screen.queryByLabelText("Upload replacement")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Genre")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("Genre")).toHaveValue("Rock");
    expect(screen.getByLabelText("Upload replacement")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel changes" }));
    expect(screen.queryByLabelText("Genre")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "MusicBrainz rematch" })).toBeInTheDocument();
  });
});
