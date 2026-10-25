/*
  arta.js — shared behavior across all four dashboard pages (for-you,
  portfolio, cv). Kept local to arta, not dis-system: this is
  page-shell wiring (theme-toggle icon/label, table-selection -> header-
  action gating), not a reusable dis-system component.

  Theme: each page stamps documentElement.dataset.theme synchronously,
  inline, as the very first thing in <head> (before any <link>) so the
  correct theme paints on the first frame -- no light-mode flash while
  dark mode loads. This file only owns the deferred remainder: showing the
  icon/label for "the theme a click switches TO", persisting a click's
  choice, and wiring the toggle button -- exactly what each page's own
  copy of this logic used to do inline, before the flash-fix required
  splitting the theme-read out to an even earlier, unavoidably-inline
  script (see each page's <head>).
*/
(function () {
  const navBar = document.querySelector('.ds-nav-bar');
  if (!navBar) return;
  const themeToggle = navBar.querySelector('.ds-nav-bar__theme-toggle');
  const themeIcon = navBar.querySelector('.ds-nav-bar__theme-icon');
  const themeLabel = navBar.querySelector('.ds-nav-bar__theme-label');
  if (!themeToggle || !themeIcon || !themeLabel) return;

  const THEME_STORAGE_KEY = 'ds-theme';

  function storeTheme(theme) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch (err) {
      // ignore -- theme just won't persist this session
    }
  }

  async function loadIcon(el) {
    // dis-system/icons/ is the Figma icon set (one set, same names as
    // job-agent uses).
    try {
      const res = await fetch(`dis-system/icons/${el.dataset.icon}.svg`);
      if (res.ok) el.innerHTML = await res.text();
    } catch (err) {
      // icon source unreachable -- leave empty
    }
  }

  // The button's label names the theme a click switches TO ("Dark" while the
  // page is light). Its icon is swapped against that (dsgnrfhmn, 2026-09-18,
  // "swap the dark/light theme icons"): the half-filled circle goes with the
  // "Dark" offer -- the same icon Figma's toggle draws on the light page --
  // and the solid circle with the "Light" offer. The offered theme lives in
  // data-offer, not in the icon name, so the icons can be assigned freely.
  const ICON_FOR_OFFER = { dark: 'theme-light', light: 'theme-dark' };
  const LABEL_FOR_OFFER = { dark: 'Dark', light: 'Light' };

  function showOffer(offer) {
    themeToggle.dataset.offer = offer;
    themeIcon.dataset.icon = ICON_FOR_OFFER[offer];
    themeLabel.textContent = LABEL_FOR_OFFER[offer];
    return loadIcon(themeIcon);
  }

  const theme = document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
  showOffer(theme === 'dark' ? 'light' : 'dark');

  themeToggle.addEventListener('click', () => {
    const switchingTo = themeToggle.dataset.offer;
    const nextOfferTheme = switchingTo === 'dark' ? 'light' : 'dark';

    // The theme switches at once, inside a view transition (dis-system site.css):
    // one cross-fade of two page snapshots, not a transition on every element.
    // The toggle's own icon is fetched after, never before: it used to be awaited
    // here, so the click waited for a network round trip.
    const root = document.documentElement;
    const apply = () => {
      root.dataset.theme = switchingTo;
      storeTheme(switchingTo);
      showOffer(nextOfferTheme);
    };
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (document.startViewTransition && !reduced) document.startViewTransition(apply);
    else apply();
  });
})();

