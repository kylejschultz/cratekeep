import { fireEvent, render, screen, waitFor, within, cleanup } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MetadataEditor } from "./MetadataEditor";

const album = { id: 7, album: "Record", albumartist: "gnash", albumartists: ["gnash", "Garrett Nash"], genre: "Pop", year: 2024, fingerprint: "fp", tracks: [
  { id: 11, title: "First", artist: "gnash", track: 1, disc: 1 },
  { id: 12, title: "Second", artist: "Guest", track: 2, disc: 1 },
] };
const plan = { plan_fingerprint: "plan-fp", changes: [{ item_id: 11, title: "First", changes: { title: { from: "First", to: "Corrected" } } }], paths: [{ item_id: 11, from: "Record/01 First.flac", to: "Record/03 Corrected.flac" }], related_files: [] };
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("retains drafts across album/track changes, previews, then submits one bound save", async () => {
  const saved = vi.fn(), fetch = vi.fn(async (url: string, init?: RequestInit) => { void init; return { ok: true, json: async () => url.endsWith("/preview") ? plan : { album } }; });
  vi.stubGlobal("fetch", fetch);
  render(<MetadataEditor album={album} onClose={vi.fn()} onSaved={saved} onBusyChange={vi.fn()}/>);
  const modal = screen.getByRole("dialog", { name: "Edit metadata" });
  fireEvent.change(within(modal).getByLabelText("Album"), { target: { value: "Renamed" } });
  fireEvent.click(screen.getByRole("button", { name: "Remove Garrett Nash" }));
  fireEvent.click(screen.getByRole("button", { name: "Tracks (2)" }));
  fireEvent.change(screen.getAllByLabelText("Title")[0], { target: { value: "Corrected" } });
  fireEvent.change(screen.getAllByLabelText("Track", { selector: "input" })[0], { target: { value: "3" } });
  fireEvent.change(screen.getByLabelText("Choose track"), { target: { value: "12" } });
  fireEvent.change(screen.getAllByLabelText("Artist", { selector: "input" })[1], { target: { value: "Other Guest" } });
  fireEvent.change(screen.getByLabelText("Choose track"), { target: { value: "11" } });
  expect(screen.getAllByLabelText("Title")[0]).toHaveValue("Corrected");
  fireEvent.click(screen.getByRole("button", { name: "Album details" }));
  expect(screen.getByLabelText("Album")).toHaveValue("Renamed");
  expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
  await screen.findByRole("button", { name: "Save changes" });
  expect(screen.getByText("Record/03 Corrected.flac", { exact: false })).toBeInTheDocument();
  const payload = { expected_fingerprint: "fp", album: { album: "Renamed", albumartists: ["gnash"] }, tracks: [{ id: 11, title: "Corrected", track: 3 }, { id: 12, artist: "Other Guest" }] };
  expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual(payload);
  fireEvent.click(screen.getByRole("button", { name: "Back to editing" }));
  expect(screen.getByLabelText("Album")).toHaveValue("Renamed");
  fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
  fireEvent.click(await screen.findByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(saved).toHaveBeenCalledWith(album));
  expect(fetch).toHaveBeenLastCalledWith("/api/library/albums/7/metadata/apply", expect.objectContaining({ body: JSON.stringify({ ...payload, plan_fingerprint: "plan-fp" }) }));
});

it("confirms discard and restores the previous focus without making requests", () => {
  const close = vi.fn();
  const trigger = document.createElement("button"); document.body.append(trigger); trigger.focus();
  const view = render(<MetadataEditor album={album} onClose={close} onSaved={vi.fn()} onBusyChange={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText("Album"), { target: { value: "Draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Cancel changes" }));
  expect(screen.getByText("Your files have not been changed.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
  expect(screen.getByLabelText("Album")).toHaveValue("Draft");
  fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
  fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
  expect(close).toHaveBeenCalledOnce();
  view.unmount(); expect(trigger).toHaveFocus(); trigger.remove();
});

it("retains drafts after a failed apply and requires a fresh review", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("/apply") ? { ok: false, status: 409, json: async () => ({ error: "Metadata changed; review again." }) } : { ok: true, json: async () => plan }));
  render(<MetadataEditor album={album} onClose={vi.fn()} onSaved={vi.fn()} onBusyChange={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText("Album"), { target: { value: "Keep this draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
  fireEvent.click(await screen.findByRole("button", { name: "Save changes" }));
  expect(await screen.findByText("Metadata changed; review again.")).toBeInTheDocument();
  expect(screen.getByLabelText("Album")).toHaveValue("Keep this draft");
  expect(screen.getByRole("button", { name: "Review changes" })).toBeEnabled();
});

it("locks close and review while a request is pending", async () => {
  let finish: ((value: unknown) => void) | undefined;
  vi.stubGlobal("fetch", vi.fn(() => new Promise(resolve => { finish = resolve; })));
  const busy = vi.fn();
  render(<MetadataEditor album={album} onClose={vi.fn()} onSaved={vi.fn()} onBusyChange={busy}/>);
  fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
  expect(screen.getByRole("button", { name: "Close metadata editor" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled();
  expect(busy).toHaveBeenCalledWith(true);
  finish?.({ ok: true, json: async () => plan });
  await screen.findByRole("button", { name: "Save changes" });
});

it("reviews and saves a location-rule change even when no files or tags move", async () => {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    void init;
    return { ok: true, json: async () => url.endsWith('/location') ? { directory: 'Pinned/Album', source_directory: 'Pinned/Album', beets_paths: [] }
      : url.endsWith('/preview') ? { changes: [], paths: [], related_files: [], location_change: { from: 'Pinned/Album', to: 'Beets naming' }, plan_fingerprint: 'folder-plan' }
      : { album } };
  });
  vi.stubGlobal('fetch', fetch);
  const saved = vi.fn();
  render(<MetadataEditor album={album} onClose={vi.fn()} onSaved={saved} onBusyChange={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button', { name: 'File location' }));
  await screen.findByText('Manual album folder');
  fireEvent.click(screen.getByRole('button', { name: 'Use Beets naming' }));
  fireEvent.click(screen.getByRole('button', { name: 'Album details' }));
  fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
  const save = await screen.findByRole('button', { name: 'Save changes' });
  expect(save).toBeEnabled();
  expect(JSON.parse(String(fetch.mock.calls.find(([url]) => url.endsWith('/preview'))?.[1]?.body))).toMatchObject({ location: { mode: 'beets' } });
  fireEvent.click(save);
  await waitFor(() => expect(saved).toHaveBeenCalledWith(album));
});
