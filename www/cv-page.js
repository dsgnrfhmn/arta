(function () {
  // The sets, the referent and its blocks come from the API (arta-api.js).
  // Nothing is kept here that the server does not confirm: every change is
  // sent first and the page shows the set the server sends back. On failure
  // the page stays as it was and a toast says why.
  const setsApi = ArtaApi.sets.cv;

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toastError = (message) => window.dsToast.show({ message, status: 'error' });

  let knownSetIds = new Set(); // every set the server has, archived ones too
  let sets = [];                         // the sidebar, in the server's order
  let active = null;                     // the open set, as the server last sent it
  // The referent pane always shows the Full CV (dsgnrfhmn, 2026-10-25: no chooser).
  let referent = { id: 'full', blocks: [] }; // the pane's source and its blocks
  let referentToken = 0;

  const el = {
    list: document.getElementById('cv-sidebar-list'),
    title: document.getElementById('cv-set-title'),
    refList: document.getElementById('cv-referent-list'),
    refAll: document.getElementById('cv-referent-all'),
    previewPages: document.getElementById('cv-preview-pages'),
    previewEmpty: document.getElementById('cv-preview-empty'),
    previewCount: document.getElementById('cv-preview-count'),
    undo: document.getElementById('cv-undo'),
    redo: document.getElementById('cv-redo'),
    loading: document.getElementById('cv-loading'),
    loadingTip: document.getElementById('cv-loading-tip'),
  };
  const entries = () => (active ? active.entries : []);

  // ---- Render ----
  function renderSidebar() {
    // The first row is the create button, as on Portfolio: part of the list, so it
    // scrolls with it and hides with it when the sidebar is collapsed. A set row
    // without the save button, bold because it is the primary action of the list.
    const create = `
      <div class="ds-sidebar-item" data-type="set" data-role="create">
        <button type="button" class="ds-text-button" data-priority="primary" data-status="active" data-underline="dotted" id="cv-create-set" data-popup-open="cv-create-set-popup"><span class="ds-text-button__text">Create new CV set</span></button>
        <button type="button" class="ds-sidebar-item__save" data-popup-open="cv-create-set-popup" aria-label="Create new CV set" tabindex="-1"><span class="ds-sidebar-item__save-icon" data-icon="add" aria-hidden="true"></span></button>
      </div>`;
    el.list.innerHTML = create + sets.map((s) => `
      <div class="ds-sidebar-item" data-type="set" data-set-id="${esc(s.id)}" data-active="${!!active && s.id === active.id}" data-context-menu="cv-set-menu">
        <button type="button" class="ds-text-button" data-priority="secondary" data-status="active" data-underline="dotted" data-select-set aria-current="${!!active && s.id === active.id}"><span class="ds-text-button__text">${esc(s.name)}</span></button>
        <button type="button" class="ds-sidebar-item__save" data-save-set aria-pressed="${s.saved}" aria-label="${s.saved ? 'Saved' : 'Save'}: ${esc(s.name)}"><span class="ds-sidebar-item__save-icon" data-icon="save" aria-hidden="true"></span></button>
      </div>`).join('');
    ArtaBoot.icons(el.list);
  }

  // Add All / Remove All in the pane head (dsgnrfhmn, 2026-10-25): Add All while any block of the
  // Full CV is missing from the set, Remove All once every one is in. Off for an archived set
  // and while the Full CV has not loaded.
  function renderAllButton(inSet) {
    const all = referent.blocks.length > 0 && referent.blocks.every((b) => inSet.has(b.id));
    el.refAll.dataset.all = all ? 'remove' : 'add';
    el.refAll.querySelector('.ds-text-button__text').textContent = all ? 'Remove All' : 'Add All';
    el.refAll.dataset.tooltip = all
      ? 'Remove every block from this CV set (Undo brings them back)'
      : 'Add every block of the Full CV to this CV set';
    const off = !referent.blocks.length || (!!active && active.archived);
    el.refAll.disabled = off;
    el.refAll.dataset.status = off ? 'disabled' : 'active';
  }

  // A block already in the set (same id) is tinted and shows Remove instead of Add. The tint is by
  // id, never by text: two blocks can read the same.
  function renderReferent() {
    const inSet = new Set(entries().map((b) => b.id));
    renderAllButton(inSet);
    el.refList.innerHTML = referent.blocks.map((b) => {
      const included = inSet.has(b.id);
      return `<li class="ds-block-row" data-type="source" data-actions="overlay" data-density="compact" data-clickable="true"${b.kind === 'heading' ? ' data-kind="heading"' : ''} data-block-id="${esc(b.id)}" data-included="${included}">
        <div class="ds-block-row__text">${esc(b.text)}</div>
        <div class="ds-block-row__actions">
          <button type="button" class="ds-text-button" data-priority="secondary" data-status="active" data-underline="dotted" data-size="small" data-action="add-block"${included ? ' tabindex="-1" aria-hidden="true"' : ''}><span class="ds-text-button__text">Add</span></button>
          <button type="button" class="ds-text-button" data-priority="secondary" data-status="active" data-underline="dotted" data-size="small" data-action="remove-block"${included ? '' : ' tabindex="-1" aria-hidden="true"'}><span class="ds-text-button__text">Remove</span></button>
        </div>
      </li>`;
    }).join('');
  }

  // The preview draws the set's real blocks on landscape sheets (DsCvSheet, staged
  // in dis-system/local-components/cv-sheet). Sheets are a greedy fill by measured
  // height, a preview layout and not the phase-two pagination engine, so the
  // "N pages" caption is the number of sheets drawn here, not an export page count.
  // The preview is also where the set is edited: a block shows Edit and Delete on hover,
  // is dragged to a new place, and the answer of the server is what gets drawn. Only
  // the paths that change the blocks ask for motion (animateNext): an added block fades
  // in, a removed or deleted one fades out and the blocks below slide up into its
  // place; a set switch, undo or any other re-render stays still. Reduced motion is
  // honored inside DsCvSheet.
  let animateNext = false;
  let revealNext = null;   // { id } of an added block, { index } of the place a removed one left
  const editor = {
    async onSave(id, text) {
      animateNext = true;
      const done = await apply(() => setsApi.editBlock(active.id, id, text), 'Could not save the block');
      animateNext = false; revealNext = null;
      return done;
    },
    async onDelete(id) {
      animateNext = true;
      revealNext = { index: entries().findIndex((b) => b.id === id) };
      const done = await apply(() => setsApi.deleteBlock(active.id, id), 'Could not delete the block');
      animateNext = false; revealNext = null;
      return done;
    },
    onReorder(ids) {
      animateNext = true;
      return apply(() => setsApi.order(active.id, ids), 'Could not reorder the blocks').then(() => { animateNext = false; });
    },
    onEmpty() {
      toastError('A block cannot be empty. Delete it instead.');
    },
  };

  function renderPreview() {
    const blocks = entries();
    const editable = !!active && !active.archived;
    const n = blocks.length
      ? DsCvSheet.render(el.previewPages, blocks, { title: 'CURRICULUM VITAE', editable: editable && editor, actions: false, clickToEdit: true, animate: animateNext, reveal: revealNext })
      : 0;
    animateNext = false;
    revealNext = null;
    el.previewCount.textContent = `${n} ${n === 1 ? 'page' : 'pages'}`;
    el.previewEmpty.hidden = n > 0;
    el.previewPages.hidden = n === 0;
  }

  // ---- How the sheets are shown (dsgnrfhmn, 2026-10-24) ----
  // dis-system's zoom bar, the same as Portfolio's: fit, edge to edge of the pane (the
  // default and the largest view), or a smaller zoom of the sheet's own 842 px on the
  // page colour. Kept for this page view only.
  DsZoomButtons.bar(document.getElementById('cv-zoom-bar'), {
    keep: { scroller: el.previewPages, items: '.ds-cv-sheet' },   // a zoom keeps the place
    fitted: () => DsZoomButtons.fitOnPaper(el.previewPages, 842),
    apply: (zoom) => DsCvSheet.setZoom(el.previewPages, zoom),
  });

  // Font files load after first paint; the sheets are measured again once they have.
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(renderPreview);

  function renderContent() {
    el.title.textContent = active ? active.name : '';
    ArtaRecent.touch('cv', active);
    // Click the name to rename it; an archived set is read-only.
    const renamable = !!active && !active.archived;
    el.title.toggleAttribute('data-editable', renamable);
    if (renamable) el.title.tabIndex = 0;
    else el.title.removeAttribute('tabindex');
    // Undo and Redo follow the set's stacks; an archived set is read-only.
    const editable = !!active && !active.archived;
    [[el.undo, active && active.can_undo], [el.redo, active && active.can_redo]].forEach(([button, can]) => {
      button.disabled = !(editable && can);
      button.dataset.status = editable && can ? 'active' : 'disabled';
    });
    // Nothing left to undo: say so on hover (arta's own words, spec 6.x).
    if (active && active.can_undo) delete el.undo.dataset.tooltip;
    else el.undo.dataset.tooltip = 'There is nothing to undo, darlin.';
    if (active && active.can_redo) delete el.redo.dataset.tooltip;
    else el.redo.dataset.tooltip = 'There is nothing to redo darlin.';
    renderReferent();
    renderPreview();
    aiEdit.render();
  }
  function renderAll() {
    renderSidebar();
    renderContent();
    ArtaPdf.refreshButtons();   // the open set may be one being exported
  }

  // ---- Talking to the server ----
  // Archived sets are not shown (they stay readable through ?set=<id>).
  async function refreshSidebar() {
    const all = (await setsApi.list()).items;
    knownSetIds = new Set(all.map((s) => s.id));
    sets = all.filter((s) => !s.archived);
    renderSidebar();
  }

  let loadingTimer = null;
  function setLoading(on) {
    el.loading.hidden = !on;
    if (!on) return;
    // Figma: the tooltip sits at the viewport's vertical middle.
    el.loadingTip.style.top = `${window.innerHeight / 2 - el.loading.getBoundingClientRect().top}px`;
  }

  // Loads the referent's blocks. An answer that arrives after another choice was
  // made is dropped (token). `veil` shows Figma's Loading state for at least a moment.
  async function chooseReferent(id, veil) {
    const token = ++referentToken;
    if (veil) setLoading(true);
    const started = Date.now();
    let blocks;
    try {
      blocks = (await ArtaApi.cv.blocks(id)).items;
    } catch (error) {
      if (token === referentToken) {
        setLoading(false);
        toastError(`Could not load the referent: ${error.message}`);
      }
      return false;
    }
    if (veil) await new Promise((r) => { clearTimeout(loadingTimer); loadingTimer = setTimeout(r, Math.max(0, 400 - (Date.now() - started))); });
    if (token !== referentToken) return false;
    referent = { id, blocks };
    setLoading(false);
    renderReferent();
    return true;
  }

  async function openSet(id) {
    active = await setsApi.get(id);
    renderAll();
  }

  // Runs a call that answers with the whole set and shows that answer.
  async function apply(call, failure) {
    try {
      active = await call();
    } catch (error) {
      toastError(`${failure}: ${error.message}`);
      return false;
    }
    renderContent();
    return true;
  }

  // A page with no set yet makes one when the first block is added.
  async function ensureActive() {
    if (active) return;
    active = await setsApi.create();
    await refreshSidebar();
    renderContent();
  }

  // ---- Sidebar: select set, toggle save ----
  el.list.addEventListener('click', async (event) => {
    const save = event.target.closest('[data-save-set]');
    const select = event.target.closest('[data-select-set]');
    if (!save && !select) return;
    const id = (save || select).closest('.ds-sidebar-item').dataset.setId;
    try {
      if (save) {
        await setsApi.setSaved(id, save.getAttribute('aria-pressed') !== 'true');
        await refreshSidebar();
      } else {
        await openSet(id);
      }
    } catch (error) {
      toastError(`${save ? 'Could not save the set' : 'Could not open the set'}: ${error.message}`);
    }
  });

  // ---- Sidebar: drag a set to place it (the order is the user's; saving moves nothing) ----
  // Requests are serialized and the newest wins: a late answer to an older drag never
  // repaints over a newer one. A failure leaves `sets` at the last confirmed order.
  const submitSetOrder = ArtaBoot.lastWins((ids) => setsApi.orderSidebar(ids));
  ArtaBoot.sortable(el.list, {
    row: '.ds-sidebar-item[data-set-id]',
    id: (row) => row.dataset.setId,
    noDrag: '.ds-sidebar-item__save, input, textarea',
    zone: el.list.closest('.ds-sidebar-list'),
    onReorder: async (ids) => {
      const result = await submitSetOrder(ids);
      if (result.stale) return;
      if (result.ok) sets = result.value.items.filter((s) => !s.archived);
      else toastError(`Could not reorder the sets: ${result.error.message}`);
      renderSidebar();
    },
  });

  // ---- A click anywhere on a referent row does what its visible action does (dsgnrfhmn,
  // 2026-10-24): Add when the block is not in the set, Remove when it is. A click that
  // ends a text selection selects; the buttons keep their own click and keyboard path.
  el.refList.addEventListener('click', (event) => {
    if (event.target.closest('button')) return;
    const row = event.target.closest('.ds-block-row[data-clickable="true"]');
    if (!row || String(window.getSelection() || '')) return;
    const action = row.dataset.included === 'true' ? 'remove-block' : 'add-block';
    row.querySelector(`[data-action="${action}"]`)?.click();
  });

  // ---- Add block (SS6.7.1): appended to the end of the active set ----
  el.refList.addEventListener('click', async (event) => {
    const add = event.target.closest('[data-action="add-block"]');
    if (!add) return;
    const row = add.closest('.ds-block-row');
    if (row.dataset.included === 'true') return;
    try {
      await ensureActive();
    } catch (error) {
      toastError(`Could not create a CV set: ${error.message}`);
      return;
    }
    animateNext = true;
    revealNext = { id: row.dataset.blockId };
    if (!(await apply(() => setsApi.addBlock(active.id, referent.id, row.dataset.blockId), 'Could not add the block'))) { animateNext = false; revealNext = null; }
  });

  // ---- Add All / Remove All: the whole Full CV in, or the set emptied; one undo step each ----
  el.refAll.addEventListener('click', async () => {
    if (el.refAll.disabled) return;
    if (el.refAll.dataset.all === 'remove') {
      if (active) await apply(() => setsApi.removeAll(active.id), 'Could not remove the blocks');
      return;
    }
    try {
      await ensureActive();
    } catch (error) {
      toastError(`Could not create a CV set: ${error.message}`);
      return;
    }
    await apply(() => setsApi.addAll(active.id, referent.id), 'Could not add the blocks');
  });

  // ---- Remove block: the same block (same id) leaves the active set ----
  el.refList.addEventListener('click', async (event) => {
    const remove = event.target.closest('[data-action="remove-block"]');
    if (!remove || !active) return;
    const row = remove.closest('.ds-block-row');
    if (row.dataset.included !== 'true') return;
    await DsCvSheet.reveal(el.previewPages, { id: row.dataset.blockId });
    await DsCvSheet.fadeOut(el.previewPages, row.dataset.blockId);
    animateNext = true;
    revealNext = { index: entries().findIndex((b) => b.id === row.dataset.blockId) };
    if (!(await apply(() => setsApi.deleteBlock(active.id, row.dataset.blockId), 'Could not remove the block'))) {
      animateNext = false; revealNext = null;
      DsCvSheet.restore(el.previewPages, row.dataset.blockId);
    }
  });

  // ---- Header actions ----
  // One step back or forward through the set's history (kept on the server).
  async function stepHistory(call, verb) {
    if (!active) return;
    try {
      active = await call(active.id);
    } catch (error) {
      if (error.status === 409) window.dsToast.show({ message: `Nothing to ${verb} here yet.` });
      else toastError(`Could not ${verb}: ${error.message}`);
      return;
    }
    renderContent();
  }
  el.undo.addEventListener('click', () => stepHistory(setsApi.undo, 'undo'));
  el.redo.addEventListener('click', () => stepHistory(setsApi.redo, 'redo'));

  // ---- Edit with AI (arta-ai-edit.js, shared with Portfolio) ----
  let confirmCallback = null;
  document.getElementById('cv-confirm-ok').addEventListener('click', () => {
    if (confirmCallback) confirmCallback();
    confirmCallback = null;
  });
  const aiEdit = ArtaAiEdit.attach({
    ids: { toggle: 'cv-edit-ai', bar: 'cv-ai-bar', input: 'cv-ai-input', run: 'cv-ai-run' },
    api: { estimate: setsApi.aiEditEstimate, run: setsApi.aiEdit, undo: setsApi.undo },
    get: () => active,
    set: (set) => { active = set; },
    render: renderContent,
    signature: (set) => JSON.stringify(set.entries.map((b) => [b.id, b.kind, b.text])),
    confirm: (title, body, okLabel, onOk) => {
      document.getElementById('cv-confirm-title').textContent = title;
      document.getElementById('cv-confirm-body').textContent = body;
      document.getElementById('cv-confirm-ok').textContent = okLabel;
      confirmCallback = onOk;
      document.getElementById('cv-confirm-trigger').click();
    },
    stepUndo: () => stepHistory(setsApi.undo, 'undo'),
    toastError,
  });
  const pdfButton = document.getElementById('cv-download-pdf');
  ArtaPdf.bindButton(pdfButton, 'cv', () => active);
  pdfButton.addEventListener('click', () => {
    if (active) ArtaPdf.download('cv', active);
  });

  // ---- Create a new CV set ----
  // The trigger is re-rendered with the list, so it is found by delegation.
  el.list.addEventListener('click', (event) => {
    if (event.target.closest('#cv-create-set, .ds-sidebar-item[data-role="create"] .ds-sidebar-item__save')) document.getElementById('cv-create-set-name').value = '';
  });
  document.getElementById('cv-create-set-confirm').addEventListener('click', async () => {
    const name = document.getElementById('cv-create-set-name').value.trim();
    try {
      active = await setsApi.create(name || undefined); // no name: the server's "CV Set N"
      await refreshSidebar();
    } catch (error) {
      toastError(`Could not create the CV set: ${error.message}`);
      return;
    }
    renderContent();
    window.dsToast.show({ message: `${active.name} created.` });
  });

  // ---- Save CV set (SS6.7.5) ----
  document.getElementById('cv-save-set').addEventListener('click', () => {
    document.getElementById('cv-save-set-name').value = active ? active.name : '';
  });
  document.getElementById('cv-save-set-confirm').addEventListener('click', async () => {
    const name = document.getElementById('cv-save-set-name').value.trim();
    if (!name) return;
    try {
      await ensureActive();
      active = await setsApi.save(active.id, name);
      await refreshSidebar();
    } catch (error) {
      toastError(`Could not save the set: ${error.message}`);
      return;
    }
    renderContent();
    window.dsToast.show({ message: `${active.name} saved.` });
  });

  // ---- Right click on a set: Rename, Duplicate, Download PDF, Delete ----
  // The menu says which set (the row it opened on); what happens is decided here.
  let menuSet = null;   // the set the open popup is about
  el.list.addEventListener('ds-context-menu-select', async (event) => {
    const { value, host } = event.detail;
    const set = sets.find((s) => s.id === host.dataset.setId);
    if (!set) return;
    menuSet = set;
    if (value === 'rename') {
      document.getElementById('cv-rename-set-name').value = set.name;
      document.getElementById('cv-rename-trigger').click();
    } else if (value === 'delete') {
      document.getElementById('cv-delete-set-body').textContent =
        `Delete "${set.name}"? The set and its contents are removed permanently. This cannot be undone.`;
      document.getElementById('cv-delete-trigger').click();
    } else if (value === 'download-pdf') {
      ArtaPdf.download('cv', set);
    } else if (value === 'duplicate') {
      try {
        let copy = await setsApi.duplicate(set.id, `${set.name} copy`);
        // A duplicate is a working copy: never saved, even when the set it came from is.
        // (The server saves a duplicate, which is what Portfolio's Save as New Set wants.)
        if (copy.saved) copy = await setsApi.setSaved(copy.id, false);
        // The server puts a new set first; here the copy goes under the one it was made
        // from. The sidebar is drawn once, after it is placed, so the copy never shows at
        // the top first.
        const shown = (await setsApi.list()).items.filter((s) => !s.archived).map((s) => s.id).filter((id) => id !== copy.id);
        shown.splice(shown.indexOf(set.id) + 1, 0, copy.id);
        await setsApi.orderSidebar(shown);
        await refreshSidebar();
        window.dsToast.show({ message: `${copy.name} created.` });
      } catch (error) {
        toastError(`Could not duplicate the CV set: ${error.message}`);
      }
    }
  });
  document.getElementById('cv-rename-set-confirm').addEventListener('click', async () => {
    const name = document.getElementById('cv-rename-set-name').value.trim();
    if (!menuSet || !name || name === menuSet.name) return;
    const { id } = menuSet;
    try {
      const renamed = await setsApi.rename(id, name);
      if (active && active.id === id) active = renamed;
      await refreshSidebar();
    } catch (error) {
      toastError(`Could not rename the CV set: ${error.message}`);
      return;
    }
    renderAll();
  });
  // ---- Rename in place ----
  // The name becomes an input with the title's own type, so nothing moves. Enter or
  // leaving the field saves, Escape keeps the old name. A rename is not a save.
  function startRename() {
    if (!active || active.archived || el.title.hidden) return;
    const input = document.createElement('input');
    input.className = 'ds-page-head__title-input';
    input.type = 'text';
    input.value = active.name;
    input.maxLength = 120;
    input.autocomplete = 'off';
    input.setAttribute('aria-label', 'CV set name');
    el.title.hidden = true;
    el.title.after(input);
    input.focus();
    input.select();
    let finished = false;
    const finish = async (commit) => {
      if (finished) return;
      finished = true;
      const name = input.value.trim();
      input.remove();
      el.title.hidden = false;
      const before = active.name;
      if (!commit || !name || name === before) return;
      const setId = active.id;
      el.title.textContent = name; // at once: no flash of the old name while the server answers
      try {
        active = await setsApi.rename(setId, name);
      } catch (error) {
        el.title.textContent = before;
        toastError(`Could not rename the CV set: ${error.message}`);
        return;
      }
      renderContent();
      await refreshSidebar();
    };
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        finish(true);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        finish(false);
      }
    });
    input.addEventListener('blur', () => finish(true));
  }
  el.title.addEventListener('click', startRename);
  el.title.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === 'F2') {
      event.preventDefault();
      startRename();
    }
  });

  document.getElementById('cv-delete-set-confirm').addEventListener('click', async () => {
    if (!menuSet) return;
    const { id, name } = menuSet;
    const wasOpen = !!active && active.id === id;
    try {
      await setsApi.remove(id);
      await refreshSidebar();
      if (wasOpen) active = sets[0] ? await setsApi.get(sets[0].id) : null;
    } catch (error) {
      toastError(`Could not delete the CV set: ${error.message}`);
      return;
    }
    renderAll();
    window.dsToast.show({ message: `${name} deleted.` });
  });

  // A link from a toast (?set=<id>, Prepare Application) opens that set; otherwise the
  // first in the sidebar. An id that is not there says so instead of showing another set.
  function setToOpen() {
    const wanted = new URLSearchParams(window.location.search).get('set');
    const found = wanted && knownSetIds.has(wanted);
    if (wanted && !found) toastError('That set was not found.');
    return found ? wanted : (sets[0] && sets[0].id);
  }

  // ---- First load: the sidebar, the first set in it, the Full CV in the left pane ----
  (async () => {
    try {
      await refreshSidebar();
      const first = setToOpen();
      if (first) await openSet(first);
      await chooseReferent('full', false);
    } catch (error) {
      ArtaBoot.showLoadError('cv-load-error', 'the CV sets', error);
    }
    renderAll();
    if (ArtaPrint.active) {
      // The PDF export: the sheets at their own size, then only they (arta-print.js).
      if (document.fonts) await document.fonts.ready;
      DsCvSheet.setZoom(el.previewPages, 1);
      ArtaPrint.finish({ container: el.previewPages, sheet: '.ds-cv-sheet', setId: active && active.id });
      return;
    }
    // The view is held (data-fade-hold) until the sheets' fonts are in, so it appears
    // once; from here re-renders stay still.
    await ArtaBoot.whenDrawn(el.previewPages);
    ArtaBoot.loaded();
  })();
})();
