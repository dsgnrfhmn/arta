/*
  Rewritten for the Figma-driven redesign (dsgnrfhmn, 2026-09-17): each section
  (For Review/Analysed/Archived/Filtered/All) has its own always-present
  table. The tab strip was removed on 2026-10-11, so there is no panel
  switching; the per-row action logic below is largely carried
  over from the pre-redesign version, adapted to the new markup
  (Full Description/Summary now use text-active-cell + a shared "view"
  popup instead of a Summary-only one, per the popup's own comment).
*/
(function () {
  const reviewTable = document.getElementById('foryou-review-table');
  const analysedTable = document.getElementById('foryou-analysed-table');
  const archivedTable = document.getElementById('foryou-archived-table');
  const filteredTable = document.getElementById('foryou-filtered-table');
  const allTable = document.getElementById('foryou-all-table');
  const descriptionTextarea = document.getElementById('foryou-description-textarea');
  const descriptionSaveBtn = document.getElementById('foryou-description-save');
  const descriptionTitle = document.getElementById('foryou-description-popup-title');
  let descriptionTargetRow = null;

  function rowName(row) {
    return row.querySelector('[data-type="text-active-cell"] .ds-table-cell__text').textContent.trim();
  }

  // Writes go through ArtaApi first; the DOM changes only once the server
  // has accepted them. On failure the row stays as it was (fail loud).
  // A text button's label lives in a span text-button/component.js wraps around its
  // text, and the underline is drawn on that span. Setting textContent on the
  // button throws the span away, and the button loses its underline. Set the
  // span's text when there is one.
  function setLabel(button, text) {
    const span = button.querySelector(':scope > .ds-text-button__text, :scope > .ds-text-button__label');
    (span || button).textContent = text;
  }

  function toastError(message) {
    window.dsToast.show({ message, status: 'error' });
  }
  const tableFor = { review: reviewTable, analysed: analysedTable, archived: archivedTable, filtered: filteredTable };
  const tableLabel = { review: 'For Review', analysed: 'Analysed' };
  function refreshSelectAll(...tables) {
    if (typeof updateSelectAllState === 'function') tables.forEach((table) => updateSelectAllState(table));
  }

  // ---- The All table (a second row for every record) ----
  // Every record has a row in its own table and a row in All. The page acts on
  // the home row only (a row in All never moves between tables), and once the
  // server has accepted the change, syncAll rebuilds the All row from what the
  // server now holds. So the two rows cannot disagree for longer than one request.
  const rowsOf = (id) => Array.from(document.querySelectorAll('.foryou-table > .ds-table-row[data-id]'))
    .filter((row) => row.dataset.id === id);
  const twinsOf = (row) => rowsOf(row.dataset.id).filter((other) => other !== row);

  // ---- Rows already acted on (dsgnrfhmn, 2026-10-24) ----
  // Opening a row's popup or link, Analyse, Archive, a status... marks the record as
  // visited for this browser session: its rows in every table take table-row's faint
  // data-visited grey, so the untouched items stand out. Selecting a row (checkbox)
  // and saving it (bookmark) do not count. Rows drawn later (a reshape, a reload,
  // a sync) get the mark back from the session's list.
  const VISITED_KEY = 'arta-foryou-visited';
  const visited = new Set((() => {
    try { return JSON.parse(sessionStorage.getItem(VISITED_KEY) || '[]'); } catch (err) { return []; }
  })());
  const markVisited = (row) => {
    if (visited.has(row.dataset.id) && row.dataset.visited !== 'true') row.dataset.visited = 'true';
  };
  function visit(id) {
    visited.add(id);
    try { sessionStorage.setItem(VISITED_KEY, JSON.stringify([...visited])); } catch (err) { /* kept for this page view */ }
    rowsOf(id).forEach(markVisited);
  }
  document.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const control = target && target.closest('a, button, [data-dropdown-trigger], .ds-dropdown-menu__item');
    const row = control && control.closest('.foryou-table > .ds-table-row[data-id]');
    if (!row || control.closest('[data-type="checkbox-cell"], [data-type="icon-cell"]')) return;
    visit(row.dataset.id);
  }, true);
  const tablesSeen = new MutationObserver((records) => records.forEach((record) => record.addedNodes.forEach((node) => {
    if (node instanceof Element && node.matches('.ds-table-row[data-id]')) markVisited(node);
  })));
  document.querySelectorAll('.foryou-table').forEach((table) => {
    tablesSeen.observe(table, { childList: true });
    table.querySelectorAll(':scope > .ds-table-row[data-id]').forEach(markVisited);
  });

  // The row an action from a button acts on: the button's own row, or for a
  // button in All, the same record's row in its own table.
  function homeRow(button) {
    const row = button.closest('.ds-table-row');
    if (row.parentElement !== allTable) return row;
    const home = twinsOf(row)[0];
    if (!home) toastError('This item has no row in its own tab. Reload the page.');
    return home || null;
  }

  function resort(table) {
    // The table shows its sort, so a row that changed under it would contradict it.
    const activeSort = table.querySelector('.ds-table-header-cell__sort-btn[data-sort-dir="asc"], .ds-table-header-cell__sort-btn[data-sort-dir="desc"]');
    // With no column sort, a table with a default order (All: by status) keeps it.
    const target = activeSort
      || (table.querySelector(':scope > .ds-table-row[data-default-rank]') && table.querySelector('.ds-table-header-cell__sort-btn'));
    if (target) {
      target.dispatchEvent(new CustomEvent('ds-table-header-cell-sort', {
        bubbles: true,
        detail: { direction: activeSort ? activeSort.dataset.sortDir : 'none' },
      }));
    }
  }

  async function syncAll(ids) {
    const wanted = new Set(ids);
    const rows = Array.from(allTable.querySelectorAll(':scope > .ds-table-row[data-id]'))
      .filter((row) => wanted.has(row.dataset.id));
    if (!rows.length) return;
    try {
      const { items } = await ArtaApi.opportunities.list({});
      const byId = new Map(items.map((item) => [item.id, item]));
      rows.forEach((row) => {
        ForYou.reshape(row, 'all', byId.get(row.dataset.id));
        row.dataset.selected = 'false'; // the record changed: its selection is done with
      });
      resort(allTable);
      refreshSelectAll(allTable);
      syncAllActions();
      allTable.dispatchEvent(new CustomEvent('arta-table-refresh')); // a row may now fall outside the Status filter
    } catch (error) {
      toastError(`Could not refresh the All tab: ${error.message}. Reload the page.`);
    }
  }

  // A row that just got its result fades green once (dis-system table-row,
  // data-flash). The attribute is removed when the animation ends, so the
  // next result can flash the same row again; under reduced motion there is
  // no animation, so it is removed right away.
  function flashRows(rows) {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    rows.forEach((row) => {
      row.removeAttribute('data-flash');
      void row.offsetWidth; // restart the animation if the row was already flashing
      row.dataset.flash = 'success';
      if (reduced) { row.removeAttribute('data-flash'); return; }
      row.addEventListener('animationend', () => row.removeAttribute('data-flash'), { once: true });
    });
  }

  // ---- Add description (SS4.6.1): the "Add Description" button is
  // only ever rendered on a row that has no description yet (Figma's
  // own 4 screens show no re-edit affordance once real text/view
  // replaces it, unlike the pre-redesign spec's "Add/Edit" popup
  // title) -- so this popup is always a fresh add, never a re-open.
  // Delegated: a row rebuilt after load (Move to "For Review", a saved
  // description cleared again) still gets its button handled.
  document.body.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-action="add-description"]');
    if (!btn) return;
    descriptionTargetRow = homeRow(btn);
    descriptionTitle.textContent = 'Add Description';
    descriptionTextarea.value = '';
  });

  descriptionSaveBtn.addEventListener('click', async () => {
    if (!descriptionTargetRow) return;
    const row = descriptionTargetRow;
    const text = descriptionTextarea.value.trim();
    if (!text) return;
    descriptionTargetRow = null;
    let record;
    try {
      record = await ArtaApi.opportunities.patch(row.dataset.id, { description: text });
    } catch (error) {
      toastError(`Could not save the description: ${error.message}`);
      return;
    }
    // Rebuilt from the saved record, so the cell shows what the server
    // stored (sanitized) and Analyze enables exactly when there is text.
    ForYou.reshape(row, 'review', record);
    syncAll([record.id]);
  });

  // ---- Shared "view" popup (Full Description / Summary, any table) ----
  // Each "view" button already carries data-popup-open="foryou-view-
  // popup" in markup (popup/component.js's own querySelectorAll pass
  // runs before this script and only wires listeners for attributes
  // present at that time -- adding the attribute here, after the fact,
  // would be too late for that pass to pick up). This listener only
  // fills in the popup's content; popup/component.js's own listener on
  // the same button does the actual opening.
  document.body.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-action="view-field"]');
    if (!btn) return;
    const row = btn.closest('.ds-table-row');
    const name = row.querySelector('[data-type="text-active-cell"] .ds-table-cell__text').textContent.trim();
    const fieldLabel = btn.dataset.fieldLabel || 'Detail';
    const source = btn.parentElement.querySelector('[data-view-source]');
    const fullText = (source || btn.parentElement.firstChild).textContent.trim();
    // A mock analysis is never presented as a real one (CLAUDE.md, Data sources).
    const mock = fieldLabel === 'Summary' && row.dataset.origin === 'mock' ? ' (mock)' : '';
    document.getElementById('foryou-view-popup-title').textContent = `${name} ${fieldLabel}${mock}`;
    // Sections with a heading (the Summary) render as heading + paragraph; any
    // other text is one paragraph. textContent only: it comes from a stranger.
    const target = document.getElementById('foryou-view-text');
    let sections = null;
    try { sections = source && source.dataset.viewSections ? JSON.parse(source.dataset.viewSections) : null; } catch (error) { sections = null; }
    target.replaceChildren();
    (Array.isArray(sections) ? sections : [{ text: fullText }]).forEach((section) => {
      if (section.heading) {
        const heading = document.createElement('h3');
        heading.className = 'ds-popup__heading';
        heading.textContent = section.heading;
        target.appendChild(heading);
      }
      const paragraph = document.createElement('p');
      paragraph.textContent = section.text || '';
      target.appendChild(paragraph);
    });
  });

  // ---- Analyse and Prepare Application (SS4.4.2, SS4.3.1) ----
  // Every call that makes a Claude call is confirmed first (SS3.7), with a figure the
  // server computes from the items (never a fixed string). Nothing changes on the page
  // until the server has answered; on failure the rows stay exactly as they were.
  // While the server runs the mock the popup says so and nothing is charged.
  const confirmFor = {
    analyse: { body: document.getElementById('foryou-analyse-confirm-body'), btn: document.getElementById('foryou-analyse-confirm-btn') },
    prepare: { body: document.getElementById('foryou-prepare-confirm-body'), btn: document.getElementById('foryou-prepare-confirm-btn') },
  };
  let pending = null; // { action, rows, skipped }

  async function askCost(action, rows, skipped) {
    pending = { action, rows, skipped };
    const c = confirmFor[action];
    c.btn.disabled = true;
    c.btn.dataset.status = 'disabled';
    c.body.textContent = 'Working out the cost...';
    let estimate;
    try {
      estimate = await ArtaApi.opportunities.costEstimate(action, rows.map((row) => row.dataset.id));
    } catch (error) {
      if (pending && pending.rows === rows) c.body.textContent = `Could not work out the cost: ${error.message}`;
      return;
    }
    if (!pending || pending.rows !== rows) return; // another confirmation was opened meanwhile
    const usd = estimate.usd.toFixed(2);
    const what = action === 'prepare'
      ? 'Prepare Application.'
      : (rows.length === 1 ? `${rowName(rows[0])}.` : `${rows.length} items.`);
    c.body.textContent = estimate.mocked
      ? `${what} This call is mocked: nothing is charged. A real call would cost approximately $${usd}.`
      : `${what} This makes a real Claude API call and costs approximately $${usd}.`;
    if (action === 'analyse') setLabel(c.btn, estimate.mocked ? 'Analyse (mock)' : `Analyse for ${usd}$`);
    else setLabel(c.btn, estimate.mocked ? 'Run (mock)' : 'Run using API');
    c.btn.disabled = false;
    c.btn.dataset.status = 'active';
  }

  document.body.addEventListener('click', (event) => {
    const analyse = event.target.closest('[data-action="analyse"]:not([disabled])');
    const analyseRow = analyse && homeRow(analyse);
    if (analyseRow) askCost('analyse', [analyseRow], 0);
    const prepare = event.target.closest('[data-action="prepare"]');
    const prepareRow = prepare && homeRow(prepare);
    if (prepareRow) askCost('prepare', [prepareRow], 0);
  });

  // Bulk: only the selected rows that can take the action are sent, and the toast says
  // how many were left out.
  document.querySelector('[data-action="bulk-analyse"]')?.addEventListener('click', () => {
    const selected = selectedRows(reviewTable);
    const rows = selected.filter((row) => row.querySelector('[data-action="analyse"]:not([disabled])'));
    askCost('analyse', rows, selected.length - rows.length);
  });
  document.querySelector('[data-action="bulk-prepare"]')?.addEventListener('click', () => {
    const selected = selectedRows(analysedTable);
    const rows = selected.filter((row) => row.dataset.status === 'analysed' && !row.dataset.prepared);
    askCost('prepare', rows, selected.length - rows.length);
  });

  confirmFor.analyse.btn.addEventListener('click', async () => {
    const run = pending;
    pending = null;
    if (!run || run.action !== 'analyse' || !run.rows.length) return;
    // The click may have come from the All tab, so the twin's button shows it too.
    const buttons = run.rows.flatMap((row) => [row, ...twinsOf(row)])
      .map((row) => row.querySelector('[data-action="analyse"]')).filter(Boolean);
    buttons.forEach((btn) => { setLabel(btn, 'Analyzing...'); btn.dataset.status = 'loading'; btn.disabled = true; });
    let saved;
    try {
      saved = await ArtaApi.opportunities.analyse(run.rows.map((row) => row.dataset.id));
    } catch (error) {
      buttons.forEach((btn) => { setLabel(btn, 'Analyze'); btn.dataset.status = 'active'; btn.disabled = false; });
      toastError(`Analyze failed: ${error.message}`);
      return;
    }
    const byId = new Map(saved.items.map((item) => [item.id, item]));
    run.rows.forEach((row) => {
      row.remove();
      ForYou.reshape(row, 'analysed', byId.get(row.dataset.id));
      row.dataset.selected = 'false';
      analysedTable.appendChild(row);
    });
    resort(analysedTable);
    refreshSelectAll(reviewTable, analysedTable);
    flashRows(run.rows);
    // The All tab's twin of each row is reshaped in place by syncAll, so it flashes too.
    syncAll(run.rows.map((row) => row.dataset.id)).then(() => flashRows(run.rows.flatMap(twinsOf)));
    const n = run.rows.length;
    window.dsToast.show({
      message: `${n === 1 ? 'Analysed' : `${n} analysed`}${run.skipped ? `, ${run.skipped} left out (no description)` : ''}.`,
    });
  });

  confirmFor.prepare.btn.addEventListener('click', async () => {
    const run = pending;
    pending = null;
    if (!run || run.action !== 'prepare' || !run.rows.length) return;
    let made;
    try {
      made = await ArtaApi.opportunities.prepare(run.rows.map((row) => row.dataset.id));
    } catch (error) {
      toastError(`Could not prepare: ${error.message}`);
      return;
    }
    const byId = new Map(made.items.map((item) => [item.opportunity.id, item]));
    run.rows.forEach((row) => ForYou.reshape(row, 'analysed', byId.get(row.dataset.id).opportunity));
    syncAll(run.rows.map((row) => row.dataset.id));
    const open = (page, id) => () => { window.location.href = `${page}?set=${encodeURIComponent(id)}`; };
    if (run.rows.length === 1) {
      const item = made.items[0];
      window.dsToast.show({
        message: `${item.opportunity.fields.name}: CV set and portfolio set generated.`,
        actions: [
          { label: 'View CV set', onClick: open('cv.html', item.cv_set_id) },
          { label: 'View portfolio set', onClick: open('portfolio.html', item.portfolio_set_id) },
        ],
      });
    } else {
      window.dsToast.show({
        message: `${run.rows.length}: CV sets and portfolio sets generated${run.skipped ? `, ${run.skipped} left out (not analysed only)` : ''}.`,
      });
    }
  });

  // ---- Archive (For Review/Analysed -> Archived) ----
  // Archiving asks first (Figma 150:16695): Archive on a row and the bulk
  // Archive both fill the confirmation and set the rows aside; the popup's
  // confirming button does the move. Cancel leaves everything as it was.
  let archivePendingRows = [];
  function requestArchive(rows) {
    archivePendingRows = rows;
    document.getElementById('foryou-archive-confirm-body').textContent = rows.length === 1
      ? `${rowName(rows[0])} moves to Archived. You can unarchive it at any time.`
      : `${rows.length} items move to Archived. You can unarchive them at any time.`;
  }

  // Rebuilt for the Archived table's columns from the saved record and
  // inserted at the top (SS4.5: a newly archived item comes first, so
  // undoing a mistaken archive is immediate). Returns the table it left.
  function archiveRow(row, record) {
    const sourceTable = row.parentElement;
    row.remove();
    ForYou.reshape(row, 'archived', record);
    const firstRow = archivedTable.querySelector(':scope > .ds-table-row');
    if (firstRow) firstRow.before(row);
    else archivedTable.appendChild(row);
    row.dataset.selected = 'false';
    return sourceTable;
  }

  document.getElementById('foryou-archive-confirm-btn').addEventListener('click', async () => {
    const rows = archivePendingRows;
    archivePendingRows = [];
    if (!rows.length) return;
    let saved;
    try {
      saved = await ArtaApi.opportunities.archive(rows.map((row) => row.dataset.id));
    } catch (error) {
      toastError(`Could not archive: ${error.message}`);
      return;
    }
    const byId = new Map(saved.items.map((item) => [item.id, item]));
    const sources = new Set(rows.map((row) => archiveRow(row, byId.get(row.dataset.id))));
    syncAll(rows.map((row) => row.dataset.id));
    refreshSelectAll(...sources, archivedTable);
    window.dsToast.show({ message: rows.length === 1 ? 'Archived.' : `${rows.length} archived.` });
  });

  // Unarchive returns each item to the table it came from (SS4.5): the
  // server keeps stage on archive, so its answer says which table that is.
  async function unarchiveRows(rows) {
    if (!rows.length) return;
    let saved;
    try {
      saved = await ArtaApi.opportunities.unarchive(rows.map((row) => row.dataset.id));
    } catch (error) {
      toastError(`Could not unarchive: ${error.message}`);
      return;
    }
    const byId = new Map(saved.items.map((item) => [item.id, item]));
    const targets = new Set();
    rows.forEach((row) => {
      const record = byId.get(row.dataset.id);
      const kind = ForYou.kindOf(record);
      row.remove();
      ForYou.reshape(row, kind, record);
      tableFor[kind].appendChild(row);
      row.dataset.selected = 'false';
      targets.add(kind);
    });
    syncAll(rows.map((row) => row.dataset.id));
    refreshSelectAll(archivedTable, ...Array.from(targets, (kind) => tableFor[kind]));
    const where = Array.from(targets, (kind) => tableLabel[kind]).join(' and ');
    window.dsToast.show({
      message: rows.length === 1 ? `Unarchived -- back in ${where}.` : `${rows.length} unarchived -- back in ${where}.`,
    });
  }

  // Filtered -> For Review. The server drops the filter reason.
  async function promoteRows(rows) {
    if (!rows.length) return false;
    let saved;
    try {
      saved = await ArtaApi.opportunities.promote(rows.map((row) => row.dataset.id));
    } catch (error) {
      toastError(`Could not move to For Review: ${error.message}`);
      return false;
    }
    const byId = new Map(saved.items.map((item) => [item.id, item]));
    rows.forEach((row) => {
      row.remove();
      ForYou.reshape(row, 'review', byId.get(row.dataset.id));
      reviewTable.appendChild(row);
      row.dataset.selected = 'false';
    });
    syncAll(rows.map((row) => row.dataset.id));
    refreshSelectAll(filteredTable, reviewTable);
    window.dsToast.show({
      message: rows.length === 1 ? 'Moved to For Review.' : `${rows.length} moved to For Review.`,
    });
    return true;
  }

  // ---- Status dropdown (dsgnrfhmn, 2026-09-21): a choice does what the matching action
  // does, through the same calls. Filtered -> For review is the promote call. A For
  // review row's dropdown is disabled: it leaves For review through Analyze. The three
  // application statuses are PUT .../application, which
  // starts an application for an Analysed row (its Updates then show in the All table).
  // Back to For review (from anywhere) or Analysed (from an application status) is the
  // same PUT (dsgnrfhmn, 2026-10-25); the options the API would refuse are disabled
  // (for-you-rows.js). On any failure the dropdown returns to the status the row had.
  function showStatus(cell, status) {
    cell.querySelector('[data-dropdown-value]').textContent = ForYou.STATUS_LABEL[status];
    cell.querySelectorAll('.ds-dropdown-menu__item').forEach((item) => {
      item.setAttribute('aria-selected', String(item.dataset.value === status));
    });
  }

  document.body.addEventListener('ds-dropdown-select', async (event) => {
    const cell = event.target.closest('.foryou-table .ds-table-cell[data-type="dropdown"]');
    if (!cell) return;
    const row = cell.closest('.ds-table-row');
    const previous = row.dataset.status;
    const value = event.detail.value;
    const home = homeRow(cell);
    if (!home || value === previous) { showStatus(cell, previous); return; }

    if (previous === 'filtered' && value === 'for_review') {
      if (!(await promoteRows([home]))) showStatus(cell, previous);
      return;
    }
    const back = value === 'for_review' || (value === 'analysed' && ForYou.hasApplication(previous));
    if (!back && (!ForYou.hasApplication(value) || !['analysed', ...ForYou.APPLICATION_STATUSES].includes(previous))) {
      showStatus(cell, previous);
      toastError(`A record cannot move from ${ForYou.STATUS_LABEL[previous]} to ${ForYou.STATUS_LABEL[value]}.`);
      return;
    }
    let record;
    try {
      record = await ArtaApi.opportunities.setStatus(home.dataset.id, value);
    } catch (error) {
      showStatus(cell, previous);
      toastError(`Could not change the status: ${error.message}`);
      return;
    }
    const kind = ForYou.kindOf(record);
    ForYou.reshape(home, kind, record);
    resort(tableFor[kind]);
    syncAll([record.id]);
    window.dsToast.show({
      message: value === 'for_review' && previous !== 'filtered'
        ? `${record.fields.name}: back in For review. Analyze it to get its summary again.`
        : `${record.fields.name}: ${ForYou.STATUS_LABEL[value]}.`,
    });
    document.dispatchEvent(new CustomEvent('foryou-record', { detail: { record } }));
  });

  document.body.addEventListener('click', (event) => {
    const rowFor = (selector) => {
      const btn = event.target.closest(selector);
      return btn ? homeRow(btn) : null;
    };
    const archiveTarget = rowFor('[data-action="archive"]');
    if (archiveTarget) requestArchive([archiveTarget]);

    const unarchiveTarget = rowFor('[data-action="unarchive"]');
    if (unarchiveTarget) unarchiveRows([unarchiveTarget]);

    const forReviewTarget = rowFor('[data-action="for-review"]');
    if (forReviewTarget) promoteRows([forReviewTarget]);
  });

  // ---- Header bulk actions (Archive/Analyze/Prepare/
  // Unarchive/Move to "For Review" -- generic per active tab's own
  // gated data-actions-for pairing, applied to every selected row as
  // one operation per SS3.4.5). ----
  function selectedRows(table) {
    return Array.from(table.querySelectorAll(':scope > .ds-table-row[data-selected="true"]'));
  }

  document.querySelectorAll('.ds-table-header-actions[data-actions-for] [data-action="bulk-archive"]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const table = document.getElementById(btn.closest('.ds-table-header-actions').dataset.actionsFor);
      requestArchive(selectedRows(table));
    });
  });

  document.querySelector('[data-action="bulk-unarchive"]')?.addEventListener('click', () => {
    unarchiveRows(selectedRows(archivedTable));
  });

  document.querySelector('[data-action="bulk-for-review"]')?.addEventListener('click', () => {
    promoteRows(selectedRows(filteredTable));
  });

  // ---- All tab bulk actions (dsgnrfhmn, 2026-09-24): a button shows only when it
  // fits every selected row, and acts on those records' rows in their own tabs
  // through the same flows as the tab's own buttons (syncAll then rebuilds the
  // All rows and clears their selection).
  const allActions = document.getElementById('foryou-all-actions');
  const allFits = {
    archive: (row) => ['for_review', 'analysed', ...ForYou.APPLICATION_STATUSES].includes(row.dataset.status),
    analyse: (row) => row.dataset.status === 'for_review' && Boolean(row.querySelector('[data-action="analyse"]:not([disabled])')),
    prepare: (row) => row.dataset.status === 'analysed' && !row.dataset.prepared,
    unarchive: (row) => row.dataset.status === 'archived',
    'for-review': (row) => row.dataset.status === 'filtered',
  };
  function syncAllActions() {
    const rows = selectedRows(allTable);
    allActions.querySelectorAll('[data-all-action]').forEach((btn) => {
      btn.hidden = !rows.length || !rows.every(allFits[btn.dataset.allAction]);
    });
  }
  allTable.addEventListener('ds-table-row-select', syncAllActions);
  allActions.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-all-action]');
    if (!btn) return;
    const homes = selectedRows(allTable).map((row) => homeRow(row.querySelector('.ds-table-cell'))).filter(Boolean);
    if (!homes.length) return;
    const run = {
      archive: () => requestArchive(homes),
      analyse: () => askCost('analyse', homes, 0),
      prepare: () => askCost('prepare', homes, 0),
      unarchive: () => unarchiveRows(homes),
      'for-review': () => promoteRows(homes),
    }[btn.dataset.allAction];
    run();
  });

  // ---- Load new opportunities (hero): reads every connected source (read-only),
  // then the page reloads so every table shows what the server now holds. ----
  const loadButton = document.getElementById('foryou-load-opportunities');
  loadButton.addEventListener('click', async () => {
    if (loadButton.dataset.busy) return;
    loadButton.dataset.busy = 'true';
    loadButton.dataset.status = 'loading';
    try {
      const result = await ArtaApi.intake.run();
      window.dsToast.show({ message: `${result.added} new, ${result.filtered} filtered.` });
      if (result.added || result.filtered) setTimeout(() => window.location.reload(), 900);
    } catch (error) {
      toastError(`Could not load opportunities: ${error.message}`);
    } finally {
      delete loadButton.dataset.busy;
      loadButton.dataset.status = 'active';
    }
  });

  // ---- Updates (moved here from the Tracker page, spec 7.5 / 7.6) ----
  // A row's "(N) Updates" button opens the replies for that record; opening marks
  // them read. The hero's "Updates (N new)" asks the mailbox for new replies
  // (no Claude call, no cost popup: button loading state only). Reply text is
  // set with textContent: it comes from a stranger.
  const updatesPopupTitle = document.getElementById('foryou-updates-popup-title');
  const updatesPopupBody = document.getElementById('foryou-updates-popup-body');
  const refreshButton = document.getElementById('foryou-updates-refresh');

  function showUnread(unread) {
    setLabel(refreshButton, `Updates (${unread} new)`);
  }
  document.addEventListener('arta-unread', (event) => showUnread(event.detail.unread));
  ArtaBoot.navDot();   // boot's own call may have answered before this listener existed

  // A record from the server, put into its row: the Updates button and its sort value.
  function showUpdates(row, updates) {
    const btn = row.querySelector('[data-action="updates"]');
    if (!btn) return;
    btn.dataset.updatesCount = String(updates.total);
    btn.dataset.unread = String(updates.unread > 0);
    setLabel(btn, `(${updates.total}) Updates`);
    btn.closest('.ds-table-cell').dataset.sortValue = String(updates.total);
  }

  function renderReplies(items) {
    updatesPopupBody.replaceChildren();
    if (!items.length) {
      const p = document.createElement('p');
      p.textContent = 'No replies yet.';
      updatesPopupBody.appendChild(p);
      return;
    }
    items.forEach((item, i) => {
      if (i > 0) updatesPopupBody.appendChild(Object.assign(document.createElement('hr'), { className: 'ds-divider' }));
      const head = document.createElement('p');
      head.textContent = `${item.sender} - ${formatReplyDate(item.received_at)}`;
      updatesPopupBody.appendChild(head);
      if (item.subject) {
        const subject = document.createElement('p');
        subject.textContent = item.subject;
        updatesPopupBody.appendChild(subject);
      }
      const body = document.createElement('p');
      body.dataset.updateBody = '';
      body.textContent = item.body;
      updatesPopupBody.appendChild(body);
    });
  }

  function formatReplyDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
    return m ? `${m[3]}.${m[2]}.${m[1]}` : '';
  }

  document.body.addEventListener('click', async (event) => {
    const btn = event.target.closest('[data-action="updates"]');
    if (!btn) return;
    const row = btn.closest('.ds-table-row');
    updatesPopupTitle.textContent = `Updates - ${rowName(row)}`;
    const wait = document.createElement('p');
    wait.textContent = 'Loading...';
    updatesPopupBody.replaceChildren(wait);
    try {
      renderReplies((await ArtaApi.opportunities.updates(row.dataset.id)).items);
      // Opening the popup marks every reply on the row read: the dot goes, the
      // count stays the total, not an unread count.
      const read = await ArtaApi.opportunities.readUpdates(row.dataset.id);
      showUpdates(row, read.opportunity.updates);
      ArtaBoot.navDot(read.unread);
    } catch (error) {
      const p = document.createElement('p');
      p.textContent = `Could not load the replies: ${error.message}`;
      updatesPopupBody.replaceChildren(p);
    }
  });

  refreshButton.addEventListener('click', async () => {
    if (refreshButton.dataset.busy) return;
    refreshButton.dataset.busy = 'true';
    refreshButton.dataset.status = 'loading';
    const originalLabel = refreshButton.textContent;
    setLabel(refreshButton, 'Refreshing...');
    try {
      const result = await ArtaApi.tracker.refresh();
      // Every row with an application is read again (archived ones show their
      // replies exactly as active ones do, spec 7.4).
      const all = await ArtaApi.opportunities.list({});
      const rows = new Map(Array.from(allTable.querySelectorAll(':scope > .ds-table-row[data-id]')).map((row) => [row.dataset.id, row]));
      all.items.forEach((rec) => {
        if (rec.application && rows.has(rec.id)) showUpdates(rows.get(rec.id), rec.updates);
      });
      ArtaBoot.navDot(result.unread);
      window.dsToast.show({
        message: result.new === 0 ? 'No new replies.' : `${result.new} new ${result.new === 1 ? 'reply' : 'replies'}.`,
      });
    } catch (error) {
      setLabel(refreshButton, originalLabel);
      toastError(`Could not refresh updates: ${error.message}`);
    } finally {
      delete refreshButton.dataset.busy;
      refreshButton.dataset.status = 'active';
    }
  });

  // ---- Load more / Load all (Figma 64:7799 footer; mock, real paging is Step 3) ----
  document.querySelectorAll('.ds-table-footer [data-action="load-more"], .ds-table-footer [data-action="load-all"]').forEach((btn) => {
    btn.addEventListener('click', () => {
      window.dsToast.show({ message: 'Mock data: no intake connected yet, nothing more to load.' });
    });
  });

  // ---- Saved (SS3.8): the favourite icons flip themselves (one row, a
  // shift-click range, or the header's save-all). Afterwards the page
  // compares every row with what the server last confirmed and sends
  // only the difference, so all three ways share one path. On failure the
  // icons go back to the confirmed state.
  const savedOnServer = new Map();
  const favouriteIcon = (row) => row.querySelector('.ds-table-cell[data-type="icon-cell"] .ds-table-cell__icon[data-icon="save"]');
  const isSaved = (row) => favouriteIcon(row).getAttribute('data-saved') === 'true';
  const allRows = () => Array.from(document.querySelectorAll('.foryou-table > .ds-table-row[data-id]'));
  allRows().forEach((row) => savedOnServer.set(row.dataset.id, isSaved(row)));

  // A record has two rows (its own table and All), each with its own icon. Whichever
  // one was clicked differs from the server; the other follows it. The header's
  // "all saved" icon of the other table is told with the same event a click sends.
  function mirrorSaved() {
    const byId = new Map();
    allRows().forEach((row) => byId.set(row.dataset.id, [...(byId.get(row.dataset.id) || []), row]));
    byId.forEach((rows, id) => {
      const changed = rows.find((row) => isSaved(row) !== savedOnServer.get(id));
      if (!changed) return;
      rows.filter((row) => isSaved(row) !== isSaved(changed)).forEach((row) => {
        const icon = favouriteIcon(row);
        icon.setAttribute('data-saved', String(isSaved(changed)));
        icon.closest('.ds-table-cell').dataset.sortValue = String(isSaved(changed));
        icon.dispatchEvent(new CustomEvent('ds-table-cell-favourite-toggle', { bubbles: true, detail: { saved: isSaved(changed) } }));
      });
    });
  }

  async function syncSaved() {
    mirrorSaved();
    for (const value of [true, false]) {
      const rows = allRows().filter((row) => isSaved(row) === value && savedOnServer.get(row.dataset.id) !== value);
      if (!rows.length) continue;
      const ids = Array.from(new Set(rows.map((row) => row.dataset.id)));
      try {
        await ArtaApi.opportunities.setSaved(ids, value);
        ids.forEach((id) => savedOnServer.set(id, value));
      } catch (error) {
        rows.forEach((row) => favouriteIcon(row).setAttribute('data-saved', String(!value)));
        toastError(`Could not save: ${error.message}`);
      }
    }
  }
  // After the click's own listeners have finished (the header icon updates
  // the rows after it dispatches the toggle event).
  document.body.addEventListener('ds-table-cell-favourite-toggle', () => setTimeout(syncSaved, 0));

  // ---- Notes (SS3.9): saved on blur, one value per opportunity.
  document.body.addEventListener('change', async (event) => {
    const input = event.target.closest('.foryou-table [data-type="input"] .ds-input');
    if (!input) return;
    const row = input.closest('.ds-table-row');
    try {
      const record = await ArtaApi.opportunities.patch(row.dataset.id, { notes: input.value });
      // The record's other row (its own table, or All) shows the note too.
      [row, ...twinsOf(row)].forEach((r) => {
        const field = r.querySelector('[data-type="input"] .ds-input');
        if (!field) return;
        field.defaultValue = record.fields.notes;
        field.value = record.fields.notes;
        field.closest('.ds-table-cell').dataset.sortValue = record.fields.notes.toLowerCase();
      });
    } catch (error) {
      input.value = input.defaultValue;
      toastError(`Could not save the note: ${error.message}`);
    }
  });
})();
