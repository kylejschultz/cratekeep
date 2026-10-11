(() => {
  "use strict";
  const hook = (root, name) => root.querySelector(`[data-folder-${name}]`);
  const make = (tag, text = '', className = '') => Object.assign(document.createElement(tag), {textContent: text, className});
  function selectionLabel(count, albumCount, singular = "folder") { return `${count} ${count === 1 ? singular : `${singular}s`} selected · ${albumCount} albums available`; }
  function replaceRows(host, rows) { const fragment = document.createDocumentFragment(); rows.forEach(row => fragment.append(row)); host.replaceChildren(fragment); }
  function setPhase(states, scopes, phase) { scopes.forEach(scope => phase === null ? states.delete(scope) : states.set(scope, phase)); }
  function create({root, adapter, review, storageKey = '', noun = 'folder', selectPage = false, autoLoad = true}) {
    if (!root || !adapter || !review) throw new Error('Folder workflow requires a root, adapter, and review controller.');
    const list = hook(root, 'list'), status = hook(root, 'status'), selectAll = hook(root, 'select-all');
    const selection = hook(root, 'selection'), scanButton = hook(root, 'scan'), importButton = hook(root, 'import'), exceptionButton = hook(root, 'exceptions');
    const confirmModal = hook(root, 'confirm'), confirmSummary = hook(root, 'confirm-summary'), confirmApply = hook(root, 'confirm-apply');
    const selected = new Set(), phases = new Map();
    let rows = [], summary = {available_album_count: 0, ready_album_count: 0, exception_count: 0}, page = 1, pages = 1, total = 0;
    let listRequest = null, listSequence = 0, summarySequence = 0, scanSequence = 0, destroyed = false, searchTimer = null, returnFocus = null;
    if (storageKey) { try { JSON.parse(sessionStorage.getItem(storageKey) || '[]').forEach(value => selected.add(value)); } catch (_) {} }
    const save = () => { if (storageKey) try { sessionStorage.setItem(storageKey, JSON.stringify([...selected])); } catch (_) {} };
    const setStatus = (message, error = false) => { status.textContent = message || ''; status.classList.toggle('error-text', error); };
    const busy = (button, value) => { if (!button) return; button.setAttribute('aria-busy', String(value)); button.disabled = value; };
    function updateControls() {
      const visible = rows.map(item => item.path), checked = visible.filter(path => selected.has(path)).length;
      selectAll.checked = visible.length > 0 && checked === visible.length; selectAll.indeterminate = checked > 0 && checked < visible.length;
      selection.textContent = selectionLabel(selected.size, summary.available_album_count || 0, noun);
      scanButton.disabled = !selected.size || scanButton.getAttribute('aria-busy') === 'true';
      importButton.textContent = `Import ready (${summary.ready_album_count || 0})`; importButton.disabled = !summary.ready_album_count;
      exceptionButton.textContent = `Review exceptions (${summary.exception_count || 0})`; exceptionButton.disabled = !summary.exception_count;
      const label = hook(root, 'page-label'); if (label) label.textContent = `Page ${page} of ${pages}`;
      ['first','previous'].forEach(name => { const node = hook(root, name); if (node) node.disabled = page <= 1; });
      ['next','last'].forEach(name => { const node = hook(root, name); if (node) node.disabled = page >= pages; });
    }
    async function openScope(item, button, body = null) {
      if (body && !body.hidden) { body.hidden = true; button.setAttribute('aria-expanded', 'false'); return; }
      if (body?.dataset.loaded === 'true') { body.hidden = false; button.setAttribute('aria-expanded', 'true'); return; }
      if (body) { body.hidden = false; button.setAttribute('aria-expanded', 'true'); body.textContent = 'Loading albums…'; }
      try {
        const data = await adapter.albums(item.path), albums = data.albums || [];
        if (!body) { if (albums.length) await review.openSequence(albums, button); else setStatus('Scan this scope before review.'); return; }
        const children = albums.map(album => {
          const row = make('article', '', 'review-album-row'), identity = make('span', `${album.artist} — ${album.album}`, 'review-folder-identity');
          const count = make('span', `${album.tracks?.length || 0} ${(album.tracks?.length || 0) === 1 ? 'track' : 'tracks'}`, 'review-track-count');
          const state = make('span', album.ready ? 'Ready' : (album.decision || album.candidate_status || 'Needs review'), 'status-pill');
          const open = make('button', '↗', 'review-album'); open.type = 'button'; open.setAttribute('aria-label', `Review ${album.album} by ${album.artist}`); open.addEventListener('click', () => review.open(album, open));
          row.append(identity, count, state, open); return row;
        });
        if (!children.length) children.push(make('p', 'No reviewable albums in this folder.'));
        replaceRows(body, children); body.dataset.loaded = 'true';
      } catch (error) { if (body) body.textContent = error.message; else setStatus(error.message, true); }
    }
    function render() {
      const nodes = rows.map(item => {
        const article = make('article', '', adapter.rowClass || 'scope-row'); article.dataset.scope = item.path;
        const summaryRow = make('div', '', adapter.summaryClass || 'review-folder-summary');
        const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = selected.has(item.path); checkbox.setAttribute('aria-label', `Select ${item.name}`);
        checkbox.addEventListener('change', () => { checkbox.checked ? selected.add(item.path) : selected.delete(item.path); save(); render(); refreshSummary(); });
        let opener;
        if (adapter.expandRows) { opener = make('button', '', 'folder-expand'); opener.type = 'button'; opener.setAttribute('aria-expanded', 'false'); opener.setAttribute('aria-label', `Expand ${item.name}`); }
        const identity = make('div', '', adapter.identityClass || 'scope-identity'); identity.append(make('strong', item.name || item.path), make('span', item.path === '.' ? 'Root-level loose audio' : `${item.track_count || 0} tracks · ${item.album_count || 0} albums`, 'metric'));
        const current = phases.get(item.path), phase = typeof current === 'object' ? current.label : current;
        const state = make('span', phase || item.status || 'Not scanned', adapter.statusClass || 'status-chip'); state.dataset.status = typeof current === 'object' ? current.status : (current || item.status || 'not-scanned'); state.setAttribute('aria-live', 'polite'); if (item.error || current?.title) state.title = current?.title || item.error;
        const body = adapter.expandRows ? make('div', '', 'review-folder-contents') : null;
        if (body) { body.hidden = true; opener.addEventListener('click', () => openScope(item, opener, body)); }
        const reviewButton = adapter.expandRows ? null : make('button', 'Review', 'button-quiet');
        if (reviewButton) { reviewButton.type = 'button'; reviewButton.disabled = !item.album_count; reviewButton.addEventListener('click', () => openScope(item, reviewButton)); }
        summaryRow.append(checkbox); if (opener) summaryRow.append(opener); summaryRow.append(identity, state); if (reviewButton) summaryRow.append(reviewButton); article.append(summaryRow); if (body) article.append(body); return article;
      });
      replaceRows(list, nodes); if (!nodes.length) list.append(make('p', adapter.emptyMessage || `No ${noun}s found.`, 'empty')); updateControls();
    }
    async function refreshSummary() {
      const sequence = ++summarySequence, scopes = [...selected];
      if (!scopes.length) { summary = {available_album_count:0, ready_album_count:0, exception_count:0}; updateControls(); return summary; }
      try { const result = await adapter.preview(scopes); if (sequence !== summarySequence || destroyed) return summary; summary = result; updateControls(); return result; }
      catch (error) { if (sequence === summarySequence) setStatus(error.message, true); throw error; }
    }
    async function load(targetPage = page, {announce = true, preserveSelection = true} = {}) {
      const sequence = ++listSequence; listRequest?.abort(); listRequest = new AbortController(); if (announce) setStatus(`Loading ${noun}s…`);
      try {
        const result = await adapter.list({page: targetPage, query: hook(root, 'search')?.value.trim() || '', status: hook(root, 'filter')?.value || 'all', signal: listRequest.signal});
        if (sequence !== listSequence || destroyed) return; rows = result.items; page = result.page || 1; pages = result.page_count || 1; total = result.total ?? rows.length;
        if (!preserveSelection) selected.clear();
        if (!selectPage && result.complete) { const valid = new Set(rows.map(item => item.path)); [...selected].forEach(path => { if (!valid.has(path)) selected.delete(path); }); }
        render(); if (announce) setStatus(`${total} ${noun}${total === 1 ? '' : 's'} loaded without album details.`); await refreshSummary();
      } catch (error) { if (error.name !== 'AbortError' && sequence === listSequence) setStatus(error.message || `Unable to load ${noun}s.`, true); }
      finally { if (sequence === listSequence) listRequest = null; }
    }
    async function scan() {
      if (!selected.size || scanButton.getAttribute('aria-busy') === 'true') return; const sequence = ++scanSequence, scopes = [...selected]; setPhase(phases, scopes, 'Queued'); render(); busy(scanButton, true);
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); if (sequence !== scanSequence || destroyed) return; setPhase(phases, scopes, 'Scanning'); render();
      try { const result = await adapter.scan(scopes); setPhase(phases, scopes, null); for (const error of result.errors || []) if (error.folder || error.scope) phases.set(error.folder || error.scope, {status:'error', label:'Error', title:error.error}); setStatus(adapter.scanMessage ? adapter.scanMessage(result) : `Scanned ${scopes.length} selected ${noun}s.`); await load(page, {announce:false}); }
      catch (error) { setPhase(phases, scopes, {status:'error', label:'Error', title:error.message}); render(); setStatus(error.message, true); }
      finally { busy(scanButton, false); updateControls(); }
    }
    function closeConfirm() { if (!confirmModal || confirmApply.getAttribute('aria-busy') === 'true') return; confirmModal.hidden = true; document.body.classList.remove('modal-open'); returnFocus?.focus(); returnFocus = null; }
    async function confirmImport() { try { summary = await refreshSummary(); confirmSummary.textContent = adapter.confirmMessage ? adapter.confirmMessage(summary) : `${summary.ready_album_count} ready albums · ${summary.track_count || 0} tracks · ${summary.exception_count} exceptions retained.`; returnFocus = importButton; confirmModal.hidden = false; document.body.classList.add('modal-open'); (hook(root, 'confirm-dialog') || confirmModal.querySelector('[role="dialog"]')).focus(); } catch (_) {} }
    async function execute() { busy(confirmApply, true); try { const result = await adapter.execute([...selected]); busy(confirmApply, false); closeConfirm(); setStatus(adapter.executeMessage ? adapter.executeMessage(result) : `${result.imported?.length || 0} albums imported; ${result.skipped?.length || 0} exceptions retained; ${result.failed?.length || 0} failed.`); await load(page, {announce:false}); await adapter.onSuccess?.(result); } catch (error) { setStatus(error.message, true); } finally { busy(confirmApply, false); } }
    async function exceptions() { busy(exceptionButton, true); try { const result = await adapter.exceptions([...selected]); await review.openSequence(result.albums || [], exceptionButton); } catch (error) { setStatus(error.message, true); } finally { busy(exceptionButton, false); updateControls(); } }
    selectAll.addEventListener('change', () => { rows.forEach(item => selectAll.checked ? selected.add(item.path) : selected.delete(item.path)); save(); render(); refreshSummary(); });
    scanButton.addEventListener('click', scan); importButton.addEventListener('click', confirmImport); exceptionButton.addEventListener('click', exceptions); confirmApply?.addEventListener('click', execute);
    root.querySelectorAll('[data-folder-confirm-close]').forEach(button => button.addEventListener('click', closeConfirm)); confirmModal?.addEventListener('mousedown', event => { if (event.target === confirmModal) closeConfirm(); });
    [['first',()=>1],['previous',()=>page-1],['next',()=>page+1],['last',()=>pages]].forEach(([name,value]) => hook(root,name)?.addEventListener('click',()=>load(value())));
    hook(root, 'search')?.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => load(1), 250); }); hook(root, 'filter')?.addEventListener('change', () => load(1));
    if (autoLoad) load(1);
    return Object.freeze({load, refreshSummary, destroy() { destroyed = true; listRequest?.abort(); clearTimeout(searchTimer); }, get selected() { return new Set(selected); }});
  }
  window.CratekeepFolderWorkflow = Object.freeze({create, selectionLabel, replaceRows, setPhase});
})();
