import type { ReactElement } from "react";
import { ToastProvider } from "./Toasts";
import { cleanup, fireEvent, render as renderComponent, screen, within, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Page } from "./pages";

const render = (ui: ReactElement) => renderComponent(ui, { wrapper: ToastProvider });

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("settings and managed album presentation", () => {
  it("labels settings concepts with accessible help and preserves configured inbox controls", () => {
    render(<Page boot={{ page: "settings", buildSha: "abc", threshold: 88, data: { settings: { library_path: "/music" }, inboxes: [{ id: "one", name: "Downloads", path: "/inbox", auto_match: true }], ftintitle: { enabled: true } } }}/>);
    const help = screen.getByRole("button", { name: "Help: Inboxes" });
    expect(help).toHaveAttribute("title", expect.stringContaining("source folders"));
    fireEvent.click(help);
    expect(screen.getByRole("note")).toHaveTextContent("source folders");
    expect(screen.getByLabelText("Library path")).toHaveValue("/music");
    fireEvent.click(screen.getByRole("tab", { name: "Metadata" }));
    expect(screen.getByRole("heading", { name: /^Matching/ })).toBeInTheDocument();
    expect(screen.getByRole("listbox", { name: "Automatically find matches for" })).toHaveValue(["one"]);
    fireEvent.click(screen.getByRole("tab", { name: "Metadata" }));
    expect(screen.getByRole("heading", { name: /Feature in Title/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Advanced" }));
    expect(screen.getByLabelText("Beets configuration")).toBeInTheDocument();
  });

  it("opens album details read-only and keeps metadata behind Edit while artwork is directly available", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ id: 7, album: "Album", albumartist: "Artist", genre: "Rock", year: 2024, fingerprint: "fp", artwork_url: "/art.jpg", tracks: [{ id: 1, track: 1, title: "Song", artist: "Artist" }] }) })));
    render(<Page boot={{ page: "library", buildSha: "abc", threshold: 90, data: { summary: {}, albums: [{ id: 7, album: "Album", albumartist: "Artist", year: 2024 }] } }}/>);
    fireEvent.click(screen.getByRole("button", { name: /Album/ }));
    expect(await screen.findByAltText("Artwork for Album")).toHaveAttribute("src", "/art.jpg");
    expect(screen.getByLabelText("Upload replacement")).toBeInTheDocument();
    expect(screen.queryByLabelText("Genre")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit metadata" }));
    expect(screen.getByLabelText("Genre")).toHaveValue("Rock");
    expect(screen.getByLabelText("Upload replacement")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel changes" }));
    expect(screen.queryByLabelText("Genre")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "MusicBrainz rematch" })).toBeInTheDocument();
  });
  it.each([false, true])("previews exact tracks and confirms the selected removal option (%s)", async deleteFiles => {
    const calls: {url: string; body?: any}[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      const body = url.endsWith("/removal/preview") ? { preview_id: "remove-1", fingerprint: "fp", delete_allowed: true, tracks: [{ id: 1, title: "Song", relative_path: "Artist/Album/01 Song.wav" }] }
        : url.endsWith("/removal/apply") ? { status: "complete", message: "Album removed.", removed: ["Song"], remaining: [], errors: [] }
        : { id: 7, album: "Album", fingerprint: "fp", tracks: [] };
      return { ok: true, json: async () => body };
    }));
    render(<Page boot={{ page: "library", buildSha: "abc", threshold: 90, data: { summary: {}, albums: [{ id: 7, album: "Album" }] } }}/>);
    fireEvent.click(screen.getByRole("button", { name: /Album/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Remove from library" }));
    expect(await screen.findByText(/Artist\/Album\/01 Song.wav/)).toBeInTheDocument();
    const checkbox = screen.getByLabelText("Also delete files");
    expect(checkbox).not.toBeChecked();
    expect(calls.some(call => call.url.endsWith("/apply"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Cancel removal" }));
    expect(screen.queryByLabelText("Also delete files")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove from library" }));
    await screen.findByLabelText("Also delete files");
    if (deleteFiles) fireEvent.click(screen.getByLabelText("Also delete files"));
    fireEvent.click(screen.getByRole("button", { name: deleteFiles ? "Delete listed files and remove album" : "Remove from Cratekeep; keep files" }));
    await screen.findByText("Album removed.");
    expect(calls.filter(call => call.url.endsWith("/removal/apply"))).toEqual([{ url: "/api/library/albums/7/removal/apply", body: { preview_id: "remove-1", expected_fingerprint: "fp", delete_files: deleteFiles, confirmed: true } }]);
  });

});

it("uses one Inbox queue without the retired Imported/Undo/source-deletion flow", async () => {
  const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ albums: [], total: 0, page: 1, page_count: 1 }) }));
  vi.stubGlobal("fetch", fetch);
  render(<Page boot={{ page: "inbox", buildSha: "test", threshold: 90, data: { inboxes: [{ id: "one", name: "Manual" }] } }}/>);
  expect(await screen.findByText("Nothing waiting here")).toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "Imported" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Delete imported" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Delete selected" })).not.toBeInTheDocument();
  expect(fetch).toHaveBeenCalledWith("/api/inbox/one/queue?page=1&q=&status=all", expect.anything());
});

