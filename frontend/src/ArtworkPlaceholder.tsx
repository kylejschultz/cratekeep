/** Neutral record sleeve for missing artwork; actual covers always take priority. */
export function ArtworkPlaceholder() {
  return <svg className="artwork-placeholder" viewBox="0 0 32 32" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true"><rect x="3" y="3" width="26" height="26" rx="1"/><circle cx="16" cy="16" r="9"/><circle cx="16" cy="16" r="2"/><path d="M10 10a8 8 0 0 1 6-2M22 22a8 8 0 0 1-6 2"/></svg>;
}