/*
  Page-level custom scrollbar (see arta.css's own comment on
  .arta-scrollbar for the why). Same technique dis-system's own
  paper-preview/component.js already implements for its inner scroll
  panel, ported here from that container's own scrollTop/scrollHeight/
  clientHeight to the whole document's (documentElement's, in standards
  mode the actual scrolling box for a plain page with no overflow
  override) equivalents.
*/
(function () {
  const root = document.documentElement;
  const scrollbar = document.createElement('div');
  scrollbar.className = 'arta-scrollbar';
  const thumb = document.createElement('div');
  thumb.className = 'arta-scrollbar__thumb';
  scrollbar.appendChild(thumb);
  document.body.appendChild(scrollbar);

  function updateThumb() {
    const scrollHeight = root.scrollHeight;
    const clientHeight = root.clientHeight;
    if (scrollHeight <= clientHeight) {
      scrollbar.hidden = true;
      return;
    }
    scrollbar.hidden = false;
    const visibleRatio = clientHeight / scrollHeight;
    const maxScroll = scrollHeight - clientHeight;
    const scrollRatio = maxScroll > 0 ? root.scrollTop / maxScroll : 0;
    thumb.style.height = `${visibleRatio * 100}%`;
    thumb.style.top = `${scrollRatio * (1 - visibleRatio) * 100}%`;
  }

  let hideTimeout;
  window.addEventListener('scroll', () => {
    updateThumb();
    root.classList.add('is-scrolling');
    clearTimeout(hideTimeout);
    hideTimeout = setTimeout(() => root.classList.remove('is-scrolling'), 600);
  });

  window.addEventListener('resize', updateThumb);
  updateThumb();
})();

/*
  Header-action gating (CLAUDE.md General UI adjustments: "header action
  buttons stay disabled until at least one row is selected, then enable.
  Applies to all tables"). Generic across tables: a [data-checkbox-group]
  table pairs with the .ds-table-header-actions block that names it via
  data-actions-for="<group id>" -- explicit pairing rather than assuming
  one table per page, since for-you.html has two (Analysed + Pending).

  Uses text-button's own existing status axis (see
  dis-system/components/text-button/meta.json) -- status=disabled is a
  real, already-styled, already-native-disabled state, not invented here.

  Utility-style buttons that aren't a batch action on the selected rows
  (Updates refresh on For You, Add Project/Add on Portfolio/CV, per-row
  actions) are not part of a gated .ds-table-header-actions block and stay
  untouched.
*/
document.querySelectorAll('.ds-table-header-actions[data-actions-for]').forEach((actions) => {
  const group = document.getElementById(actions.dataset.actionsFor);
  if (!group) return;
  const buttons = Array.from(actions.querySelectorAll(':scope > .ds-text-button[data-gated]'));

  function sync() {
    const anySelected = group.querySelector('.ds-table-row[data-selected="true"]') !== null;
    buttons.forEach((btn) => {
      btn.disabled = !anySelected;
      btn.dataset.status = anySelected ? 'active' : 'disabled';
    });
  }

  sync();
  group.addEventListener('ds-table-row-select', sync);
});