it("shows compact library rows and preserves search/sort/filter state in pagination", () => {
  render(<Page boot={{ page: "library", buildSha: "test", threshold: 90, data: { summary: { albums: 26 }, albums: [{ id: 7, album: "Record", albumartist: "Artist", year: 2024, track_count: 2, genre: "Jazz" }], query: "Song", sort: "year", order: "desc", artist_filter: "Artist", album_filter: "Record", page: 2, page_count: 3, page_size: 25 } }}/>);
  expect(screen.getByRole("button", { name: /Record Artist/ })).toHaveClass("library-record");
  expect(screen.getByText("Page 2 of 3 · 25 per page")).toBeInTheDocument();
  const previous = new URL(screen.getByRole("link", { name: "Previous" }).getAttribute("href")!, "http://localhost");
  expect(Object.fromEntries(previous.searchParams)).toEqual({ q: "Song", sort: "year", order: "desc", artist: "Artist", album: "Record", page: "1" });
  expect(screen.getByRole("combobox", { name: "Sort order" })).toHaveValue("desc");
});

it("edits a track from the compact table using the existing fingerprint contract", async () => {
  const album = { id: 7, album: "Record", albumartist: "Artist", fingerprint: "fp", tracks: [{ id: 11, track: 1, title: "First", artist: "Artist" }, { id: 12, track: 2, title: "Second", artist: "Guest" }] };
  const fetch = vi.fn(async (url: string, init?: RequestInit) => ({ ok: true, json: async () => init?.method === "PATCH" ? { album: { ...album, tracks: [{ ...album.tracks[0], title: "Corrected" }, album.tracks[1]] } } : album }));
  vi.stubGlobal("fetch", fetch);
  render(<Page boot={{ page: "library", buildSha: "test", threshold: 90, data: { albums: [album] } }}/>);
  fireEvent.click(screen.getByRole("button", { name: /Record Artist/ }));
  expect(await screen.findByRole("table", { name: "Tracks on Record" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Edit track First" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Edit metadata" }));
  fireEvent.click(screen.getByRole("button", { name: "Edit track First" }));
  expect(screen.getByLabelText("Title")).toHaveValue("First");
  const editedRow = screen.getByLabelText("Title").closest("tr")!;
  expect(within(editedRow).getByLabelText("Artist")).toHaveValue("Artist");
  expect(within(editedRow).getByLabelText("Track")).toHaveValue(1);
  expect(within(editedRow).getByLabelText("Disc")).toBeInTheDocument();
  expect(document.querySelector(".track-editor")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Edit track Second" }));
  expect(screen.getByLabelText("Title")).toHaveValue("Second");
  fireEvent.click(screen.getByRole("button", { name: "Edit track First" }));
  fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Discarded" } });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByLabelText("Title")).not.toBeInTheDocument();
  expect(fetch.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "Edit track First" }));
  fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Corrected" } });
  fireEvent.change(screen.getByLabelText("Artist"), { target: { value: "Corrected artist" } });
  fireEvent.change(screen.getByLabelText("Track"), { target: { value: "3" } });
  fireEvent.change(screen.getByLabelText("Disc"), { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "Save track" }));
  expect(await screen.findByText("Track metadata saved.")).toBeInTheDocument();
  expect(fetch).toHaveBeenCalledWith("/api/library/albums/7/tracks/11", expect.objectContaining({ body: JSON.stringify({ expected_fingerprint: "fp", title: "Corrected", artist: "Corrected artist", track: 3, disc: 2 }) }));
});

