# One-time Beacon analytics banner

Status: approved by Kyle, 2026-11-04, with browser-local dismissal. Existing
quiet-scan candidate 2d0f044 awaits Kyle's testing.

## Intent and inspected behavior

Ask Cratekeep users once whether they want to share optional analytics with Kyle,
following the existing Prism/Nestview pattern. Cratekeep already has a default-off
Beacon worker and an About settings toggle; reuse that consent and sender.

References inspected locally: Prism src/App.tsx AnalyticsBanner and consent helpers;
Nestview frontend/src/components/AnalyticsBanner.tsx; Cratekeep analytics.py,
Settings About UI and tests/test_analytics.py. Kyle chose browser-local dismissal
so clearing site data can restore the invitation. Analytics consent remains server-owned.

## Approved experience

- A compact, non-blocking banner below the shared header after first-run setup,
  available across app pages until answered. Do not interrupt setup, file work,
  navigation or review modals. Match the existing quiet app styling/mobile layout.
- Title: "Help improve Cratekeep".
- Copy: "Share anonymous install details and library totals with me through Beacon.
  No music names, paths or listening history are sent."
- Actions: "Share analytics", "No thanks", and "What's shared" linking to
  Settings → About's existing explanation. Reading the explanation is not consent
  or dismissal; the banner remains until an explicit choice.
- Share analytics enables existing server-owned Beacon consent through a dedicated
  endpoint, wakes the existing worker and records browser-local dismissal after
  successful persistence. Never send on the request thread or add a second sender.
- No thanks dismisses in browser local storage without altering server consent.
  It survives reloads/restarts/updates in that browser. Clearing site data restores
  the invitation if analytics are still disabled. Another browser can see it.
- Already-enabled installations never show the banner. Disabled installations show
  it until dismissed in that browser. Settings retains the existing consent toggle.
- While saving, disable duplicate choices. If persistence fails, keep the banner
  visible and show a concise retryable error; do not claim consent was saved.

"One-time" means no prompting again after the user responds, rather than hiding
it merely because it rendered once. This follows the reference apps' behavior.

## Privacy and implementation constraints

Use only the existing payload: random installation ID, build/version, architecture,
OS, timestamp/dev channel, and aggregate artist/album/song counts. Do not add events,
media names, paths, account details, listening history or a new third-party SDK.
Keep current once-daily limits, timeout/error isolation and consent recheck before
network transmission. The transport receives a connection IP as described in the
existing Settings copy; do not copy Nestview's absolute "no IP addresses" claim.
No install ID or analytics request before opt-in. Do not expose internal install
IDs/last-attempt state in page bootstrap. Keep consent server-owned and dismissal
browser-local. The dedicated endpoint only enables analytics; never submit stale full settings from the banner.
Do not change analytics on Kyle's live installation while testing.

## Acceptance and validation

- [x] Fresh/default-off configured installations show the banner; setup and existing
      opted-in installations and dismissed browsers do not.
- [x] Opt-in persists and reaches the existing worker; dismissal persists with no
      network request or install-ID creation. Both suppress future prompting.
- [x] Reload, restart and updates retain browser dismissal; clearing site storage
      restores it while disabled. Another browser sees its own invitation.
- [x] Settings opt-in suppresses the banner regardless of browser dismissal.
- [x] Viewing disclosure or saving unrelated settings does not silently answer.
- [x] Save failures remain visible/retryable and duplicate clicks cannot conflict.
- [x] Actual data disclosure matches the current sender; default-off and daily
      sender/privacy tests still pass without real Beacon traffic.
- [x] Keyboard, mobile, dark/light and existing review/navigation flows work.

Use a stub Beacon transport for tests. Run relevant backend/frontend gates and
focused browser checks; do not opt real installations into analytics for validation.
After approval, deliver the normal combined linux/amd64 local-registry candidate
with source/digest verification and a focused checklist. Keep this as a distinct
feature commit so it remains separable from metadata/progress work. No merge,
release, deployment or PR publication is authorized by this planning request.

Implementation validation: frontend consent/error/storage regression tests, backend
setup/consent/restart/privacy tests and real-browser checks at 1440/768/390 in dark
and light themes. Network transport stubbed; no live installation opted in. User
acceptance remains pending, including the final candidate in Kyle's environment.

### Same-page disclosure correction — 2026-11-04

Kyle found that “What’s shared” did not open About when already on Settings.
Reproduced in a real browser: the hash changed but About stayed unselected because
Settings only read the hash on mount. Settings must follow hash changes (including
history navigation and the existing matching alias) without remounting or losing
unsaved fields. Added a regression test and repeated the banner flow starting on
Settings, rather than relying only on the previous Overview-to-Settings check.
