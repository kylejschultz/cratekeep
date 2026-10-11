(() => {
  "use strict";
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const one = (root, hook) => root.querySelector(`[data-review-${hook}]`);
  const focusable = root => [...root.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),[tabindex]:not([tabindex="-1"])')].filter(node => !node.closest('[hidden]'));
  const field = (label, value) => { const node = document.createElement('span'); node.textContent = `${label}: ${value || 'Not provided'}`; return node; };
  function create({root, adapter, presentation = window.CratekeepReview, onSuccess = () => {}, labels = {}}) {
    if (!root || !adapter || !presentation) throw new Error('Review workflow requires a root, adapter, and presentation renderer.');
    const dialog = one(root, 'dialog'), status = one(root, 'status'), candidates = one(root, 'candidates');
    const executeButton = one(root, 'execute'), approveButton = one(root, 'approve'), findButton = one(root, 'find');
    const duplicateAction = one(root, 'duplicate-action'), mbidModal = one(root, 'mbid-modal'), trackModal = one(root, 'track-modal');
    let album = null, selected = null, returnFocus = null, nestedReturnFocus = null, busyOwner = null, openingState = '', succeeded = false, queue = [], transition = 0;
    const selectedValue = value => value === 'as-is' ? null : value;
    const snapshot = () => JSON.stringify({selected, duplicate: duplicateAction.value});
    const setStatus = (message, error = false) => { status.textContent = message || ''; status.classList.toggle('error-text', error); };
    function setBusy(value, message = '', owner = null) {
      if (busyOwner && busyOwner !== owner) busyOwner.setAttribute('aria-busy', 'false');
      busyOwner = value ? owner : null;
      root.dataset.busy = String(value);
      root.querySelectorAll('button,select,input').forEach(control => { if (!control.matches('[data-review-close]')) control.disabled = value; });
      if (owner) { owner.setAttribute('aria-busy', String(value)); const spinner = owner.querySelector('.button-spinner'); if (spinner) spinner.hidden = !value; }
      if (!value && album) syncActions();
      if (message) setStatus(message);
    }
    function syncActions() {
      approveButton.hidden = adapter.canApprove === false; approveButton.disabled = adapter.canApprove === false;
      findButton.hidden = !adapter.match; findButton.disabled = !adapter.match;
      executeButton.disabled = !album || succeeded || (adapter.requiresApproval && album.decision !== 'approved');
      one(root, 'execute').querySelector('.button-label').textContent = labels.execute || 'Import';
    }
    function render() {
      if (!album) return;
      one(root, 'title').textContent = `${album.artist || album.current_metadata?.artist || 'Unknown artist'} — ${album.album || album.current_metadata?.album || 'Unknown album'}`;
      const current = album.current_metadata || {artist: album.artist, album: album.album, track_count: album.tracks?.length};
      const art = one(root, 'current-art'); art.replaceChildren();
      if (current.artwork_url) { const image = document.createElement('img'); image.src = current.artwork_url; image.alt = 'Current artwork'; art.append(image); } else art.textContent = 'No art';
      one(root, 'current-meta').replaceChildren(field('Artist', current.artist), field('Album', current.album), field('Genre', current.genre), field('Year', current.year), field('Tracks', current.track_count || album.tracks?.length));
      const asIsId = adapter.asIs ? 'as-is' : null;
      presentation.renderCandidates({album, container: candidates, selectedId: selected, asIsId, openTrackComparison, onSelect: id => { selected = id; render(); }});
      const chosen = (album.candidates || []).find(item => item.id === selected);
      const duplicate = chosen?.duplicate_preflight || album.duplicate_preflight || {status: 'clear', warnings: []};
      const warning = one(root, 'duplicate'); warning.hidden = duplicate.status === 'clear';
      one(root, 'duplicate-message').textContent = duplicate.message || '';
      one(root, 'duplicate-files').replaceChildren(...(duplicate.warnings || []).map(item => { const li = document.createElement('li'); li.textContent = `${item.incoming_path || ''}${item.managed_paths?.length ? ` → ${item.managed_paths.join(', ')}` : ''}`; return li; }));
      duplicateAction.closest('label').hidden = duplicate.status !== 'possible';
      duplicateAction.value = album.duplicate_action || '';
      setStatus(album.execution_error || album.candidate_error || labels.ready || 'Choose metadata, then continue.');
      syncActions();
    }
    async function load(value) { album = typeof value === 'object' ? value : await adapter.load(value); selected = album.selection_mode === 'as-is' ? (adapter.asIs ? 'as-is' : null) : (album.selected_candidate_id ?? album.proposed_match?.id ?? album.candidates?.[0]?.id ?? (adapter.asIs ? 'as-is' : null)); render(); openingState = snapshot(); }
    async function open(value, trigger) {
      transition += 1; returnFocus = trigger || document.activeElement; succeeded = false; root.hidden = false; document.body.classList.add('modal-open'); setStatus('Loading reviewed album…'); dialog.focus();
      try { await load(value); } catch (error) { setStatus(error.message || 'Unable to load this album.', true); }
    }
    async function openSequence(values, trigger) { queue = [...values]; if (!queue.length) return false; await open(queue.shift(), trigger); return true; }
    function closeNested(modal, restore = true) { if (modal.hidden || root.dataset.busy === 'true') return; modal.hidden = true; dialog.inert = false; dialog.removeAttribute('aria-hidden'); if (restore) nestedReturnFocus?.focus(); nestedReturnFocus = null; }
    function close({clearQueue = true, restore = true, force = false} = {}) {
      if (!force && root.dataset.busy === 'true') return false;
      if (!force && !succeeded && album && snapshot() !== openingState && !window.confirm('Discard changes to this album review?')) return false;
      closeNested(trackModal, false); closeNested(mbidModal, false); root.hidden = true; document.body.classList.remove('modal-open'); transition += 1;
      if (!clearQueue && queue.length) { const next = queue.shift(); setTimeout(() => open(next, returnFocus), 0); return true; }
      if (clearQueue) queue = []; const target = returnFocus; album = null; selected = null; if (restore) { const focusTarget = target?.isConnected && !target.disabled ? target : adapter.focusFallback?.(); focusTarget?.focus(); } return true;
    }
    function advance() { if (queue.length) close({clearQueue: false, restore: false, force: true}); else close({clearQueue: false, force: true}); }
    function openNested(modal, trigger) { nestedReturnFocus = trigger; modal.hidden = false; dialog.inert = true; dialog.setAttribute('aria-hidden', 'true'); one(modal, modal === trackModal ? 'track-dialog' : 'mbid-input')?.focus(); }
    function openTrackComparison(tracks, candidateName, button) { one(root, 'track-title').textContent = `Track changes — ${candidateName}`; presentation.renderTrackRows(one(root, 'track-rows'), tracks); openNested(trackModal, button); }
    findButton.addEventListener('click', async event => { setBusy(true, 'Finding bounded MusicBrainz candidates…', event.currentTarget); try { album = await adapter.match(album); selected = album.selected_candidate_id ?? album.candidates?.[0]?.id ?? null; render(); } catch (error) { setStatus(error.message, true); } finally { setBusy(false, '', event.currentTarget); } });
    approveButton.addEventListener('click', async event => { setBusy(true, 'Saving explicit approval…', event.currentTarget); try { album = await adapter.save(album, {decision: 'approved', candidate_id: selectedValue(selected), duplicate_action: duplicateAction.value || undefined}); render(); openingState = snapshot(); setStatus(selected === 'as-is' || selected === null ? 'Approved As Is.' : 'Candidate approved.'); } catch (error) { setStatus(error.message, true); } finally { setBusy(false, '', event.currentTarget); } });
    one(root, 'skip').addEventListener('click', async event => { setBusy(true, 'Saving skipped status…', event.currentTarget); try { await adapter.save(album, {decision: 'skipped'}); await onSuccess({kind: 'skip', album}); advance(); } catch (error) { setStatus(error.message, true); } finally { setBusy(false, '', event.currentTarget); } });
    executeButton.addEventListener('click', async event => {
      const chosen = (album.candidates || []).find(item => item.id === selected), duplicate = chosen?.duplicate_preflight || album.duplicate_preflight || {status: 'clear'};
      if (duplicate.status === 'ambiguous') return setStatus('Import is blocked because managed recording identity is ambiguous.', true);
      if (duplicate.status === 'possible' && !duplicateAction.value) return setStatus('Choose a duplicate decision before importing this album.', true);
      setBusy(true, labels.executing || 'Validating files and importing…', event.currentTarget);
      let failed = false;
      try { const result = await adapter.execute(album, {candidate_id: selectedValue(selected), duplicate_action: duplicateAction.value || undefined, status: setStatus}); succeeded = true; await onSuccess({kind: 'execute', album, result}); advance(); } catch (error) { failed = true; setStatus(adapter.errorMessage ? adapter.errorMessage(error) : error.message, true); } finally { setBusy(false, '', event.currentTarget); if (failed) one(root, 'execute').querySelector('.button-label').textContent = labels.retry || 'Retry import'; }
    });
    one(root, 'open-mbid').addEventListener('click', event => { one(root, 'mbid-status').textContent = ''; one(root, 'mbid-input').value = ''; openNested(mbidModal, event.currentTarget); });
    one(root, 'mbid-form').addEventListener('submit', async event => { event.preventDefault(); const input = one(root, 'mbid-input'), value = input.value.trim(), submit = one(root, 'mbid-submit'); if (!UUID.test(value)) { one(root, 'mbid-status').textContent = 'Enter a canonical MusicBrainz release UUID.'; input.focus(); return; } setBusy(true, 'Searching MusicBrainz for this release…', submit); try { album = await adapter.exact(album, value); selected = album.selected_candidate_id ?? album.candidates?.[0]?.id ?? null; render(); openingState = snapshot(); setBusy(false, '', submit); closeNested(mbidModal); setStatus('MusicBrainz release added and selected. No files were changed.'); } catch (error) { one(root, 'mbid-status').textContent = error.message; } finally { setBusy(false, '', submit); } });
    root.querySelectorAll('[data-review-track-close]').forEach(button => button.addEventListener('click', () => closeNested(trackModal)));
    root.querySelectorAll('[data-review-mbid-close]').forEach(button => button.addEventListener('click', () => closeNested(mbidModal)));
    root.querySelectorAll('[data-review-close]').forEach(button => button.addEventListener('click', () => close()));
    one(root, 'cancel').addEventListener('click', () => close()); duplicateAction.addEventListener('change', syncActions);
    root.addEventListener('mousedown', event => { if (event.target === root) close(); else if (event.target === trackModal) closeNested(trackModal); else if (event.target === mbidModal) closeNested(mbidModal); });
    root.addEventListener('keydown', event => { const active = !trackModal.hidden ? trackModal : !mbidModal.hidden ? mbidModal : root; if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); active === root ? close() : closeNested(active); return; } if (event.key !== 'Tab') return; const nodes = focusable(active); if (!nodes.length) return; const first = nodes[0], last = nodes.at(-1); if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } });
    return Object.freeze({open, openSequence, close, get album() { return album; }});
  }
  window.CratekeepReviewWorkflow = Object.freeze({create});
})();