/*
  Live table search: an
  <input data-search-for="<table id>"> filters that table's own
  .ds-table-row children only, live, case-insensitive, over each row's
  full textContent (which already includes text "hidden" behind
  truncation -- CSS line-clamp/ellipsis never removes the text from the
  DOM, only from view, so a plain textContent match finds it for free,
  same reasoning site.js's own initSampleTableSearch already documents).
  Visibility toggles via the native `hidden` attribute, this repo's own
  established convention (see that same site.js comment) rather than
  inline style.display.

  Column filters (table-header-cell's data-filter, e.g. For You's All tab)
  narrow the same rows, so the two are one calculation here, not two that
  overwrite each other's `hidden`: a row shows when it matches the query AND
  its cell in each filtered column carries one of that column's chosen
  values among its data-filter-value tokens (space-separated; For You's
  Status cell carries one, the status). It is recomputed on typing, on a filter choice, and on
  `arta-table-refresh` (a page dispatches it after it rebuilt rows). No-results state: a single full-width row is
  inserted/shown reading "No matches" -- created once, lazily, not
  hardcoded into every table's own markup.
*/
document.querySelectorAll('[data-search-for]').forEach((input) => {
  const table = document.getElementById(input.dataset.searchFor);
  if (!table) return;

  let emptyRow = null;
  function ensureEmptyRow() {
    if (emptyRow) return emptyRow;
    emptyRow = document.createElement('div');
    emptyRow.className = 'ds-table-row';
    emptyRow.hidden = true;
    const cell = document.createElement('div');
    cell.className = 'ds-table-cell';
    cell.style.gridColumn = '1 / -1';
    cell.dataset.type = 'empty-state';
    cell.innerHTML = '<span class="ds-table-cell__text">No matches</span>';
    emptyRow.appendChild(cell);
    table.appendChild(emptyRow);
    return emptyRow;
  }

  // The row's text for matching. A dropdown's option list is in the DOM but is not
  // what the row shows, so a status menu that lists "Declined" must not make every
  // row match "declined": only the shown value counts.
  function rowText(row) {
    const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) => (node.parentElement.closest('.ds-dropdown-menu') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    let text = '';
    while (walker.nextNode()) text += walker.currentNode.textContent;
    return text.toLowerCase();
  }

  // [column index, allowed values] for every filter header cell that narrows the
  // rows. A single filter allows its one chosen value; a multiple filter
  // (data-filter-multiple) allows every ticked one, none ticked allows nothing.
  // A multiple filter with every value ticked narrows nothing.
  function activeFilters() {
    const headers = Array.from(table.children).filter((el) => !el.classList.contains('ds-table-row'));
    const filters = [];
    headers.forEach((cell, index) => {
      if (cell.hasAttribute('data-filter-multiple')) {
        const trigger = cell.querySelector('.ds-table-header-cell__filter-btn');
        if (trigger && trigger.dataset.filterActive === 'true') {
          filters.push([index, (cell.dataset.filterValues || '').split(' ').filter(Boolean)]);
        }
      } else if (cell.dataset.filterValue) {
        filters.push([index, [cell.dataset.filterValue]]);
      }
    });
    return filters;
  }

  function apply() {
    const query = input.value.trim().toLowerCase();
    const filters = activeFilters();
    const rows = Array.from(table.querySelectorAll(':scope > .ds-table-row'))
      .filter((row) => row !== emptyRow);
    let visibleCount = 0;
    rows.forEach((row) => {
      const matches = (query === '' || rowText(row).includes(query))
        && filters.every(([index, values]) => (row.children[index]?.dataset.filterValue || '').split(' ').some((token) => values.includes(token)));
      row.hidden = !matches;
      if (matches) visibleCount += 1;
    });
    // The empty-state row is created only once it is needed, so a refresh with
    // nothing to narrow adds nothing to the table.
    if (rows.length > 0 && (visibleCount === 0 || emptyRow)) {
      ensureEmptyRow().hidden = visibleCount !== 0;
    }
  }

  input.addEventListener('input', apply);
  table.addEventListener('ds-table-header-cell-filter', apply);
  table.addEventListener('arta-table-refresh', apply);
});

/*
  Column-sort row reordering: table-header-cell/component.js (dis-system)
  deliberately only cycles data-sort-dir and dispatches a bubbling
  ds-table-header-cell-sort event -- it can't own a specific host table's
  row shape, so the actual reorder is this page-level listener's job (see
  that component's own meta.json rationale). Generalizes site.js's own
  initSampleTableSort algorithm (none/asc/desc cycle, data-sort-value
  preferred over textContent, "none" restores authored order) rather than
  reinventing it -- one listener per [data-sort-group] container, since
  for-you.html alone has more than one sortable table.

  "none" restores the row order captured at setup time -- rows added
  later (e.g. a row moved into Analysed after Analyse succeeds) are not
  part of that captured snapshot, so resetting to "none" after such a
  move won't include them in their original position; a known, small gap
  in a mock-data phase, not fixed here.
*/
document.querySelectorAll('[data-sort-group]').forEach((group) => {
  function getHeaderCells() {
    return Array.from(group.children).filter((el) => !el.classList.contains('ds-table-row'));
  }
  function getRows() {
    return Array.from(group.children).filter((el) => el.classList.contains('ds-table-row'));
  }
  const defaultRows = getRows();

  group.addEventListener('ds-table-header-cell-sort', (event) => {
    const headerCell = event.target.closest('.ds-table-cell, .ds-table-header-cell');
    const colIndex = getHeaderCells().indexOf(headerCell);
    if (colIndex === -1) return;

    const direction = event.detail.direction;
    let rows;
    if (direction === 'none' && getRows().some((row) => row.dataset.defaultRank)) {
      // A table whose rows carry data-default-rank (For You's All: status order)
      // has a computed default, not an authored one: the current rows by rank,
      // ties in the order captured at setup.
      const authored = (row) => { const i = defaultRows.indexOf(row); return i === -1 ? defaultRows.length : i; };
      rows = getRows().sort((a, b) => (Number(a.dataset.defaultRank) - Number(b.dataset.defaultRank))
        || (authored(a) - authored(b)));
    } else if (direction === 'none') {
      rows = defaultRows;
    } else {
      rows = getRows();
      rows.sort((a, b) => {
        const cellA = a.children[colIndex];
        const cellB = b.children[colIndex];
        const valA = (cellA && (cellA.dataset.sortValue ?? cellA.textContent) || '').trim().toLowerCase();
        const valB = (cellB && (cellB.dataset.sortValue ?? cellB.textContent) || '').trim().toLowerCase();
        // Blank values sort last in both directions (spec 3.9, "empty
        // last"): a row with no deadline / note is not "earliest".
        if (!valA !== !valB) return !valA ? 1 : -1;
        if (valA < valB) return direction === 'asc' ? -1 : 1;
        if (valA > valB) return direction === 'asc' ? 1 : -1;
        return 0;
      });
    }
    rows.forEach((row) => group.appendChild(row));
  });
});


