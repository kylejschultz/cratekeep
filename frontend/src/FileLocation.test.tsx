import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { FileLocation } from "./FileLocation";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const album = { id: 7, tracks: [{ id: 11, relative_path: 'Old/Album/01 Song.flac' }] };
const info = { directory: '', source_directory: 'Old/Album', beets_paths: [{ item_id: 11, to: 'New/Album/01 Song.flac' }] };

it('browses Library folders and stages an exact new album folder without writing', async () => {
  const choose = vi.fn();
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    expect(init?.method).toBeUndefined();
    return { ok: true, json: async () => url.endsWith('/location') ? info : url.endsWith('path=Artist') ? { path: 'Artist', entries: [] } : { path: '', entries: [{ name: 'Artist', path: 'Artist' }] } };
  });
  vi.stubGlobal('fetch', fetch);
  render(<FileLocation album={album} onChoose={choose} disabled={false}/>);
  await screen.findByText('Library / Old/Album');
  fireEvent.click(screen.getByRole('button', { name: 'Browse folders' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Artist' }));
  await screen.findByText('No subfolders.');
  fireEvent.change(screen.getByLabelText('New album folder'), { target: { value: 'Chosen Album' } });
  fireEvent.click(screen.getByRole('button', { name: 'Use new folder' }));
  expect(choose).toHaveBeenCalledWith({ mode: 'manual', directory: 'Artist/Chosen Album' });
  expect(fetch.mock.calls).toHaveLength(3);
});

it('explains ambiguous layouts but still allows explicit Beets organization', async () => {
  const choose = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ ...info, manual_error: 'Files span separate album folders.' }) })));
  render(<FileLocation album={album} onChoose={choose} disabled={false}/>);
  await screen.findByText('Files span separate album folders.');
  expect(screen.getByRole('button', { name: 'Browse folders' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Use Beets naming' }));
  expect(choose).toHaveBeenCalledWith({ mode: 'beets' });
});