it("keeps inline track changes available after a failed save", async () => {
  const album = { id: 7, album: "Record", albumartist: "Artist", fingerprint: "fp", tracks: [{ id: 11, track: 1, disc: 1, title: "First", artist: "Artist" }] };
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => init?.method === "PATCH" ? { ok: false, status: 409, json: async () => ({ error: "Metadata changed; reload before saving." }) } : { ok: true, json: async () => album }));
  render(<Page boot={{ page: "library", buildSha: "test", threshold: 90, data: { albums: [album] } }}/>);
  fireEvent.click(screen.getByRole("button", { name: /Record Artist/ }));
  fireEvent.click(await screen.findByRole("button", { name: "Edit metadata" }));
  fireEvent.click(screen.getByRole("button", { name: "Edit track First" }));
  fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Keep my draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Save track" }));
  expect(await screen.findByText("Metadata changed; reload before saving.")).toBeInTheDocument();
  expect(screen.getByLabelText("Title")).toHaveValue("Keep my draft");
  expect(screen.getByRole("button", { name: "Save track" })).toBeEnabled();
});

it.each(["123e4567-e89b-42d3-a456-426614174000", ""])("shows the stored album release MBID in Library details: %s", async mbid => {
  const album = { id: 7, album: "Record", albumartist: "Artist", mb_albumid: mbid, tracks: [] };
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => album })));
  render(<Page boot={{ page: "library", buildSha: "test", threshold: 90, data: { albums: [album] } }}/>);
  fireEvent.click(screen.getByRole("button", { name: /Record Artist/ }));
  expect(await screen.findByText("MusicBrainz release MBID")).toBeInTheDocument();
  if (mbid) expect(screen.getByRole("link", { name: mbid })).toHaveAttribute("href", `https://musicbrainz.org/release/${mbid}`);
  else expect(screen.getByText("Not associated with a MusicBrainz release")).toBeInTheDocument();
});

it("shows Library size in the overview and keeps Inbox size with source cards", () => {
  render(<Page boot={{ page: "overview", buildSha: "test", threshold: 90, data: { library_summary: { bytes: 1024, albums: 3, tracks: 7 }, inbox_bytes: 8192, inbox_album_count: 2, inbox_file_count: 4, inbox_summaries: [{ id: "one", name: "Manual", path: "/fixture/inbox", pending_albums: 2, files: 4, bytes: 8192, status: "needs review" }] } }}/>);
  const metrics = document.querySelector(".metric-grid") as HTMLElement;
  expect(within(metrics).getByText("Library size")).toBeInTheDocument();
  expect(within(metrics).getByText("1.0 KB")).toBeInTheDocument();
  expect(within(metrics).queryByText("Inbox size")).not.toBeInTheDocument();
  const status = screen.getByRole("region", { name: "Inbox status" });
  expect(within(status).getByRole("heading", { name: "Inbox size" })).toBeInTheDocument();
  expect(screen.getByRole("article", { name: "Manual status" })).toHaveTextContent("8.0 KB");
});

