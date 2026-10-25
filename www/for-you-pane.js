/*
  For You: a record's details beside the All table (dsgnrfhmn, 2026-10-25), docked like a task
  pane in Word (dis-system detail-pane, staged). A click on a row itself (not on one of
  its controls) opens them; the open row's click, Close or Esc closes them, another row
  swaps them. The open row is the table's current row (table-row's data-current).

  The pane shows the record from the API: its actions (Analyze, Prepare Application,
  Archive, Unarchive, Move to "For Review", Add Description, its Updates), Open link, the
  Status dropdown, the dates, the Summary when there is one (a record without one shows
  its Full Description and the rest), the reason it was filtered, and Notes.

  Every control in the pane acts through the row's own control, so it does exactly what
  the row does (confirmations, cost popups, the calls, the rows' redraw): a button clicks
  the row's button, a status choice is the row dropdown's choice, a note is the row's note.
  The pane's copies carry no data-action, so the page's row handlers never see them.
  Whenever the open row is redrawn (a status, an action), the pane reads the record again.
*/
(function () {
  const allTable = document.getElementById('foryou-all-table');
  const splitEl = document.getElementById('foryou-split');
  const template = document.getElementById('foryou-detail-pane');
  if (!allTable || !splitEl || !template || !window.DsDetailPane) return;

  const rowOf = (id) => allTable.querySelector(`:scope > .ds-table-row[data-id="${CSS.escape(id)}"]`);
  const pane = DsDetailPane.split(splitEl, { onClose: () => markRow(null) });
  let watcher = null;

  function markRow(id) {
    allTable.querySelectorAll(':scope > .ds-table-row[data-current]').forEach((row) => {
      row.removeAttribute('data-current');
      row.removeAttribute('aria-current');
    });
    const row = id && rowOf(id);
    if (row) {
      row.setAttribute('data-current', 'true');
      row.setAttribute('aria-current', 'true');
    }
  }

  // ---- the pane's content ------------------------------------------------------------

  function paragraph(text) {
    const p = document.createElement('p');
    p.className = 'ds-detail__text';
    p.textContent = text;
    return p;
  }

  // A pane copy of one of the row's buttons; clicking it clicks the row's.
  function proxy(rowButton) {
    const copy = rowButton.cloneNode(true);
    const action = copy.dataset.action;
    copy.removeAttribute('data-action');
    copy.removeAttribute('data-popup-open');
    copy.removeAttribute('id');
    copy.dataset.proxy = action;
    return copy;
  }

  function rowActions(row) {
    const wanted = ['analyse', 'prepare', 'archive', 'unarchive', 'for-review', 'add-description', 'updates'];
    return wanted
      .map((action) => row.querySelector(`[data-action="${action}"]`))
      .filter(Boolean)
      .map(proxy);
  }

  function linkButton(rec) {
    const href = ForYou.safeHref(rec.fields.link);
    if (href === '#') return null;
    const a = document.createElement('a');
    a.className = 'ds-text-button';
    a.dataset.priority = 'secondary';
    a.dataset.status = 'active';
    a.dataset.underline = 'solid';   // it leaves the page (dis-system's convention)
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.innerHTML = '<span class="ds-text-button__text">Open link</span>';
    return a;
  }

  function statusControl(row) {
    const dropdown = row.querySelector('.ds-table-cell[data-type="dropdown"] .ds-dropdown');
    if (!dropdown) return null;
    const copy = dropdown.cloneNode(true);
    copy.querySelectorAll('[data-dropdown-trigger]').forEach((b) => b.removeAttribute('data-tooltip'));
    return copy;
  }

  function dates(rec) {
    const f = rec.fields;
    const lines = [];
    if (f.deadline) lines.push(`Apply by ${ForYou.formatDate(f.deadline)}`);
    if (f.residency_start || f.residency_end) {
      lines.push(`Residency ${[f.residency_start, f.residency_end].filter(Boolean).map(ForYou.formatDate).join(' to ')}`);
    }
    if (f.decision_date) lines.push(`Decision ${ForYou.formatDate(f.decision_date)}`);
    return lines.join('\n');
  }

  function summary(rec) {
    const box = document.createElement('div');
    const sections = ForYou.summarySections(rec);
    (Array.isArray(sections) && sections.length ? sections : [{ text: rec.fields.summary }]).forEach((section) => {
      if (section.heading) {
        const h = document.createElement('h4');
        h.className = 'ds-detail__heading';
        h.textContent = section.heading;
        box.appendChild(h);
      }
      box.appendChild(paragraph(section.text || ''));
    });
    return box;
  }

  function notes(rec) {
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'ds-input ds-detail__field';
    input.dataset.fill = 'filled';
    input.placeholder = 'Add a note';
    input.setAttribute('aria-label', 'Notes');
    input.value = rec.fields.notes || '';
    input.dataset.paneNotes = '';
    return input;
  }

  function fill(el, rec, row) {
    const status = ForYou.statusOf(rec);
    el.setAttribute('aria-label', `Details of ${rec.fields.name}`);
    el.querySelector('.ds-pane__title').textContent = rec.fields.name;
    el.querySelector('.ds-pane__caption').textContent =
      `${rec.fields.type === 'grant' ? 'Grant' : 'Open call'}, ${ForYou.STATUS_LABEL[status]}`;
    const B = DsDetailPane.block;
    const parts = [];
    const buttons = [...rowActions(row), linkButton(rec)].filter(Boolean);
    if (buttons.length) parts.push(DsDetailPane.actions(buttons));
    const statusEl = statusControl(row);
    if (statusEl) parts.push(B('Status', statusEl));
    const when = dates(rec);
    if (when) parts.push(B('Dates', when));
    if (rec.fields.summary) {
      // A mock analysis is never presented as a real one (CLAUDE.md, Data sources).
      parts.push(B(rec.fields.analysis_origin === 'mock' ? 'Summary (mock)' : 'Summary', summary(rec)));
    }
    parts.push(B('Full description', rec.fields.description || 'No description yet.'));
    if (rec.fields.filter_reason) parts.push(B('Reason to filter', rec.fields.filter_reason));
    parts.push(B('Notes', notes(rec)));
    const body = el.querySelector('.ds-detail__body');
    body.replaceChildren(...parts);
    // The dropdown copy is wired like the rows' own (dropdown-menu's initDropdown), and
    // given the arrow the row's trigger already has.
    body.querySelectorAll('.ds-dropdown').forEach((d) => window.initDropdown && window.initDropdown(d));
  }

  // ---- opening, closing, keeping up with the row ---------------------------------------

  async function show(id, keepScroll) {
    const row = rowOf(id);
    if (!row) return pane.close();
    let rec;
    try {
      rec = await ArtaApi.opportunities.get(id);
    } catch (error) {
      window.dsToast.show({ message: `Could not open the details: ${error.message}`, status: 'error' });
      return;
    }
    const current = pane.openId === id ? pane.pane() : null;
    const el = current || template.content.firstElementChild.cloneNode(true);
    const scroller = el.querySelector('.ds-pane__scroll');
    const top = keepScroll && current ? scroller.scrollTop : 0;
    fill(el, rec, row);
    if (!current) pane.open(id, el);
    scroller.scrollTop = top;
    markRow(id);
    watch(row, id);
  }

  // The row is redrawn by the page after a status change or an action: read it again.
  function watch(row, id) {
    if (watcher) watcher.disconnect();
    let timer = 0;
    watcher = new MutationObserver(() => {
      clearTimeout(timer);
      timer = setTimeout(() => { if (pane.openId === id) show(id, true); }, 150);
    });
    watcher.observe(row, { childList: true, attributes: true, attributeFilter: ['data-status', 'data-prepared'] });
  }

  function close() {
    if (watcher) watcher.disconnect();
    watcher = null;
    pane.close();
  }

  // A click on the row itself, not on one of its controls.
  allTable.addEventListener('click', (event) => {
    const row = event.target.closest('.ds-table-row[data-id]');
    if (!row || row.parentElement !== allTable) return;
    if (event.target.closest('a[href], button, input, textarea, select, .ds-dropdown, [data-type="checkbox-cell"], [data-type="icon-cell"]')) return;
    if (pane.openId === row.dataset.id) close();
    else show(row.dataset.id, false);
  });

  pane.side.addEventListener('click', (event) => {
    if (event.target.closest('[data-pane-close]')) return close();
    const copy = event.target.closest('[data-proxy]');
    if (!copy || copy.disabled) return;
    const row = pane.openId && rowOf(pane.openId);
    const own = row && row.querySelector(`[data-action="${copy.dataset.proxy}"]`);
    if (own) own.click();
  });

  // A status picked in the pane is the row dropdown's pick.
  pane.side.addEventListener('ds-dropdown-select', (event) => {
    event.stopPropagation();
    const row = pane.openId && rowOf(pane.openId);
    const dropdown = row && row.querySelector('.ds-table-cell[data-type="dropdown"] .ds-dropdown');
    if (dropdown) dropdown.dispatchEvent(new CustomEvent('ds-dropdown-select', { bubbles: true, detail: event.detail }));
  });

  // A note written in the pane is the row's note (saved by the page on change).
  pane.side.addEventListener('change', (event) => {
    const input = event.target.closest('[data-pane-notes]');
    const row = input && pane.openId && rowOf(pane.openId);
    const own = row && row.querySelector('[data-type="input"] .ds-input');
    if (!own) return;
    own.value = input.value;
    own.dispatchEvent(new Event('change', { bubbles: true }));
  });
})();
