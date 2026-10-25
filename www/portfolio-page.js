(function () {
  // The sets, their projects and the project list come from the API
  // (arta-api.js). Nothing is kept here that the server does not confirm:
  // every change is sent first and the page shows the set the server
  // sends back. On failure the page stays as it was and a toast says why.
  const setsApi = ArtaApi.sets.portfolio;

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const describe = (entry) => `${plural(entry.image_count, 'image')}, ${entry.text_amount}`;
  const entryName = (entry) => (entry.project_missing ? 'Project not found' : entry.project_name);
  const toastError = (message) => window.dsToast.show({ message, status: 'error' });

  let knownSetIds = new Set(); // every set the server has, archived ones too
  let sets = [];    // the sidebar, in the server's order (SS5.3)
  let active = null; // the open set, as the server last sent it

  const el = {
    list: document.getElementById('portfolio-sidebar-list'),
    title: document.getElementById('portfolio-set-title'),
    saveAs: document.getElementById('portfolio-save-set'),
    table: document.getElementById('portfolio-table'),
    count: document.getElementById('portfolio-table-count'),
    previewCount: document.getElementById('portfolio-preview-count'),
    previewEmpty: document.getElementById('portfolio-preview-empty'),
    previewWrapper: document.getElementById('portfolio-preview-wrapper'),
    previewPages: document.getElementById('portfolio-preview-pages'),
    preview: document.querySelector('.portfolio-preview'),
    zoomBar: document.getElementById('portfolio-zoom-bar'),
    undo: document.getElementById('portfolio-undo'),
    redo: document.getElementById('portfolio-redo'),
  };

  // ---- Render ----
  function renderSidebar() {
    // The first rows are the create rows, drawn like Figma D_SYSTEM 28:1729 ("Create New
    // Set": a bold dotted name, a plus in the 44px cell, a closing hairline). Part of the
    // list, so they scroll with it and hide with it when the sidebar is collapsed. The
    // icon cell is a second trigger for the same action; it does not toggle anything.
    // New Empty Set makes an empty set and opens it (empty table with its Add Project row,
    // empty preview; no popup); New Full Set makes a set of every project with all its
    // pictures and full text, to be thinned out (dsgnrfhmn, 2026-10-24).
    const create = `
      <div class="ds-sidebar-item" data-type="set" data-role="create">
        <button type="button" class="ds-text-button" data-priority="primary" data-status="active" data-underline="dotted" id="portfolio-create-set"><span class="ds-text-button__text">New Empty Set</span></button>
        <button type="button" class="ds-sidebar-item__save" data-create-empty aria-label="New Empty Set" tabindex="-1"><span class="ds-sidebar-item__save-icon" data-icon="add" aria-hidden="true"></span></button>
      </div>
      <div class="ds-sidebar-item" data-type="set" data-role="create">
        <button type="button" class="ds-text-button" data-priority="primary" data-status="active" data-underline="dotted" id="portfolio-create-full-set"><span class="ds-text-button__text">New Full Set</span></button>
        <button type="button" class="ds-sidebar-item__save" data-create-full aria-label="New Full Set" tabindex="-1"><span class="ds-sidebar-item__save-icon" data-icon="add" aria-hidden="true"></span></button>
      </div>`;
    el.list.innerHTML = create + sets.map((s) => `
      <div class="ds-sidebar-item" data-type="set" data-set-id="${esc(s.id)}" data-active="${!!active && s.id === active.id}" data-context-menu="portfolio-set-menu">
        <button type="button" class="ds-text-button" data-priority="secondary" data-status="active" data-underline="dotted" data-select-set aria-current="${!!active && s.id === active.id}"><span class="ds-text-button__text">${esc(s.name)}</span></button>
        <button type="button" class="ds-sidebar-item__save" data-save-set aria-pressed="${s.saved}" aria-label="${s.saved ? 'Saved' : 'Save'}: ${esc(s.name)}"><span class="ds-sidebar-item__save-icon" data-icon="save" aria-hidden="true"></span></button>
      </div>`).join('');
    ArtaBoot.icons(el.list);
  }

  function renderTable() {
    const entries = active ? active.entries : [];
    el.table.querySelectorAll(':scope > .ds-table-row').forEach((row) => row.remove());
    el.table.querySelectorAll(':scope > [data-add-row]').forEach((row) => row.remove());
    selected.forEach((id) => { if (!entries.some((p) => p.id === id)) selected.delete(id); });
    el.table.insertAdjacentHTML('beforeend', entries.map((p) => `
      <div class="ds-table-row" role="row" data-project-id="${esc(p.id)}" data-selected="${selected.has(p.id)}"${p.id === currentEntry ? ' data-current="true"' : ''}>
        <div class="ds-table-cell" data-type="checkbox-cell" data-select role="checkbox" tabindex="0" aria-checked="${selected.has(p.id)}" aria-label="Select ${esc(entryName(p))}">
          <span class="ds-table-cell__icon" data-icon="checkbox" data-checkbox-state="unchecked" aria-hidden="true"></span>
          <span class="ds-table-cell__icon" data-icon="checkbox-selected" data-checkbox-state="checked" aria-hidden="true"></span>
        </div>
        <div class="ds-table-cell" data-type="checkbox-cell"><span class="ds-table-cell__icon" data-icon="drag" role="img" aria-label="Reorder"></span></div>
        <div class="ds-table-cell" data-type="text-active-cell" role="cell"><span class="ds-table-cell__text">${esc(entryName(p))}</span></div>
        <div class="ds-table-cell" data-type="text-passive-cell" role="cell"><span class="ds-table-cell__text">${esc(describe(p))}</span></div>
        <div class="ds-table-cell" data-type="2-actions" role="cell">
          <button type="button" class="ds-text-button" data-priority="secondary" data-status="active" data-underline="dotted" data-action="edit-project" data-popup-open="portfolio-add-project-popup"><span class="ds-text-button__text">Edit</span></button>
          <button type="button" class="ds-text-button" data-priority="secondary" data-status="active" data-underline="dotted" data-action="delete-project" data-popup-open="portfolio-confirm-popup"><span class="ds-text-button__text">Delete</span></button>
        </div>
      </div>`).join(''));
    // The last row adds a project (dsgnrfhmn, 2026-10-20: Add Project lives in the table,
    // as one of its rows). Not a .ds-table-row (see .portfolio-add-row), so the removal above leaves it alone.
    el.table.insertAdjacentHTML('beforeend', `
      <div class="portfolio-add-row" role="row" data-add-row data-action-row>
        <div class="ds-table-cell" data-type="checkbox-cell" role="cell"></div>
        <div class="ds-table-cell" data-type="text-active-cell" role="cell">
          <button type="button" class="ds-text-button" data-priority="secondary" data-status="active" data-underline="dotted" data-action="add-project" data-popup-open="portfolio-add-project-popup"><span class="ds-text-button__icon" data-icon="add" aria-hidden="true"></span><span class="ds-text-button__label">Add Project</span></button>
        </div>
      </div>`);
    ArtaBoot.icons(el.table); // the drag handle's icon is injected, not CSS
    el.count.textContent = `${entries.length} total`;
    showSelection();
  }

  // ---- Ticking projects for bulk actions (dsgnrfhmn, 2026-10-25) ----
  // The table's checkboxes look and behave like dis-system table-row's (data-selected on
  // the row, select-all in the header, Shift+click ticks a range, Space / Enter on the
  // focused box), but are wired here by delegation: the table is redrawn after every
  // change, and table-row's component.js only wires the rows present when it runs. The
  // ticked ids outlive a redraw; renderTable drops those no longer in the open set (entry
  // ids are unique, so another set's never match).
  const selected = new Set();
  let selectAnchor = null;     // the last row ticked by hand, the start of a Shift range

  function showSelection() {
    const rows = Array.from(el.table.querySelectorAll(':scope > .ds-table-row[data-project-id]'));
    rows.forEach((row) => {
      const on = selected.has(row.dataset.projectId);
      row.setAttribute('data-selected', String(on));
      const box = row.querySelector('[data-select]');
      if (box) box.setAttribute('aria-checked', String(on));
    });
    const all = el.table.querySelector('[data-select-all]');
    const n = rows.filter((row) => selected.has(row.dataset.projectId)).length;
    const state = rows.length && n === rows.length ? 'checked' : n ? 'indeterminate' : 'unchecked';
    all.dataset.state = state;
    all.setAttribute('aria-checked', state === 'indeterminate' ? 'mixed' : String(state === 'checked'));
    // arta.js turns the gated bulk buttons on and off on this event.
    el.table.dispatchEvent(new CustomEvent('ds-table-row-select', { bubbles: true, detail: { count: n } }));
  }

  function toggleRow(row, range) {
    const id = row.dataset.projectId;
    const on = !selected.has(id);
    const rows = Array.from(el.table.querySelectorAll(':scope > .ds-table-row[data-project-id]'));
    const from = range && selectAnchor ? rows.findIndex((r) => r.dataset.projectId === selectAnchor) : -1;
    if (from !== -1) {
      const to = rows.indexOf(row);
      rows.slice(Math.min(from, to), Math.max(from, to) + 1)
        .forEach((r) => (on ? selected.add(r.dataset.projectId) : selected.delete(r.dataset.projectId)));
    } else if (on) {
      selected.add(id);
    } else {
      selected.delete(id);
    }
    selectAnchor = id;
    showSelection();
  }

  function toggleAll() {
    const rows = Array.from(el.table.querySelectorAll(':scope > .ds-table-row[data-project-id]'));
    const every = rows.length && rows.every((r) => selected.has(r.dataset.projectId));
    rows.forEach((r) => (every ? selected.delete(r.dataset.projectId) : selected.add(r.dataset.projectId)));
    showSelection();
  }

  el.table.addEventListener('click', (event) => {
    const box = event.target.closest('[data-select]');
    if (box) toggleRow(box.closest('.ds-table-row'), event.shiftKey);
    else if (event.target.closest('[data-select-all]')) toggleAll();
  });
  el.table.addEventListener('mousedown', (event) => {
    if (event.shiftKey && event.target.closest('[data-select]')) event.preventDefault();   // no text selection
  });
  el.table.addEventListener('keydown', (event) => {
    if (event.key !== ' ' && event.key !== 'Enter') return;
    const box = event.target.closest('[data-select], [data-select-all]');
    if (!box) return;
    event.preventDefault();
    if (box.hasAttribute('data-select-all')) toggleAll();
    else toggleRow(box.closest('.ds-table-row'), event.shiftKey);
  });

  // Bulk Delete: the ticked projects out of this set, after a confirmation, as one step
  // that one Undo brings back.
  document.querySelector('[data-actions-for="portfolio-table"] [data-action="bulk-delete"]').addEventListener('click', () => {
    const ids = active ? active.entries.filter((e) => selected.has(e.id)).map((e) => e.id) : [];
    if (!ids.length) return;
    const what = ids.length === 1 ? `"${entryName(active.entries.find((e) => e.id === ids[0]))}"` : `${ids.length} projects`;
    askConfirm(ids.length === 1 ? 'Delete project' : 'Delete projects', `Remove ${what} from this portfolio set?`, 'Delete', async () => {
      const setId = active.id;
      if (await apply(() => setsApi.deleteEntries(setId, ids), 'Could not remove the projects')) {
        ids.forEach((id) => selected.delete(id));
        showSelection();
      }
    });
    document.getElementById('portfolio-confirm-trigger').click();
  });

  // The page count is the server's estimate (pages_origin "mock") until the
  // phase-two pagination engine exists. Which template each sheet uses, and what is
  // on it, is the server's too (layout/sheets.py, after the artist's own portfolio
  // PDF); this only draws it with dis-system's portfolio-sheet, so the PDF can
  // print the same.
  // Any picture of an entry can be dragged onto another picture of the same entry, on
  // any of its sheets, to swap the two (dsgnrfhmn, 2026-10-24): a grid cell with the
  // opening's picture, a full page with a cell, and so on. Works trade with works,
  // exhibition views with exhibition views. Not in an archived set.
  // Only the pictures in view are loaded (dsgnrfhmn, 2026-10-25): a full set has hundreds,
  // and loading and decoding them all at once took the tab to about 700 MB. A picture
  // on its way shows dis-system skeleton's grey box (ds-skeleton-image). The PDF
  // export needs every picture, as drawn, with nothing fading: neither there.
  const PICTURE = ArtaPrint.active ? 'ds-portfolio-sheet__image' : 'ds-portfolio-sheet__image ds-skeleton-image';
  const LAZY = ArtaPrint.active ? '' : ' loading="lazy" decoding="async"';
  const sheetImage = (sheet, image, cell) => {
    const entry = active && !active.archived && sheet.entry_id ? active.entries.find((e) => e.id === sheet.entry_id) : null;
    const list = entry ? (entry.works.includes(image.id) ? 'works' : entry.exhibitions.includes(image.id) ? 'exhibitions' : null) : null;
    // data-role: portfolio-sheet draws a work whole and lets an exhibition view fill its cell.
    return `<img class="${PICTURE}" data-role="${image.role === 'exhibition' ? 'exhibition' : 'work'}"${cell === undefined ? '' : ` data-cell="${cell}"`}${
      list ? ` draggable="true" data-entry-id="${esc(sheet.entry_id)}" data-image-id="${esc(image.id)}" data-list="${list}"` : ''
    } src="${esc(ArtaApi.projects.thumbUrl(sheet.project_id, image.id))}" alt=""${LAZY}>`;
  };
  const captionLines = (image) => {
    const c = image.caption;
    return `<p>${esc(c.prefix)}<b>${esc(c.label)}</b>${esc(c.suffix)}</p>${c.lines.map((l) => `<p>${esc(l)}</p>`).join('')}`;
  };
  // The layouts a sheet can be switched to from its menu (dsgnrfhmn, 2026-10-24); the first
  // sheet of a project keeps its title and text, the others are pictures only.
  const OPENING_LAYOUTS = [
    { name: 'grid-side', label: 'Text and four pictures' },
    { name: 'side', label: 'Text and a tall picture' },
    { name: 'figure-side', label: 'Text and a wide picture' },
    { name: 'band', label: 'Title and a picture across' },
    { name: 'auto', label: 'Automatic' },
  ];
  const WORK_LAYOUTS = [
    { name: 'full', label: 'One picture, the whole page' },
    { name: 'figure', label: 'One picture, centred' },
    { name: 'pair', label: 'Two side by side' },
    { name: 'triple', label: 'Three in a row' },
    { name: 'grid', label: 'Four in a grid' },
    { name: 'auto', label: 'Automatic' },
  ];
  const ROW_LAYOUTS = new Set(['pair', 'triple']);

  // Templates whose caption sits over the picture (white, or black over a bright one).
  const CAPTION_ON_IMAGE = new Set(['band', 'side', 'full']);
  const COLUMN = new Set(['band', 'side', 'figure-side', 'grid-side']);
  // Texts on a sheet can be edited in place in a set that is not archived (dsgnrfhmn,
  // 2026-10-24); data-editable names which one, dis-system's portfolio-sheet draws it.
  const editableSet = () => !!active && !active.archived;
  const editAttr = (field, imageId) => (editableSet()
    ? ` data-editable="${field}"${imageId ? ` data-image-id="${esc(imageId)}"` : ''}` : '');
  const paragraphsHtml = (sheet) =>
    `<div class="ds-portfolio-sheet__text"${editAttr('text')}>${sheet.paragraphs.map((p) => `<p>${esc(p)}</p>`).join('')}</div>`;

  function sheetHtml(sheet, entries) {
    // The cover (page 1 of the portfolio PDF, dsgnrfhmn 2026-10-24) is a picture of a whole
    // page, same 842x599 as the sheet: drawn full bleed, no caption.
    if (sheet.layout === 'cover' && sheet.cover && sheet.texts) {
      // Rebuilt as text around the PDF's own photo (dsgnrfhmn, 2026-10-24).
      // Its texts are editable in place too, in this set only (dsgnrfhmn, 2026-10-24).
      const t = sheet.texts;
      const lines = (list) => list.map((line) => `<p>${esc(line)}</p>`).join('');
      const cover = (field) => editAttr(`cover-${field}`);
      return `<div class="ds-portfolio-sheet" data-layout="cover"><div class="ds-portfolio-sheet__column">`
        + `<p class="ds-portfolio-sheet__cover-title"${cover('title')}>${esc(t.title)}</p><p class="ds-portfolio-sheet__cover-name"${cover('name')}>${esc(t.name)}</p>`
        + `<div class="ds-portfolio-sheet__cover-details"${cover('details')}>${lines(t.details)}</div>`
        + `<p class="ds-portfolio-sheet__cover-heading"${cover('heading')}>${esc(t.heading)}</p>`
        + `<div class="ds-portfolio-sheet__text"${cover('paragraphs')}>${lines(t.paragraphs)}</div></div>`
        + (sheet.photo ? `<img class="${PICTURE}" src="${esc(ArtaApi.projects.coverPhotoUrl(sheet.cover))}" alt="">` : '')
        + `<div class="ds-portfolio-sheet__cover-contacts"${cover('contacts')}>${lines(t.contacts)}</div></div>`;
    }
    if (sheet.layout === 'cover') {
      return sheet.cover
        ? `<div class="ds-portfolio-sheet" data-layout="full"><img class="${PICTURE}" src="${esc(ArtaApi.projects.coverUrl(sheet.cover))}" alt="Cover"></div>`
        : `<div class="ds-portfolio-sheet" data-layout="missing"><p class="ds-portfolio-sheet__note">Cover: ${esc(sheet.note || 'it could not be read.')}</p></div>`;
    }
    const parts = [];
    if (COLUMN.has(sheet.layout)) {
      parts.push(`<div class="ds-portfolio-sheet__column"><h3 class="ds-portfolio-sheet__title"${editAttr('title')}>${esc(sheet.title)}</h3>${
        sheet.subtitle ? `<p class="ds-portfolio-sheet__subtitle"${editAttr('subtitle')}>${esc(sheet.subtitle)}</p>` : ''}${
        sheet.paragraphs.length ? paragraphsHtml(sheet) : ''}</div>`);
    } else if (sheet.paragraphs.length) {
      parts.push(paragraphsHtml(sheet));
    }
    if (sheet.layout === 'missing') {
      const entry = entries.get(sheet.entry_id);
      parts.push(`<p class="ds-portfolio-sheet__note">${esc(entry ? entryName(entry) : 'Project')}: this project could not be read.</p>`);
    }
    if (sheet.slots) {
      // A grid: a picture or an empty box per cell. grid-side has no captions (as in
      // the PDF); grid stacks one per filled cell on the left.
      parts.push(...sheet.slots.map((slot) => (slot.image
        ? sheetImage(sheet, slot.image, slot.cell)
        : `<div class="ds-portfolio-sheet__slot" data-cell="${slot.cell}"></div>`)));
      if (sheet.layout === 'grid') {
        parts.push(`<div class="ds-portfolio-sheet__caption">${sheet.slots.filter((s) => s.image)
          .map((s) => `<div class="ds-portfolio-sheet__caption-item"${editAttr('caption', s.image.id)}>${captionLines(s.image)}</div>`).join('')}</div>`);
      } else if (ROW_LAYOUTS.has(sheet.layout)) {
        parts.push(...sheet.slots.filter((s) => s.image)
          .map((s) => `<div class="ds-portfolio-sheet__caption" data-cell="${s.cell}"${editAttr('caption', s.image.id)}>${captionLines(s.image)}</div>`));
      }
    } else if (sheet.images.length) {
      parts.push(sheetImage(sheet, sheet.images[0]));
      parts.push(`<div class="ds-portfolio-sheet__caption"${CAPTION_ON_IMAGE.has(sheet.layout) ? ' data-on="image"' : ''}${editAttr('caption', sheet.images[0].id)}>${captionLines(sheet.images[0])}</div>`);
    }
    // A sheet of works or of exhibition views gets the layout menu at its right middle
    // (not in an archived set).
    const editable = active && !active.archived;
    const worksSheet = editable && typeof sheet.plan_index === 'number';
    const viewsSheet = editable && typeof sheet.exhibition_index === 'number';
    // Its last button deletes the page from this set (dsgnrfhmn, 2026-10-24).
    const remove = { remove: 'Delete this page' };
    if (worksSheet) parts.push(DsLayoutPicker.html(sheet.plan_index === 0 ? OPENING_LAYOUTS : WORK_LAYOUTS, sheet.layout, remove));
    if (viewsSheet) parts.push(DsLayoutPicker.html(WORK_LAYOUTS, sheet.layout, remove));
    const address = `${sheet.entry_id ? ` data-entry-id="${esc(sheet.entry_id)}"` : ''}${
      worksSheet ? ` data-plan-index="${sheet.plan_index}"` : ''}${viewsSheet ? ` data-exhibition-index="${sheet.exhibition_index}"` : ''}`;
    return `<div class="ds-portfolio-sheet" data-layout="${esc(sheet.layout)}"${address}>${parts.join('')}</div>`;
  }

  let previewToken = 0;
  let previewDrawn = Promise.resolve(); // the latest renderPreview(), awaited by the first load
  let shownPages = [];                  // the sheets drawn last, read by the layout menu

  async function renderPreview() {
    const token = ++previewToken;
    const n = active ? active.pages : 0;
    el.previewCount.textContent = `${n} ${n === 1 ? 'Page' : 'Pages'}`;
    el.previewEmpty.hidden = n > 0;
    el.previewWrapper.hidden = n === 0;
    if (n === 0) {
      el.previewPages.innerHTML = '';
      return;
    }
    // Another set's sheets take a moment (and their pictures): a spinner at the
    // preview's middle meanwhile (dsgnrfhmn, 2026-10-25). An edit of the open set redraws
    // in place without one.
    const switching = previewSetId !== active.id;
    previewSetId = active.id;
    const done = switching ? ArtaBoot.busy(el.preview, 'Loading the preview') : () => {};
    let pages;
    try {
      pages = (await setsApi.preview(active.id)).pages;
    } catch (error) {
      done();
      if (token === previewToken) toastError(`Could not draw the preview: ${error.message}`);
      return;
    }
    if (token !== previewToken) { done(); return; } // a newer change was drawn meanwhile
    const entries = new Map(active.entries.map((e) => [e.id, e]));
    shownPages = pages;
    el.previewPages.innerHTML = pages.map((sheet) => sheetHtml(sheet, entries)).join('');
    // Re-measure paper-preview's custom scroll thumb without showing it.
    window.dispatchEvent(new Event('resize'));
    spyCurrent();
    ArtaBoot.whenDrawn(el.previewPages).then(done);
  }
  let previewSetId = null;   // the set whose sheets the preview shows

  // ---- The project under the reader's eye (dsgnrfhmn, 2026-10-25) ----
  // While the preview scrolls, the project whose sheet crosses a line a third of the way
  // down the preview is the current one: its row in Included projects is marked
  // (dis-system table-row's data-current: it looks hovered, Edit and Delete up) and
  // scrolled into the table's view, so it can be edited from there at once. The cover
  // sheet belongs to no project: then no row is marked.
  let currentEntry = null;
  function spyCurrent() {
    const pane = el.previewPages;
    const line = pane.getBoundingClientRect().top + pane.clientHeight / 3;
    let id = null;
    for (const sheet of pane.querySelectorAll('.ds-portfolio-sheet')) {
      if (sheet.getBoundingClientRect().bottom > line) {
        id = sheet.dataset.entryId || null;
        break;
      }
    }
    if (id === currentEntry) return;
    currentEntry = id;
    el.table.querySelectorAll(':scope > .ds-table-row[data-current]').forEach((row) => row.removeAttribute('data-current'));
    const row = id && el.table.querySelector(`:scope > .ds-table-row[data-project-id="${CSS.escape(id)}"]`);
    if (row) {
      row.setAttribute('data-current', 'true');
      // The row is display: contents (no box of its own): its first cell is scrolled to.
      const cell = row.firstElementChild;
      if (cell) cell.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  }
  let spyFrame = 0;
  el.previewPages.addEventListener('scroll', () => {
    if (spyFrame) return;
    spyFrame = requestAnimationFrame(() => { spyFrame = 0; spyCurrent(); });
  }, { passive: true });

  // ---- Editing a text on a sheet in place (dsgnrfhmn, 2026-10-24) ----
  // A click on a title, year, text or caption makes it editable. The project text is
  // edited whole (all its paragraphs, also where a sheet shows part of it). Enter saves
  // a title or a year; Ctrl+Enter or leaving the field saves any; Esc gives up. The edit
  // is this set's own (PUT .../texts): Drive and the other sets keep their text, and an
  // emptied field goes back to the project's own text. The cover's texts (data-editable
  // cover-<field>) go the same way to the set's own cover (PUT .../cover); its lists are
  // edited as lines, the bio as paragraphs apart by a blank line.
  let textEdit = null;   // { node, field, entryId, imageId, original }
  const COVER_LINES = new Set(['cover-details', 'cover-contacts']);
  const SINGLE_LINE = new Set(['title', 'subtitle', 'cover-title', 'cover-name', 'cover-heading']);
  const fullText = (entryId) => (shownPages.find((s) => s.entry_id === entryId && s.entry_text !== undefined) || {}).entry_text || '';

  function startTextEdit(node) {
    const field = node.dataset.editable;
    const entryId = node.closest('.ds-portfolio-sheet').dataset.entryId;
    let original = node.innerText.trim();
    if (COVER_LINES.has(field)) original = original.split(/\n+/).join('\n');
    if (field === 'cover-paragraphs') original = original.split(/\n\s*\n/).join('\n\n');
    if (field === 'text') {
      original = fullText(entryId).trim();
      node.innerHTML = original.split(/\n{2,}/).map((p) => `<p>${esc(p)}</p>`).join('');
    }
    textEdit = { node, field, entryId, imageId: node.dataset.imageId, original };
    node.contentEditable = 'true';
    node.focus();
    const range = document.createRange();
    range.selectNodeContents(node);
    range.collapse(false);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
  }

  async function endTextEdit(save) {
    const edit = textEdit;
    if (!edit) return;
    textEdit = null;
    edit.node.contentEditable = 'false';
    let value = edit.node.innerText.replace(/\u00a0/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
    if (COVER_LINES.has(edit.field)) value = value.split(/\n+/).join('\n');
    if (!save || value === edit.original) {
      renderPreview();
      return;
    }
    if (edit.field.startsWith('cover-')) {
      const field = edit.field.slice('cover-'.length);
      const saved = await apply(() => setsApi.setCoverTexts(active.id, { [field]: value }), 'Could not save the text');
      if (saved && !value) window.dsToast.show({ message: 'Back to the cover\'s own text.' });
      if (!saved) renderPreview();
      return;
    }
    const body = edit.field === 'caption' ? { captions: { [edit.imageId]: value } } : { [edit.field]: value };
    const done = await apply(() => setsApi.setTexts(active.id, edit.entryId, body), 'Could not save the text');
    if (done && !value) window.dsToast.show({ message: 'Back to the project\'s own text.' });
    if (!done) renderPreview();
  }

  el.previewPages.addEventListener('click', (event) => {
    const node = event.target instanceof Element ? event.target.closest('[data-editable]') : null;
    if (!node || node.isContentEditable || !editableSet() || String(window.getSelection() || '')) return;
    if (textEdit) return;
    startTextEdit(node);
  });
  el.previewPages.addEventListener('keydown', (event) => {
    if (!textEdit || event.target !== textEdit.node) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      endTextEdit(false);
    } else if (event.key === 'Enter' && (event.ctrlKey || event.metaKey || SINGLE_LINE.has(textEdit.field))) {
      event.preventDefault();
      endTextEdit(true);
    }
  });
  el.previewPages.addEventListener('focusout', (event) => {
    if (textEdit && event.target === textEdit.node) endTextEdit(true);
  });
  // Pasted text arrives as plain text: no formatting, no markup from elsewhere.
  el.previewPages.addEventListener('paste', (event) => {
    if (!textEdit || event.target.closest('[data-editable]') !== textEdit.node) return;
    event.preventDefault();
    document.execCommand('insertText', false, event.clipboardData.getData('text/plain'));
  });

  // ---- How the sheets are shown (dsgnrfhmn, 2026-10-24) ----
  // dis-system's zoom bar, the same as CV's: fit, edge to edge of the pane (the default
  // and the largest view), or a smaller zoom of the sheet's own 842 px on the page colour.
  // Kept for this page view only.
  DsZoomButtons.bar(el.zoomBar, {
    keep: { scroller: el.previewPages, items: '.ds-portfolio-sheet' },   // a zoom keeps the place
    // Zoomed out, the sheets' layout menu hangs beside them (data-sheet-tools, dsgnrfhmn
    // 2026-10-24), so a step leaves room for it and its gap on each side of the sheet.
    fitted: () => {
      const picker = el.previewPages.querySelector('.ds-layout-picker');
      const gap = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ds-spacing-6')) || 0;
      return DsZoomButtons.fitOnPaper(el.previewPages, 842, picker ? picker.offsetWidth + gap : 0);
    },
    apply: (zoom) => {
      if (zoom === null) {
        delete el.preview.dataset.zoom;
        delete el.preview.dataset.sheetTools;
        el.preview.style.removeProperty('--portfolio-zoom');
      } else {
        el.preview.dataset.zoom = '';
        el.preview.dataset.sheetTools = 'outside';
        el.preview.style.setProperty('--portfolio-zoom', String(zoom));
      }
      window.dispatchEvent(new Event('resize'));   // paper-preview re-measures its scroll thumb
    },
  });

  // ---- A sheet's layout menu (dsgnrfhmn, 2026-10-24) ----
  // The entry's plan becomes the layouts of its sheets before this one, as they are
  // drawn now, then the chosen one; the sheets after it go back to the automatic
  // choice. "Automatic" keeps only the sheets before. Kept in this set only.
  el.previewPages.addEventListener('click', async (event) => {
    const option = event.target instanceof Element ? event.target.closest('[data-layout-option]') : null;
    if (!option || !active) return;
    const sheetEl = option.closest('.ds-portfolio-sheet[data-plan-index], .ds-portfolio-sheet[data-exhibition-index]');
    const entryId = sheetEl.dataset.entryId;
    const part = sheetEl.dataset.exhibitionIndex !== undefined ? 'exhibitions' : 'works';
    const key = part === 'works' ? 'plan_index' : 'exhibition_index';
    const index = Number(part === 'works' ? sheetEl.dataset.planIndex : sheetEl.dataset.exhibitionIndex);
    const before = shownPages
      .filter((s) => s.entry_id === entryId && typeof s[key] === 'number' && s[key] < index)
      .sort((a, b) => a[key] - b[key])
      .map((s) => s.layout);
    const choice = option.dataset.layoutOption;
    const plan = choice === 'auto' ? before : [...before, choice];
    await apply(() => setsApi.setLayout(active.id, entryId, plan, part), 'Could not change the page layout');
  });

  // ---- Deleting a page from the set (dsgnrfhmn, 2026-10-24) ----
  // The trash button of a page's layout menu asks first, then deletes the page in this
  // set only: its pictures, and on a project's first page its text. Undo brings it back.
  el.previewPages.addEventListener('click', (event) => {
    const button = event.target instanceof Element ? event.target.closest('[data-layout-remove]') : null;
    if (!button || !active) return;
    const sheetEl = button.closest('.ds-portfolio-sheet[data-plan-index], .ds-portfolio-sheet[data-exhibition-index]');
    const entryId = sheetEl.dataset.entryId;
    const part = sheetEl.dataset.exhibitionIndex !== undefined ? 'exhibitions' : 'works';
    const index = Number(part === 'works' ? sheetEl.dataset.planIndex : sheetEl.dataset.exhibitionIndex);
    const entry = active.entries.find((e) => e.id === entryId);
    const setId = active.id;
    askConfirm('Delete page', `Delete this page of "${entry ? entryName(entry) : 'the project'}" from this portfolio set? `
      + 'Its pictures and text leave this set only; Undo brings the page back.', 'Delete', async () => {
      await apply(() => setsApi.deleteSheet(setId, entryId, part, index), 'Could not delete the page');
    });
    document.getElementById('portfolio-confirm-trigger').click();
  });

  // ---- Swapping two pictures of an entry by drag and drop (dsgnrfhmn, 2026-10-24) ----
  // Sheets are filled with the entry's works in pick order (layout/sheets.py), so two
  // pictures trade places, also across sheets, when they trade places in that order. The swap is
  // an ordinary entry edit: validated by the server, one undo step, preview redrawn.
  let dragged = null;
  const swapTarget = (node) => {
    const img = node instanceof Element ? node.closest('img[draggable="true"][data-image-id]') : null;
    return img && dragged && img !== dragged && img.dataset.entryId === dragged.dataset.entryId
      && img.dataset.list === dragged.dataset.list ? img : null;
  };
  const clearDrop = () => el.previewPages.querySelectorAll('[data-drop]').forEach((img) => img.removeAttribute('data-drop'));

  el.previewPages.addEventListener('dragstart', (event) => {
    const img = event.target instanceof Element ? event.target.closest('img[draggable="true"][data-image-id]') : null;
    if (!img) return;
    dragged = img;
    img.dataset.dragging = 'true';
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', img.dataset.imageId);
  });
  el.previewPages.addEventListener('dragover', (event) => {
    const target = swapTarget(event.target);
    if (!target) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    if (target.dataset.drop !== 'true') {
      clearDrop();
      target.dataset.drop = 'true';
    }
  });
  el.previewPages.addEventListener('dragleave', (event) => {
    const target = swapTarget(event.target);
    if (target && !(event.relatedTarget instanceof Node && target.contains(event.relatedTarget))) target.removeAttribute('data-drop');
  });
  el.previewPages.addEventListener('dragend', () => {
    if (dragged) dragged.removeAttribute('data-dragging');
    dragged = null;
    clearDrop();
  });
  el.previewPages.addEventListener('drop', async (event) => {
    const target = swapTarget(event.target);
    if (!target) return;
    event.preventDefault();
    const from = dragged.dataset.imageId;
    const to = target.dataset.imageId;
    const entry = active.entries.find((e) => e.id === target.dataset.entryId);
    clearDrop();
    if (!entry) return;
    const swap = (ids) => ids.map((id) => (id === from ? to : id === to ? from : id));
    const inWorks = target.dataset.list === 'works';
    await apply(() => setsApi.editEntry(active.id, entry.id, {
      project_id: entry.project_id,
      works: inWorks ? swap(entry.works) : entry.works,
      exhibitions: inWorks ? entry.exhibitions : swap(entry.exhibitions),
      text_amount: entry.text_amount,
    }), 'Could not swap the pictures');
  });

  function renderContent() {
    el.title.textContent = active ? active.name : '';
    ArtaRecent.touch('portfolio', active);
    // Click the name to rename it (SS5.4.1); an archived set is read-only.
    const renamable = !!active && !active.archived;
    el.title.toggleAttribute('data-editable', renamable);
    if (renamable) el.title.tabIndex = 0;
    else el.title.removeAttribute('tabindex');
    el.saveAs.disabled = !active;
    el.saveAs.dataset.status = active ? 'active' : 'disabled';
    renderTable();
    previewDrawn = renderPreview();
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

  // A page with no set yet makes one when the first project is added.
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
        // The spinner covers reading the set too, not only its sheets.
        const done = ArtaBoot.busy(el.preview, 'Loading the preview');
        try {
          await openSet(id);
          await previewDrawn;
        } finally {
          done();
        }
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

  // ---- Shared anchored confirmation (SS5.6.3), used by Delete project ----
  const confirmPopup = {
    title: document.getElementById('portfolio-confirm-title'),
    body: document.getElementById('portfolio-confirm-body'),
    okBtn: document.getElementById('portfolio-confirm-ok'),
  };
  let confirmCallback = null;
  function askConfirm(title, body, okLabel, onConfirm) {
    confirmPopup.title.textContent = title;
    confirmPopup.body.textContent = body;
    confirmPopup.okBtn.textContent = okLabel;
    confirmCallback = onConfirm;
  }
  confirmPopup.okBtn.addEventListener('click', () => {
    if (confirmCallback) confirmCallback();
    confirmCallback = null;
  });

  // ---- Project rows: delete, edit ----
  el.table.addEventListener('click', (event) => {
    const row = event.target.closest('.ds-table-row');
    if (!row || !active) return;
    const entry = active.entries.find((p) => p.id === row.dataset.projectId);
    if (!entry) return;
    if (event.target.closest('[data-action="delete-project"]')) {
      askConfirm('Delete project', `Remove "${entryName(entry)}" from this portfolio set?`, 'Delete', async () => {
        await apply(() => setsApi.deleteEntry(active.id, entry.id), 'Could not remove the project');
      });
    } else if (event.target.closest('[data-action="edit-project"]')) {
      prepareProjectPopup(entry);
    } else if (!event.target.closest('button, [data-type="checkbox-cell"]')) {
      scrollToEntry(entry.id);
    }
  });

  // A click on a project's row scrolls the preview to that project's first sheet
  // (dsgnrfhmn, 2026-10-24), animated here rather than by the browser's smooth scrolling,
  // which a browser setting can turn off: 450 ms ease-in-out, the page's fade length.
  // At once under reduced motion. A new click takes over from a running scroll.
  let scrollFrame = 0;
  function scrollToEntry(entryId) {
    const pane = el.previewPages;
    const sheet = pane.querySelector(`.ds-portfolio-sheet[data-entry-id="${CSS.escape(entryId)}"]`);
    if (!sheet) return;
    const pad = parseFloat(getComputedStyle(pane).paddingTop) || 0;
    const max = pane.scrollHeight - pane.clientHeight;
    const to = Math.max(0, Math.min(max, sheet.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop - pad));
    cancelAnimationFrame(scrollFrame);
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      pane.scrollTop = to;
      return;
    }
    const from = pane.scrollTop;
    const start = performance.now();
    const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - ((-2 * t + 2) ** 2) / 2);
    const step = (now) => {
      const t = Math.min(1, (now - start) / 450);
      pane.scrollTop = from + (to - from) * ease(t);
      if (t < 1) scrollFrame = requestAnimationFrame(step);
    };
    scrollFrame = requestAnimationFrame(step);
  }
  // Add project reads the projects anew from Drive (dsgnrfhmn, 2026-10-22: only Add project
  // and creating a set, never on a timer; New Full Set does it on the server). The popup opens at once with the last list; the
  // list is redrawn when the new one arrives. On failure the last list stays, and the
  // toast says so (the message carries the date of that list).
  let projectsRefresh = null;
  function refreshProjects() {
    if (projectsRefresh) return projectsRefresh;
    projectsRefresh = ArtaApi.projects.refresh()
      .then(({ items }) => {
        document.querySelector('#ap-dropdown .ds-dropdown-menu').innerHTML = items
          .map((p) => `<button type="button" class="ds-dropdown-menu__item" role="option" data-value="${esc(p.id)}" aria-selected="${p.id === ap.projectId}">${esc(p.name)}</button>`)
          .join('');
      })
      .catch((error) => toastError(`Could not update the projects from Drive: ${error.message}`))
      .finally(() => { projectsRefresh = null; });
    return projectsRefresh;
  }

  // The Add Project row is re-rendered with the table, so it is found by delegation.
  // The whole Add Project row is its button (dsgnrfhmn, 2026-10-24).
  el.table.addEventListener('click', (event) => {
    const row = event.target.closest('[data-add-row]');
    if (row && !event.target.closest('button')) row.querySelector('[data-action="add-project"]').click();
  });
  el.table.addEventListener('click', (event) => {
    if (event.target.closest('[data-action="add-project"]')) {
      prepareProjectPopup(null);
      refreshProjects();
    }
  });
  // The create rows are re-rendered with the list, so they are found by delegation.
  // New Empty Set and New Full Set are each made on the server in one step, then
  // opened. A second click while one is on its way does nothing.
  let creating = false;
  el.list.addEventListener('click', async (event) => {
    const full = !!event.target.closest('#portfolio-create-full-set, [data-create-full]');
    if ((!full && !event.target.closest('#portfolio-create-set, [data-create-empty]')) || creating) return;
    creating = true;
    // The row's plus turns into the loading glyph until the set is made (dsgnrfhmn,
    // 2026-10-25; dis-system sidebar-list's busy create row). The sidebar is drawn
    // anew when it is done; on a failure the plus comes back here.
    const row = event.target.closest('.ds-sidebar-item[data-role="create"]');
    const icon = row && row.querySelector('.ds-sidebar-item__save-icon');
    if (row && icon) {
      row.setAttribute('data-busy', '');
      icon.dataset.icon = 'loading';
      ArtaBoot.icons(row);
    }
    try {
      active = await (full ? setsApi.createFull() : setsApi.create());
      await refreshSidebar();
      renderAll();
    } catch (error) {
      toastError(`Could not create the ${full ? 'full ' : ''}portfolio set: ${error.message}`);
      if (row && icon && row.isConnected) {
        row.removeAttribute('data-busy');
        icon.dataset.icon = 'add';
        ArtaBoot.icons(row);
      }
    } finally {
      creating = false;
    }
  });

  // ---- Add / edit project popup (SS5.6.1) ----
  // Runs before popup.js opens the panel (its opener listens on document,
  // this on the button), so the panel always opens already prepared. The
  // dropdown items are the projects the API lists, rendered before the
  // components ran (see the boot script above). Pictures are cached
  // thumbnails; an original is never loaded here.
  const apEl = {
    overlay: document.getElementById('portfolio-add-project-popup'),
    title: document.getElementById('portfolio-add-project-title'),
    dropdown: document.getElementById('ap-dropdown'),
    trigger: document.querySelector('#ap-dropdown [data-dropdown-trigger]'),
    value: document.querySelector('#ap-dropdown [data-dropdown-value]'),
    // Live: the list is re-rendered when the projects are read anew from Drive.
    get items() { return Array.from(document.querySelectorAll('#ap-dropdown .ds-dropdown-menu__item')); },
    works: document.getElementById('ap-works'),
    exhibitions: document.getElementById('ap-exhibitions'),
    radios: Array.from(document.querySelectorAll('#ap-text .ds-radio')),
    save: document.getElementById('portfolio-add-project-save'),
    resets: { works: document.querySelector('[data-ap-reset="works"]'), exhibitions: document.querySelector('[data-ap-reset="exhibitions"]') },
  };
  const ap = { project: null, projectId: null, works: [], exhibitions: [], text: 'short', editingId: null, token: 0, initial: '', discarding: false };
  // What the form holds, to tell whether closing it would lose a choice.
  const apSnapshot = () => JSON.stringify([ap.projectId, ap.works, ap.exhibitions, ap.text]);
  const kindLabel = { works: 'Work', exhibitions: 'Exhibition' };
  const imagesOf = (kind) => (ap.project ? ap.project.images.filter((i) => (i.role === 'exhibition') === (kind === 'exhibitions')) : []);

  function renderPicker(kind, focusId) {
    const host = apEl[kind];
    const order = ap[kind];
    if (!ap.project) {
      host.innerHTML = '<div class="ds-empty-state" data-size="block"><span class="ds-empty-state__message">Add a project to see the preview</span></div>';
    } else if (!imagesOf(kind).length) {
      host.innerHTML = '<div class="ds-empty-state" data-size="block"><span class="ds-empty-state__message">No images here</span></div>';
    } else {
      host.innerHTML = imagesOf(kind).map((image, i) => {
        const pos = order.indexOf(image.id);
        const on = pos !== -1;
        const src = ArtaApi.projects.thumbUrl(ap.project.id, image.id);
        return `<button type="button" class="ds-gallery-image" data-type="picker" data-image-id="${esc(image.id)}" aria-pressed="${on}" aria-label="${kindLabel[kind]} image ${i + 1}${on ? `, picked ${pos + 1}` : ''}"><span class="ds-gallery-image__image"><img class="ds-gallery-image__img" src="${esc(src)}" alt="" loading="lazy" decoding="async"></span>${on ? `<span class="ds-gallery-image__badge">${pos + 1}</span>` : ''}</button>`;
      }).join('');
    }
    const reset = apEl.resets[kind];
    reset.disabled = order.length === 0;
    reset.dataset.status = order.length ? 'active' : 'disabled';
    if (focusId) {
      const again = host.querySelector(`[data-image-id="${focusId}"]`);
      if (again) again.focus();
    }
  }

  // Accordion (dsgnrfhmn, 2026-10-24): the popup opens with only step 1 open. Once a project
  // is chosen, the image steps that have images of their kind open; one with none stays
  // closed and says so beside its title. Step 4 stays closed, its title saying which
  // description goes in. The arrow or a click on a head still opens or closes it by hand.
  function setSteps() {
    if (!window.dsPopupStep) return;
    document.querySelectorAll('#portfolio-add-project-popup .ds-popup-step__head[data-collapsible]').forEach((head) => {
      const note = head.querySelector('[data-ap-empty]');
      let open = false;
      if (head.dataset.apStep === 'project') {
        open = true;
      } else if (note) {
        const has = !!ap.project && imagesOf(note.dataset.apEmpty).length > 0;
        note.hidden = !ap.project || has;
        open = has;
      }
      window.dsPopupStep.setOpen(head, open);
    });
    renderTextNote();
  }

  // Step 4's note: the default, or what this entry has chosen instead.
  function renderTextNote() {
    const note = document.querySelector('[data-ap-text-note]');
    if (note) note.textContent = ap.text === 'short' ? '(Default is short description)' : `(${ap.text[0].toUpperCase()}${ap.text.slice(1)} description)`;
  }

  function renderRadios() {
    apEl.radios.forEach((r) => {
      const on = r.dataset.value === ap.text;
      r.setAttribute('aria-checked', String(on));
      r.tabIndex = on ? 0 : -1;
    });
    renderTextNote();
  }

  // Loads the project's pictures. Answers that arrive after another
  // choice was made are dropped (token).
  async function chooseProject(id, keepPicks) {
    const token = ++ap.token;
    ap.project = null;
    ap.projectId = id || null;
    if (!keepPicks) {
      ap.works = [];
      ap.exhibitions = [];
    }
    apEl.save.disabled = true;
    apEl.save.dataset.status = 'disabled';
    apEl.items.forEach((item) => item.setAttribute('aria-selected', String(!!id && item.dataset.value === id)));
    if (id) apEl.trigger.removeAttribute('data-placeholder');
    else apEl.trigger.setAttribute('data-placeholder', '');
    renderPicker('works');
    renderPicker('exhibitions');
    if (!id) {
      setSteps();
      return;
    }
    let project;
    try {
      project = await ArtaApi.projects.get(id);
    } catch (error) {
      if (token === ap.token) toastError(`Could not load the project: ${error.message}`);
      return;
    }
    if (token !== ap.token) return;
    ap.project = project;
    // Picks that are no longer in the project are dropped, not sent.
    ap.works = ap.works.filter((imageId) => imagesOf('works').some((i) => i.id === imageId));
    ap.exhibitions = ap.exhibitions.filter((imageId) => imagesOf('exhibitions').some((i) => i.id === imageId));
    apEl.save.disabled = false;
    apEl.save.dataset.status = 'active';
    renderPicker('works');
    renderPicker('exhibitions');
    setSteps();
  }

  function prepareProjectPopup(entry) {
    const setName = active ? active.name : 'a new portfolio set';
    ap.editingId = entry ? entry.id : null;
    apEl.title.textContent = entry ? `Edit project in “${setName}”`
      : `Add a project to “${setName}”`;
    // The label lives in the button's text span: the dotted underline is drawn on it.
    apEl.save.querySelector('.ds-text-button__text').textContent = entry ? 'Save Project' : 'Add Project';
    if (entry) {
      ap.works = entry.works.slice();
      ap.exhibitions = entry.exhibitions.slice();
      ap.text = entry.text_amount;
      apEl.value.textContent = entryName(entry);
      chooseProject(entry.project_missing ? null : entry.project_id, true);
    } else {
      ap.text = 'short';
      apEl.value.textContent = 'Choose the project from the list';
      chooseProject(null);
    }
    renderRadios();
    ap.initial = apSnapshot();
  }

  // Cancel and the close icon ask first when the form holds changes (same small
  // confirmation as Delete project). The popup component asks the page through
  // ds-popup-before-close; setting data-popup-open on the button during that event
  // makes the same click open the confirmation anchored to it. Discard closes for real.
  const addProjectOverlay = document.getElementById('portfolio-add-project-popup');
  addProjectOverlay.addEventListener('ds-popup-before-close', (event) => {
    const button = event.detail.button;
    const isCancel = !!button && (button.id === 'portfolio-add-project-cancel' || button.classList.contains('ds-popup__close'));
    if (!isCancel || ap.discarding || apSnapshot() === ap.initial) return;
    event.preventDefault();
    askConfirm('Discard changes', 'Close without saving? The choices you made in this form will be lost.', 'Discard', () => {
      ap.discarding = true;
      button.click();
      ap.discarding = false;
    });
    button.dataset.popupOpen = 'portfolio-confirm-popup';
    setTimeout(() => button.removeAttribute('data-popup-open'), 0);
  });

  apEl.dropdown.addEventListener('ds-dropdown-select', (event) => {
    chooseProject(event.detail.value || null);
  });

  ['works', 'exhibitions'].forEach((kind) => {
    apEl[kind].addEventListener('click', (event) => {
      const card = event.target.closest('.ds-gallery-image');
      if (!card) return;
      const id = card.dataset.imageId;
      ap[kind] = ap[kind].includes(id) ? ap[kind].filter((x) => x !== id) : [...ap[kind], id];
      renderPicker(kind, id);
    });
    apEl.resets[kind].addEventListener('click', () => {
      ap[kind] = [];
      renderPicker(kind);
    });
  });

  // Radio group: one stop for Tab, arrows move and select, as a native group does.
  apEl.radios.forEach((radio, index) => {
    radio.addEventListener('click', () => {
      ap.text = radio.dataset.value;
      renderRadios();
    });
    radio.addEventListener('keydown', (event) => {
      const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
      if (!step) return;
      event.preventDefault();
      const next = apEl.radios[(index + step + apEl.radios.length) % apEl.radios.length];
      ap.text = next.dataset.value;
      renderRadios();
      next.focus();
    });
  });

  apEl.save.addEventListener('click', async () => {
    if (!ap.project) return;
    const entry = { project_id: ap.project.id, works: ap.works.slice(), exhibitions: ap.exhibitions.slice(), text_amount: ap.text };
    const editingId = ap.editingId;
    try {
      await ensureActive();
    } catch (error) {
      toastError(`Could not create a portfolio set: ${error.message}`);
      return;
    }
    await apply(
      () => (editingId ? setsApi.editEntry(active.id, editingId, entry) : setsApi.addEntry(active.id, entry)),
      editingId ? 'Could not save the project' : 'Could not add the project'
    );
  });

  // ---- Drag to reorder (whole row: handle, name, details) ----
  ArtaBoot.sortable(el.table, {
    row: '.ds-table-row',
    id: (row) => row.dataset.projectId,
    image: (row) => row.querySelector('[data-type="text-active-cell"]'),
    onReorder: async (ids) => {
      if (active) await apply(() => setsApi.order(active.id, ids), 'Could not reorder the projects');
    },
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
  const pdfButton = document.getElementById('portfolio-download-pdf');
  ArtaPdf.bindButton(pdfButton, 'portfolio', () => active);
  pdfButton.addEventListener('click', () => { if (active) ArtaPdf.download('portfolio', active); });
  el.redo.addEventListener('click', () => stepHistory(setsApi.redo, 'redo'));

  // ---- Edit with AI (arta-ai-edit.js, shared with CV) ----
  const aiEdit = ArtaAiEdit.attach({
    ids: { toggle: 'portfolio-edit-ai', bar: 'portfolio-ai-bar', input: 'portfolio-ai-input', run: 'portfolio-ai-run' },
    api: {
      estimate: setsApi.aiEditEstimate,
      run: setsApi.aiEdit,
      undo: setsApi.undo,
    },
    get: () => active,
    set: (set) => { active = set; },
    render: renderContent,
    signature: (set) => JSON.stringify(set.entries.map((e) => [e.id, e.works, e.exhibitions, e.text_amount, e.text_override || ''])),
    confirm: (title, body, okLabel, onOk) => {
      askConfirm(title, body, okLabel, onOk);
      document.getElementById('portfolio-confirm-trigger').click();
    },
    stepUndo: () => stepHistory(setsApi.undo, 'undo'),
    toastError,
  });

  // ---- Save as New Set (SS5.5.5) ----
  // Saving is automatic, so this is not "save": it names a duplicate of the open set.
  // The original stays open and untouched.
  el.saveAs.addEventListener('click', () => {
    const name = active ? `${active.name} copy` : '';
    document.getElementById('portfolio-save-set-name').value = name.slice(0, 120);
  });
  document.getElementById('portfolio-save-set-confirm').addEventListener('click', async () => {
    const name = document.getElementById('portfolio-save-set-name').value.trim();
    if (!name || !active) return;
    let copy;
    try {
      copy = await setsApi.duplicate(active.id, name);
      await refreshSidebar();
    } catch (error) {
      toastError(`Could not save the new set: ${error.message}`);
      return;
    }
    window.dsToast.show({
      message: `${copy.name} created.`,
      actions: [{ label: 'Open', onClick: () => openSet(copy.id).catch((e) => toastError(`Could not open the set: ${e.message}`)) }],
    });
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
      document.getElementById('portfolio-rename-set-name').value = set.name;
      document.getElementById('portfolio-rename-trigger').click();
    } else if (value === 'delete') {
      askConfirm('Delete portfolio set',
        `Delete "${set.name}"? The set and its contents are removed permanently. This cannot be undone.`,
        'Delete portfolio set', async () => {
          const wasOpen = !!active && active.id === set.id;
          try {
            await setsApi.remove(set.id);
            await refreshSidebar();
            if (wasOpen) active = sets[0] ? await setsApi.get(sets[0].id) : null;
          } catch (error) {
            toastError(`Could not delete the portfolio set: ${error.message}`);
            return;
          }
          renderAll();
          window.dsToast.show({ message: `${set.name} deleted.` });
        });
      document.getElementById('portfolio-confirm-trigger').click();
    } else if (value === 'download-pdf') {
      ArtaPdf.download('portfolio', set);
    } else if (value === 'duplicate') {
      try {
        let copy = await setsApi.duplicate(set.id, `${set.name} copy`.slice(0, 120));
        // A duplicate is a working copy: never saved, even when the set it came from is.
        if (copy.saved) copy = await setsApi.setSaved(copy.id, false);
        // The server puts a new set first; here the copy goes under the one it was made
        // from. The sidebar is drawn once, after it is placed.
        const shown = (await setsApi.list()).items.filter((s) => !s.archived).map((s) => s.id).filter((id) => id !== copy.id);
        shown.splice(shown.indexOf(set.id) + 1, 0, copy.id);
        await setsApi.orderSidebar(shown);
        await refreshSidebar();
        window.dsToast.show({ message: `${copy.name} created.` });
      } catch (error) {
        toastError(`Could not duplicate the portfolio set: ${error.message}`);
      }
    }
  });
  document.getElementById('portfolio-rename-set-confirm').addEventListener('click', async () => {
    const name = document.getElementById('portfolio-rename-set-name').value.trim();
    if (!menuSet || !name || name === menuSet.name) return;
    const { id } = menuSet;
    try {
      const renamed = await setsApi.rename(id, name);
      if (active && active.id === id) active = renamed;
      await refreshSidebar();
    } catch (error) {
      toastError(`Could not rename the portfolio set: ${error.message}`);
      return;
    }
    renderAll();
  });

  // ---- Rename in place (SS5.4.1) ----
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
    input.setAttribute('aria-label', 'Portfolio set name');
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
        toastError(`Could not rename the set: ${error.message}`);
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

  // A link from a toast (?set=<id>, Prepare Application) opens that set; otherwise the
  // first in the sidebar. An id that is not there says so instead of showing another set.
  function setToOpen() {
    const wanted = new URLSearchParams(window.location.search).get('set');
    const found = wanted && knownSetIds.has(wanted);
    if (wanted && !found) toastError('That set was not found.');
    return found ? wanted : (sets[0] && sets[0].id);
  }

  // ---- First load: the sidebar, then the first set in it ----
  (async () => {
    try {
      await refreshSidebar();
      const first = setToOpen();
      if (first) await openSet(first);
    } catch (error) {
      ArtaBoot.showLoadError('portfolio-load-error', 'the portfolio sets', error);
    }
    renderAll();
    // The view is held (data-fade-hold) until the preview and its pictures are drawn,
    // so it appears once; from here re-renders stay still.
    await previewDrawn;
    if (ArtaPrint.active) {
      // The PDF export: keep only the sheets (arta-print.js).
      ArtaPrint.finish({ container: el.previewPages, sheet: '.ds-portfolio-sheet', setId: active && active.id });
      return;
    }
    await ArtaBoot.whenDrawn(el.previewPages);
    ArtaBoot.loaded();
  })();
})();
