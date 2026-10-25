/*
  for-you-rows.js — builds the For You table rows from the API and boots the
  page.

  Why a boot step: dis-system's components (checkbox, favourite, sort,
  popups, icons) and arta.js wire themselves to the elements that exist when
  they run, once, with no re-init hook. So the rows are rendered first and
  the scripts marked type="text/boot" run afterwards, in document order.
  The page code then sees the same DOM the static page used to ship. The boot
  step itself is arta-boot.js, shared with the other pages.

  Fail loud: if the API cannot be reached the tables stay empty and a
  message says why. Nothing falls back to stale or built-in rows.

  Markup notes (kept from the static rows this replaces):
  - The tables are queries over one record set: For Review is stage
    for_review, Analysed is stage analysed, Filtered is stage filtered,
    Archived is every archived record whatever its stage, and All is every
    record. A record therefore has one row in its own table and a second row
    in All. The All row is a view: it carries the actions of the record's own
    table, but the page acts on the row in the home table and then refreshes
    the All row from the server (for-you.html, syncAll).
  - Every row carries data-status, the status of its record (statusOf): filtered,
    for_review, analysed, the application's own applied / approved /
    declined, or archived while the record is archived (its stage and application
    stay, so Unarchive brings them back). The row tint and the Status column read it. "Prepared" is not a
    status: it is data-prepared on an Analysed row, and only changes its actions.
    A prepared row and an application row have no action left (empty cell).
  - The Status column is a dropdown on every table. What it offers is
    statusChoices(rec): the same rules the API enforces (409), so an option that
    would be refused is disabled with the reason in its tooltip. Nothing goes back:
    Only Filtered can move (to For review). A For review row's dropdown is disabled:
    it leaves For review through Analyze. An Archived row's too: it leaves through Unarchive.
  - A blank deadline cell is left blank and sorts last (Figma 150:16779).
  - An empty description shows only "Add Description" and Analyze starts
    disabled: there is nothing to analyse yet.
  - Every value is escaped; text and attributes never receive raw markup.
*/
(function () {
  const TABLE_IDS = {
    review: 'foryou-review-table',
    analysed: 'foryou-analysed-table',
    archived: 'foryou-archived-table',
    filtered: 'foryou-filtered-table',
    all: 'foryou-all-table',
  };

  const esc = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  // Links come from strangers' emails: http(s) only, anything else is inert.
  const safeHref = (link) => (/^https?:\/\//i.test(link || '') ? link : '#');

  function formatDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
    return m ? `${m[3]}.${m[2]}.${m[1]}` : '';
  }

  const kindOf = (rec) => (rec.flags.archived
    ? 'archived'
    : { for_review: 'review', analysed: 'analysed', filtered: 'filtered' }[rec.stage]);

  // The record's status: "archived" while it is archived (dsgnrfhmn, 2026-09-24),
  // else its application's while it has one, else where it is. Archiving keeps
  // stage and application (opportunity.archive), so Unarchive brings the
  // previous status back.
  const statusOf = (rec) => (rec.flags.archived ? 'archived'
    : rec.application ? rec.application.status
      : rec.stage === 'filtered' ? 'filtered'
        : rec.stage === 'for_review' ? 'for_review' : 'analysed');

  const STATUS_LABEL = {
    filtered: 'Filtered', for_review: 'For review', analysed: 'Analysed',
    applied: 'Applied', approved: 'Approved', declined: 'Declined',
    archived: 'Archived',
  };
  // Workflow order, not alphabetical: the Status column sorts by it.
  const STATUS_RANK = { filtered: 0, for_review: 1, analysed: 2, applied: 3, approved: 4, declined: 5, archived: 6 };
  // The All table's default order (dsgnrfhmn, 2026-09-24), shown while no column sort
  // is on.
  const ALL_ORDER = ['approved', 'analysed', 'for_review', 'applied', 'declined', 'archived', 'filtered'];
  const allRank = (rec) => ALL_ORDER.indexOf(statusOf(rec));
  const APPLICATION_STATUSES = ['applied', 'approved', 'declined'];
  const hasApplication = (status) => APPLICATION_STATUSES.includes(status);

  // What each row button does, in a tooltip (dis-system tooltip: data-tooltip).
  const ACTION_TIPS = {
    archive: 'Move this item to Archived',
    'add-description': 'Write a description so this item can be analysed',
    analyse: 'Analyse this item. You confirm the cost first',
    prepare: 'Prepare application materials for this item',
    unarchive: 'Bring this item back from Archived',
    'for-review': 'Send this item back to For Review',
  };
  const tipAttr = (text) => (text ? ` data-tooltip="${esc(text)}"` : '');

  // Labels sit in text-button's own span from the start: the underline is drawn on it,
  // and text-button's component.js wraps only the buttons present when it runs, so a row
  // redrawn later (reshape, after a status change) would lose its underlines otherwise.
  const label = (text) => `<span class="ds-text-button__text">${text}</span>`;

  const button = (priority, status, action, text, extra) =>
    `<button type="button" class="ds-text-button" data-priority="${priority}" data-status="${status}" data-underline="dotted" data-action="${action}"${tipAttr(ACTION_TIPS[action])}${extra || ''}>${label(text)}</button>`;

  // `collapse` is the name table-cell gives the button when the column has no room for the
  // excerpt (dis-system, data-collapse-label); the excerpt-less filter reason has none.
  const viewButton = (fieldLabel, collapse) =>
    `<button type="button" class="ds-text-button" data-priority="secondary" data-status="active" data-underline="dotted" data-action="view-field"${fieldLabel === 'Reason to Filter' ? tipAttr('Read why this item was filtered') : ''} data-field-label="${fieldLabel}"${collapse ? ` data-collapse-label="${collapse}"` : ''} data-popup-open="foryou-view-popup">${label('view')}</button>`;

  // ---- cells -------------------------------------------------------------

  const checkboxCell = () => `
          <div class="ds-table-cell" data-type="checkbox-cell">
            <span class="ds-table-cell__icon" data-icon="checkbox" data-checkbox-state="unchecked" aria-hidden="true" role="img" aria-label="Select row"></span>
            <span class="ds-table-cell__icon" data-icon="checkbox-selected" data-checkbox-state="checked" aria-hidden="true" role="img" aria-label="Select row"></span>
          </div>`;

  const saveCell = (rec) => `
          <div class="ds-table-cell" data-type="icon-cell">
            <span class="ds-table-cell__icon" data-icon="save"${rec.flags.saved ? ' data-saved="true"' : ''} aria-hidden="true" role="img" aria-label="Saved"></span>
          </div>`;

  const nameCell = (rec) =>
    `<div class="ds-table-cell" data-type="text-active-cell"><span class="ds-table-cell__text">${esc(rec.fields.name)}</span></div>`;

  const typeCell = (rec, sortable) => {
    const label = rec.fields.type === 'grant' ? 'Grant' : 'Open call';
    const sort = sortable ? ` data-sort-value="${esc(label.toLowerCase())}"` : '';
    return `<div class="ds-table-cell" data-type="text-passive-cell"${sort}><span class="ds-table-cell__text">${label}</span></div>`;
  };

  const dateCell = (rec) => {
    const shown = formatDate(rec.fields.deadline);
    return shown
      ? `<div class="ds-table-cell" data-type="date" data-sort-value="${esc(rec.fields.deadline)}"><span class="ds-table-cell__text">${shown}</span></div>`
      : '<div class="ds-table-cell" data-type="date" data-sort-value=""></div>';
  };

  // `full` is for a cell whose shown text is not what "view" opens. It rides
  // along in a hidden span, so the popup and the live search both read it from the DOM.
  //
  // The button is named after the field ("summary", "full description") when the column
  // leaves less room than the excerpt needs: dis-system's table-cell hides the excerpt then.
  // `sections` ([{heading?, text}]) rides along as JSON for a popup that shows headings.
  const textWithView = (text, fieldLabel, full, cellAttrs, sections) =>
    `<div class="ds-table-cell" data-type="text-active-cell"${cellAttrs || ''}><span class="ds-table-cell__text"><span class="ds-table-cell__clip">${esc(text)}</span> ${viewButton(fieldLabel, fieldLabel)}${full ? `<span hidden data-view-source${sections ? ` data-view-sections="${esc(JSON.stringify(sections))}"` : ''}>${esc(full)}</span>` : ''}</span></div>`;

  // The Summary popup text: eligibility and relevance first (they live here and nowhere
  // else, spec 4.6.2), then the analysis blocks as labelled paragraphs, the verdict last
  // (the job-agent layout). A record analysed before the blocks existed shows its short
  // summary only. Plain text: the popup keeps the line breaks.
  const DETAIL_LABELS = [
    ['overview', 'Overview'], ['offer', 'Offer'], ['requirements', 'Requirements'], ['focus', 'Focus'],
    ['fit', 'Fit with your profile'], ['red_flags', 'Red flags'],
  ];

  // The verdict block alone: "apply: reason". Also the tooltip on the Summary cell.
  function verdictText(rec) {
    const d = rec.fields.details;
    if (!d || !d.verdict) return '';
    return d.verdict_reason ? `${d.verdict}: ${d.verdict_reason}` : d.verdict;
  }

  // The sections of the popup text, in order: [{ heading?, text }]. A section with a
  // heading is a labelled block (the analysis blocks, the verdict); the two without are
  // the eligibility line and the short summary.
  function summarySections(rec) {
    const f = rec.fields;
    const head = [];
    if (f.eligible) head.push(`Eligible: ${f.eligible}`);
    if (typeof f.relevance === 'number') head.push(`Relevance: ${f.relevance.toFixed(2)}`);
    const sections = [{ text: head.join(' | ') }, { text: f.summary }];
    const d = f.details;
    if (d) DETAIL_LABELS.forEach(([key, label]) => { if (d[key] && d[key] !== '-') sections.push({ heading: label, text: d[key] }); });
    const verdict = verdictText(rec);
    if (verdict) sections.push({ heading: 'Verdict', text: verdict });
    return sections.filter((section) => section.text);
  }

  // The same text as one plain string (the live search reads it, and the popup falls
  // back to it): heading, line break, text; blocks separated by a blank line.
  function summaryText(rec) {
    return summarySections(rec)
      .map((section) => (section.heading ? `${section.heading}\n${section.text}` : section.text))
      .join('\n\n');
  }

  // Where the column has room, the cell shows the verdict block ("apply: reason", dsgnrfhmn
  // 2026-10-24); a record analysed before the blocks existed shows its short summary.
  // "view" opens the whole summary in the popup. Hovering the cell shows the verdict
  // alone (dis-system tooltip, shown at once: data-tooltip-instant skips the 1 s
  // delay), for a verdict cut by the ellipsis or a column with room for the button only.
  const summaryCell = (rec) => {
    if (!rec.fields.summary) return '<div class="ds-table-cell" data-type="text-active-cell"></div>';
    const verdict = verdictText(rec);
    return textWithView(verdict || rec.fields.summary, 'Summary', summaryText(rec),
      verdict ? ` data-tooltip="${esc(verdict)}" data-tooltip-instant` : '', summarySections(rec));
  };

  const emptyCell = () => '<div class="ds-table-cell" data-type="text-active-cell"></div>';

  const descriptionCell = (rec, editable) => {
    if (rec.fields.description) return textWithView(rec.fields.description, 'Full Description');
    if (!editable) return '<div class="ds-table-cell" data-type="text-active-cell"></div>';
    return `<div class="ds-table-cell" data-type="text-active-cell">
            ${button('secondary', 'active', 'add-description', 'Add Description', ' data-popup-open="foryou-description-popup"')}
          </div>`;
  };

  // What the Status dropdown offers for a record. Every status is listed so the
  // vocabulary is visible; the ones the record cannot move to are disabled, with
  // the reason as the tooltip. The rules mirror the API (opportunity.set_status,
  // promote, analysable), which refuses the same moves with a 409. Since
  // 2026-10-25 (dsgnrfhmn) a record can go back: to For review from anywhere (its
  // verdict goes, Analyze brings it back), to Analysed from an application status.
  function whyNot(rec, current, value) {
    if (value === current) return '';
    if (value === 'for_review') return '';
    if (value === 'analysed') {
      if (current === 'filtered') return 'Move it to For review first.';
      if (current === 'for_review') return 'Analyze it first.';
      return '';
    }
    // an application status
    if (current === 'filtered' || current === 'for_review') return 'Analyze it first.';
    return '';
  }

  function statusChoices(rec) {
    const current = statusOf(rec);
    const values = ['for_review', 'analysed', ...APPLICATION_STATUSES];
    // Archived leaves only through Unarchive (the trigger is disabled too).
    if (current === 'archived') {
      return values.map((value) => ({ value, label: STATUS_LABEL[value], disabled: true, reason: 'Unarchive it first.' }));
    }
    if (current === 'filtered') values.unshift('filtered');
    return values.map((value) => {
      const reason = whyNot(rec, current, value);
      return { value, label: STATUS_LABEL[value], disabled: Boolean(reason), reason };
    });
  }

  // The Status column: a dropdown like the Tracker's. A record that was filtered
  // (has a reason) also gets "view", which opens why. data-filter-value is what
  // the All table's Status filter matches (arta.js).
  const statusCell = (rec) => {
    const current = statusOf(rec);
    const items = statusChoices(rec).map((c) =>
      `<button type="button" class="ds-dropdown-menu__item" role="option" data-value="${esc(c.value)}"${c.disabled ? ` disabled title="${esc(c.reason)}"` : ''}>${esc(c.label)}</button>`
    ).join('\n                ');
    const reason = rec.fields.filter_reason
      ? ` ${viewButton('Reason to Filter')}<span hidden data-view-source>${esc(rec.fields.filter_reason)}</span>` : '';
    return `<div class="ds-table-cell" data-type="dropdown" data-sort-value="${STATUS_RANK[current]}" data-filter-value="${current}">
            <div class="ds-dropdown">
              <button type="button" class="ds-input-dropdown" data-size="middle" aria-haspopup="listbox" aria-label="Status of ${esc(rec.fields.name)}"${current === 'for_review' || current === 'archived' ? '' : tipAttr('Change the status of this item')} data-dropdown-trigger${current === 'for_review' || current === 'archived' ? ' disabled' : ''}>
                <span data-dropdown-value>${STATUS_LABEL[current]}</span>
                <span class="ds-input-dropdown__icon" data-icon="arrow-down" aria-hidden="true"></span>
              </button>
              <div class="ds-dropdown-menu" role="listbox" hidden>
                ${items}
              </div>
            </div>${reason}
          </div>`;
  };

  // The Updates button of a record with an application (moved from the Tracker
  // page). Opens the shared updates popup; its count is the total, unread shows
  // the accent (data-unread). Sorts by the total.
  const updatesCell = (rec) => {
    if (!rec.application || !rec.updates) return emptyCell();
    const total = rec.updates.total;
    return `<div class="ds-table-cell" data-type="1-action" data-sort-value="${total}">
            <button type="button" class="ds-text-button" data-priority="secondary" data-status="active" data-underline="dotted" data-action="updates"${tipAttr('Replies to this application')} data-updates-count="${total}" data-unread="${rec.updates.unread > 0}" data-popup-open="foryou-updates-popup">${label(`(${total}) Updates`)}</button>
          </div>`;
  };

  const linkCell = (rec) =>
    `<div class="ds-table-cell" data-type="link"><a class="ds-text-button" data-priority="secondary" data-status="active" data-underline="solid" href="${esc(safeHref(rec.fields.link))}" rel="noopener noreferrer" target="_blank">${label('Link to source')}</a></div>`;

  const notesCell = (rec) => {
    const notes = rec.fields.notes || '';
    return `<div class="ds-table-cell" data-type="input" data-sort-value="${esc(notes.toLowerCase())}"><input class="ds-input" type="text"${notes ? ` value="${esc(notes)}"` : ''} placeholder="Add a note"></div>`;
  };

  const oneAction = (html) => `<div class="ds-table-cell" data-type="1-action">
            ${html}
          </div>`;

  const twoActions = (a, b) => `<div class="ds-table-cell" data-type="2-actions">
            ${a}
            ${b}
          </div>`;

  function actionsCell(kind, rec) {
    if (kind === 'review') {
      const empty = !rec.fields.description;
      return twoActions(
        button('secondary', 'active', 'archive', 'Archive', ' data-popup-open="foryou-archive-confirm-popup"'),
        empty
          ? '<button type="button" class="ds-text-button" data-priority="primary" data-status="disabled" data-underline="dotted" data-action="analyse" data-tooltip="Add a description first, then it can be analysed" disabled><span class="ds-text-button__text">Analyze</span></button>'
          : button('primary', 'active', 'analyse', 'Analyze', ' data-popup-open="foryou-analyse-confirm-popup"')
      );
    }
    if (kind === 'analysed') {
      const status = statusOf(rec);
      // An application or a prepared set leaves nothing to do here: the status is in
      // the Status column, and applying is the dropdown's job.
      if (hasApplication(status) || rec.prepared) return '<div class="ds-table-cell" data-type="1-action"></div>';
      return oneAction(button('primary', 'active', 'prepare', 'Prepare Application', ' data-popup-open="foryou-prepare-confirm-popup"'));
    }
    if (kind === 'archived') return oneAction(button('primary', 'active', 'unarchive', 'Unarchive'));
    return oneAction(button('primary', 'active', 'for-review', 'Move to "For Review"'));
  }

  // Cells after the checkbox and save cells. Those two are the ones the
  // components wired, so a row that moves table keeps them and only these
  // are rebuilt (see reshape).
  function bodyCells(kind, rec) {
    const cells = {
      review: () => [nameCell(rec), typeCell(rec, false), descriptionCell(rec, true), statusCell(rec), linkCell(rec), notesCell(rec), actionsCell(kind, rec)],
      analysed: () => [nameCell(rec), typeCell(rec, true), dateCell(rec), summaryCell(rec), descriptionCell(rec, false), statusCell(rec), linkCell(rec), notesCell(rec), actionsCell(kind, rec)],
      archived: () => [nameCell(rec), typeCell(rec, false), dateCell(rec), summaryCell(rec), descriptionCell(rec, false), statusCell(rec), linkCell(rec), notesCell(rec), actionsCell(kind, rec)],
      filtered: () => [nameCell(rec), typeCell(rec, false), descriptionCell(rec, false), statusCell(rec), linkCell(rec), notesCell(rec), actionsCell(kind, rec)],
      // Every column of the four tables, plus Updates. A cell the record's own table
      // does not have stays empty: Full Description is editable only while the
      // record is For Review, Updates only while the record has an application.
      // Notes is on every row, whatever the status.
      all: () => {
        const own = kindOf(rec);
        return [
          nameCell(rec), typeCell(rec, true), dateCell(rec), summaryCell(rec),
          descriptionCell(rec, own === 'review'),
          statusCell(rec),
          linkCell(rec), notesCell(rec), updatesCell(rec), actionsCell(own, rec),
        ];
      },
    }[kind]();
    return cells.join('\n          ');
  }

  function rowHtml(kind, rec) {
    // Where the analysis came from (mock or live), so the Summary popup can say so.
    const origin = rec.fields.analysis_origin ? ` data-origin="${esc(rec.fields.analysis_origin)}"` : '';
    const prepared = rec.prepared ? ' data-prepared="true"' : '';
    // arta.js restores this order when the All table has no column sort.
    const rank = kind === 'all' ? ` data-default-rank="${allRank(rec)}"` : '';
    return `<div class="ds-table-row" data-selected="false" data-status="${statusOf(rec)}"${prepared}${origin}${rank} data-id="${esc(rec.id)}">${checkboxCell()}${saveCell(rec)}
          ${bodyCells(kind, rec)}
        </div>`;
  }

  // Rebuild a live row for another table, keeping its checkbox and save
  // cells (and everything the components attached to them).
  function reshape(row, kind, rec) {
    const fresh = document.createElement('template');
    fresh.innerHTML = rowHtml(kind, rec);
    const next = fresh.content.firstElementChild;
    Array.from(row.children).slice(2).forEach((cell) => cell.remove());
    Array.from(next.children).slice(2).forEach((cell) => {
      row.appendChild(cell);
      // dropdown-menu wires the dropdowns that exist when it loads, so a rebuilt
      // cell's own dropdown is wired here.
      cell.querySelectorAll('.ds-dropdown').forEach((dropdown) => window.initDropdown && window.initDropdown(dropdown));
      // Likewise the arrow icon input-dropdown inlines once at load: copy a loaded one.
      const arrow = document.querySelector('.ds-input-dropdown__icon svg');
      cell.querySelectorAll('.ds-input-dropdown__icon').forEach((icon) => { if (arrow && !icon.firstElementChild) icon.innerHTML = arrow.outerHTML; });
    });
    row.dataset.status = next.dataset.status;
    if (next.dataset.prepared) row.dataset.prepared = next.dataset.prepared;
    else row.removeAttribute('data-prepared');
    if (next.dataset.origin) row.dataset.origin = next.dataset.origin;
    else row.removeAttribute('data-origin');
    if (next.dataset.defaultRank) row.dataset.defaultRank = next.dataset.defaultRank;
    else row.removeAttribute('data-default-rank');
    row.dataset.id = rec.id;
    return row;
  }

  function renderTable(kind, records) {
    const table = document.getElementById(TABLE_IDS[kind]);
    // Array sort is stable: within one status the API's order stays.
    if (kind === 'all') records = [...records].sort((a, b) => allRank(a) - allRank(b));
    table.insertAdjacentHTML('beforeend', records.map((rec) => rowHtml(kind, rec)).join('\n        '));
  }

  // ---- boot --------------------------------------------------------------

  async function boot() {
    try {
      const [review, analysed, archived, filtered, all] = await Promise.all([
        window.ArtaApi.opportunities.list({ stage: 'for_review', archived: false }),
        window.ArtaApi.opportunities.list({ stage: 'analysed', archived: false }),
        window.ArtaApi.opportunities.list({ archived: true }),
        window.ArtaApi.opportunities.list({ stage: 'filtered' }),
        window.ArtaApi.opportunities.list({}),
      ]);
      renderTable('review', review.items);
      renderTable('analysed', analysed.items);
      renderTable('archived', archived.items);
      renderTable('filtered', filtered.items);
      renderTable('all', all.items);
    } catch (error) {
      ArtaBoot.showLoadError('foryou-load-error', 'opportunities', error);
    }
    ArtaBoot.navDot();
    try {
      await ArtaBoot.runScripts();
    } finally {
      ArtaBoot.loaded();
    }
  }

  window.ForYou = { TABLE_IDS, kindOf, statusOf, STATUS_LABEL, STATUS_RANK, APPLICATION_STATUSES, hasApplication, rowHtml, reshape,
    formatDate, safeHref, summarySections, verdictText };

  ArtaBoot.ready(boot);
})();