// Nav title: while the hero block's title is on screen the bar's left slot stays empty;
// once the title has scrolled under the bar, the bar shows it (Figma 331:36235). Only
// pages with a hero title (For You) have both elements.
(function () {
  const nav = document.querySelector('.ds-nav-bar');
  const title = document.querySelector('.ds-hero-block__title');
  if (!nav || !title || !nav.querySelector('[data-nav-title]') || !('IntersectionObserver' in window)) return;
  const update = (entry) => {
    const under = !entry.isIntersecting && entry.boundingClientRect.bottom <= nav.getBoundingClientRect().bottom;
    if (under) nav.setAttribute('data-title', 'shown');
    else nav.removeAttribute('data-title');
  };
  // The bar covers the top of the viewport, so the title counts as gone once it is under it.
  new IntersectionObserver((entries) => entries.forEach(update), {
    rootMargin: `-${Math.round(nav.getBoundingClientRect().height)}px 0px 0px 0px`,
  }).observe(title);
})();

/*
  Fixed widths for the text buttons of the nav bar and the hero's action row.
  A button's width must not change when its text does: the current page's nav
  link is bold (priority primary) and the others are not, so a link used to be
  wider on its own page than on the others and the row shifted between pages;
  the theme toggle swaps Dark/Light; the hero's Updates button changes its count
  and says "Refreshing...". Each button gets a min-width of its widest
  state, measured once the fonts are in: bold for the nav links, and every text
  listed in its data-width-samples ("a|b|c"), the theme toggle's own two labels
  included. The label is centered in the reserved width.
*/
(function () {
  const buttons = Array.from(document.querySelectorAll('.ds-nav-bar .ds-text-button, .ds-hero-block__actions .ds-text-button'));
  if (!buttons.length) return;

  const labelOf = (button) => button.querySelector(':scope > .ds-text-button__text, :scope > .ds-text-button__label');

  function samplesOf(button) {
    const listed = (button.dataset.widthSamples || '').split('|').filter(Boolean);
    if (button.classList.contains('ds-nav-bar__theme-toggle')) listed.push('Dark', 'Light');
    return listed;
  }

  function lock() {
    buttons.forEach((button) => {
      const label = labelOf(button);
      button.style.minWidth = '';
      let widest = button.getBoundingClientRect().width;

      if (button.closest('.ds-nav-bar__center')) {
        const priority = button.dataset.priority;
        button.dataset.priority = 'primary';
        widest = Math.max(widest, button.getBoundingClientRect().width);
        if (priority === undefined) delete button.dataset.priority;
        else button.dataset.priority = priority;
      }

      const samples = samplesOf(button);
      if (label && samples.length) {
        // The label's own child nodes go back as they were (the nav link holds a
        // dot span; restoring textContent would drop it).
        const original = Array.from(label.childNodes);
        samples.forEach((sample) => {
          label.textContent = sample;
          widest = Math.max(widest, button.getBoundingClientRect().width);
        });
        label.replaceChildren(...original);
      }

      button.style.minWidth = `${widest}px`;
      button.style.justifyContent = 'center';
    });
  }

  lock();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(lock);
})();
