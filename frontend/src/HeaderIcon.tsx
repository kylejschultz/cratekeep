export function HeaderIcon({ name }: { name: "settings" | "activity" | "sun" | "moon" }) {
  return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
    {name === "settings" && <><path d="M10 2h4l.6 3 2 1.2 2.8-.9 2 3.4-2.2 2v2.6l2.2 2-2 3.4-2.8-.9-2 1.2L14 22h-4l-.6-3-2-1.2-2.8.9-2-3.4 2.2-2v-2.6l-2.2-2 2-3.4 2.8.9 2-1.2Z"/><circle cx="12" cy="12" r="3"/></>}
    {name === "activity" && <path d="M2 12h5l3-8 4 16 3-8h5"/>}
    {name === "sun" && <><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></>}
    {name === "moon" && <path d="M20.8 13A9 9 0 0 1 11 3.2 9 9 0 1 0 20.8 13Z"/>}
  </svg>;
}