it("opens and closes album details in the inspector while retaining the list", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ id: 7, album: "Record", tracks: [], fingerprint: "fp" }) })));
  render(<Page boot={{ page: "library", buildSha: "test", threshold: 90, data: { albums: [{ id: 7, album: "Record", albumartist: "Artist" }] } }}/>);
  const row = screen.getByRole("button", { name: /Record Artist/ });
  fireEvent.click(row);
  expect(await screen.findByRole("region", { name: "Record" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(row).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(screen.getByRole("button", { name: "Close album" }));
  expect(screen.queryByRole("region", { name: "Record" })).not.toBeInTheDocument();
  expect(row).toHaveAttribute("aria-pressed", "false");
});

it("blocks album switching and repeat actions until a save finishes", async () => {
  let finish: (() => void) | undefined;
  const album = { id: 7, album: "Record", albumartist: "Artist", fingerprint: "fp", tracks: [] };
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "PATCH") await new Promise<void>(resolve => { finish = resolve; });
    return { ok: true, json: async () => album };
  }));
  render(<Page boot={{ page: "library", buildSha: "test", threshold: 90, data: { albums: [album, { id: 8, album: "Other", albumartist: "Artist" }] } }}/>);
  fireEvent.click(screen.getByRole("button", { name: /Record Artist/ }));
  fireEvent.click(await screen.findByRole("button", { name: "Edit metadata" }));
  fireEvent.click(screen.getByRole("button", { name: "Save album" }));
  await waitFor(() => expect(screen.getByRole("button", { name: /Other Artist/ })).toBeDisabled());
  expect(screen.getByRole("button", { name: "Save album" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Close album" })).toBeDisabled();
  finish?.();
  await screen.findByText("Album metadata saved.");
  await waitFor(() => expect(screen.getByRole("button", { name: /Other Artist/ })).toBeEnabled());
});

it("renders artist browse rows with album counts, genres, drill-down and paginated view state", () => {
  render(<Page boot={{ page: "library", buildSha: "test", threshold: 90, data: { view: "artists", artists: [{ artist: "Élan & Co", album_count: 3, genres: "Jazz, Soul" }], matching_artists: 26, page: 1, page_count: 2 } }}/>);
  expect(screen.getByRole("heading", { name: "Browse artists" })).toBeInTheDocument();
  const row = screen.getByRole("link", { name: /Élan & Co 3 Jazz, Soul/ });
  expect(new URL(row.getAttribute("href")!, "http://localhost").searchParams.get("artist")).toBe("Élan & Co");
  expect(new URL(screen.getByRole("link", { name: "Next" }).getAttribute("href")!, "http://localhost").searchParams.get("view")).toBe("artists");
  expect(screen.queryByRole("complementary", { name: "Album inspector" })).not.toBeInTheDocument();
});

it("keeps Inbox identities, edits and multiselect choices together through row changes", () => {
  location.hash = "";
  render(<Page boot={{ page: "settings", buildSha: "test", threshold: 90, data: { inboxes: [{ id: "one", name: "Downloads", path: "/downloads", auto_match: true }, { id: "two", name: "Manual", path: "/manual", auto_match: false }] } }}/>);
  const table = screen.getByRole("table", { name: "Configured Inboxes" });
  expect(within(table).queryByRole("textbox")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Edit Downloads" }));
  expect(screen.getByRole("button", { name: "Save settings" })).toBeDisabled();
  fireEvent.change(within(table).getByLabelText("Name for Downloads"), { target: { value: "Renamed" } });
  fireEvent.change(within(table).getByLabelText("Inbox path for Downloads"), { target: { value: "/new-downloads" } });
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  fireEvent.click(screen.getByRole("button", { name: "+ Add inbox" }));
  fireEvent.change(screen.getByLabelText("Name for Inbox 3"), { target: { value: "New source" } });
  fireEvent.change(screen.getByLabelText("Inbox path for Inbox 3"), { target: { value: "/new-source" } });
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete Manual" }));
  expect(screen.getByText("/new-downloads")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "Metadata" }));
  const select = screen.getByRole("listbox", { name: "Automatically find matches for" });
  expect(select).toHaveValue(["one"]);
  expect(screen.getByRole("option", { name: "Renamed" })).toHaveValue("one");
  expect(screen.getByRole("option", { name: "New source (save first)" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Clear selection" }));
  expect(select).toHaveValue([]);
  fireEvent.click(screen.getByRole("button", { name: "Select all" }));
  expect(select).toHaveValue(["one"]);
  const form = screen.getByRole("button", { name: "Save settings" }).closest("form")!;
  const payload = new FormData(form);
  expect(payload.getAll("inbox_id")).toEqual(["one", ""]);
  expect(payload.getAll("inbox_name")).toEqual(["Renamed", "New source"]);
  expect(payload.getAll("inbox_auto_match_id")).toEqual(["one"]);
});

it("puts explanatory Settings text in keyboard and touch accessible help", () => {
  location.hash = "";
  render(<Page boot={{ page: "settings", buildSha: "test", threshold: 90, data: {} }}/>);
  expect(screen.queryByRole("heading", { name: /Navidrome/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "Matching" })).not.toBeInTheDocument();
  const help = screen.getByRole("button", { name: "Help: Inboxes" });
  fireEvent.click(help);
  expect(screen.getByRole("note")).toHaveTextContent("Deleting a row does not delete files");
  fireEvent.keyDown(help, { key: "Escape" });
  expect(screen.queryByRole("note")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "Metadata" }));
  fireEvent.click(screen.getByRole("button", { name: "Help: Feature in Title" }));
  expect(screen.getByRole("note")).toHaveTextContent("For example");
});

it("cancels Inbox edits and new rows without changing submitted configuration", () => {
  location.hash = "";
  render(<Page boot={{ page: "settings", buildSha: "test", threshold: 90, data: { inboxes: [{ id: "one", name: "Manual", path: "/manual", auto_match: true }] } }}/>);
  fireEvent.click(screen.getByRole("button", { name: "Edit Manual" }));
  fireEvent.change(screen.getByLabelText("Name for Manual"), { target: { value: "Discarded" } });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(within(screen.getByRole("table", { name: "Configured Inboxes" })).getByText("Manual")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "+ Add inbox" }));
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  expect(screen.getByRole("button", { name: "Save settings" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  const form = screen.getByRole("button", { name: "Save settings" }).closest("form")!;
  expect(new FormData(form).getAll("inbox_name")).toEqual(["Manual"]);
  expect(screen.getByRole("button", { name: "Save settings" })).toBeEnabled();
});

it("routes old matching links to Metadata and preserves an explicitly saved toast duration", () => {
  location.hash = "matching";
  render(<Page boot={{ page: "settings", buildSha: "test", threshold: 90, data: { settings: { toast_duration_seconds: "15" } } }}/>);
  expect(screen.getByRole("tab", { name: "Metadata" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("heading", { name: /^Matching/ })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "General" }));
  expect(screen.getByLabelText("Toast duration (seconds)")).toHaveValue(15);
});

it("links Overview source cards directly to their Inbox without exposing paths", () => {
  render(<Page boot={{ page: "overview", buildSha: "test", threshold: 90, data: { inbox_summaries: [{ id: "source one", name: "Manual", path: "/private/source", files: 2, pending_albums: 1 }] } }}/>);
  expect(screen.getByRole("link", { name: "Open Manual Inbox" })).toHaveAttribute("href", "/inbox?source=source%20one");
  expect(screen.queryByText("/private/source")).not.toBeInTheDocument();
});

it("selects the configured Inbox requested by the source link", async () => {
  history.replaceState(null, "", "/inbox?source=two");
  const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ albums: [], total: 0, page: 1, page_count: 1 }) }));
  vi.stubGlobal("fetch", fetch);
  render(<Page boot={{ page: "inbox", buildSha: "test", threshold: 90, data: { inboxes: [{ id: "one", name: "First" }, { id: "two", name: "Second" }] } }}/>);
  expect(screen.getByRole("tab", { name: "Second" })).toHaveAttribute("aria-selected", "true");
  await screen.findByText("Nothing waiting here");
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/inbox/two/queue"), expect.anything());
  history.replaceState(null, "", "/");
});


it("submits album edits from the fixed footer with the required fingerprint", async () => {
  const patch = vi.fn();
  const album = { id: 7, album: "Album", albumartist: "Artist", genre: "Rock", year: 2024, fingerprint: "current-fp", tracks: [] };
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "PATCH") patch(JSON.parse(String(init.body)));
    return { ok: true, json: async () => album };
  }));
  render(<Page boot={{ page: "library", buildSha: "test", threshold: 90, data: { albums: [album] } }}/>);
  fireEvent.click(screen.getByRole("button", { name: /Album Artist/ }));
  fireEvent.click(await screen.findByRole("button", { name: "Edit metadata" }));
  const save = screen.getByRole("button", { name: "Save album" });
  expect(save.closest("footer")).toHaveAttribute("aria-label", "Album actions");
  expect(save).toHaveAttribute("form", "album-form");
  fireEvent.click(save);
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ expected_fingerprint: "current-fp", album: "Album", albumartist: "Artist", genre: "Rock", year: 2024 }));
});


describe("About settings", () => {
  it("shows the real build, community links and an explicit default-off analytics choice", () => {
    location.hash = "about";
    render(<Page boot={{ page: "settings", buildSha: "abcdef1234567890", threshold: 90, data: { settings: {}, about: { build: "abcdef1234567890", discord_url: "https://discord.gg/cratekeep-test" } } }}/>);
    expect(screen.getByRole("tab", { name: "About" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Build abcdef123456")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Join the Discord" })).toHaveAttribute("href", "https://discord.gg/cratekeep-test");
    const toggle = screen.getByRole("checkbox", { name: "Share anonymous install analytics" });
    expect(toggle).not.toBeChecked();
    expect(screen.getByText(/No artist or track names/)).toBeInTheDocument();
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("tab", { name: "General" }));
    fireEvent.click(screen.getByRole("tab", { name: "About" }));
    expect(toggle).toBeChecked();
    expect(screen.getByRole("button", { name: "Save settings" })).toBeEnabled();
    location.hash = "";
  });
  it("shows the release version alongside its source build", () => {
    location.hash = "about";
    render(<Page boot={{ page: "settings", buildSha: "abcdef1234567890", threshold: 90, data: { settings: {}, about: { version: "1.0.0", build: "abcdef1234567890" } } }}/>);
    expect(screen.getByText("1.0.0 · Build abcdef123456")).toBeInTheDocument();
    location.hash = "";
  });
  it("retains saved analytics consent and labels an unknown build honestly", () => {
    location.hash = "about";
    render(<Page boot={{ page: "settings", buildSha: "unknown", threshold: 90, data: { settings: { analytics_enabled: "1" }, about: { build: "unknown", discord_url: "https://discord.gg/cratekeep-test" } } }}/>);
    expect(screen.getByText("Development")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Share anonymous install analytics" })).toBeChecked();
    location.hash = "";
  });
});
