/*
  arta-boot.js — runs the page scripts marked type="text/boot", in document
  order, once the page has put its data into the DOM.

  Why: dis-system's components (checkbox, favourite, sort, dropdown, popups,
  icons) and arta.js wire the elements that exist when they run, once, with no
  re-init hook. A page that fills its tables or dropdowns from the API renders
  them first and only then calls ArtaBoot.runScripts(), so those scripts see
  the same DOM a static page would have shipped.
*/
(function () {
  /*
    Icons: one request per icon name, not one per element.
    dis-system's components inline their icons with a fetch of their own for every
    element (a table of 100 rows asks for checkbox.svg 100 times). Those requests
    fill the browser's connection pool and hold back the next component scripts,
    so the dropdown arrows arrived about two seconds after the rows. This memoises
    GETs of /icons/*.svg by URL and hands every caller its own Response, so a name
    is fetched once. A failed fetch is not remembered (the next render retries).
    prefetchIcons() starts the known set at page load, in parallel with the data.
  */
  const nativeFetch = window.fetch.bind(window);
  const svgCache = new Map();
  function iconUrl(input, init) {
    if (init && init.method && init.method.toUpperCase() !== 'GET') return null;
    const raw = typeof input === 'string' ? input : (input instanceof URL ? input.href : (input && input.url));
    if (!raw) return null;
    const url = new URL(raw, document.baseURI);
    return /\/icons\/[^/]+\.svg$/.test(url.pathname) ? url.href : null;
  }
  function cachedIcon(url) {
    if (!svgCache.has(url)) {
      const request = nativeFetch(url).then(async (res) => ({ ok: res.ok, status: res.status, text: await res.text() }));
      request.catch(() => svgCache.delete(url));
      svgCache.set(url, request);
    }
    return svgCache.get(url).then((icon) => {
      if (!icon.ok) svgCache.delete(url);
      return new Response(icon.text, { status: icon.status, headers: { 'Content-Type': 'image/svg+xml' } });
    });
  }
  window.fetch = function (input, init) {
    const url = iconUrl(input, init);
    return url ? cachedIcon(url) : nativeFetch(input, init);
  };
  function prefetchIcons(names) {
    names.forEach((name) => { cachedIcon(new URL(`dis-system/icons/${name}.svg`, document.baseURI).href).catch(() => {}); });
  }

  // The boot scripts are run in order, one request after another. Ask for all of
  // them up front so they download while the page's data is being fetched.
  function preloadBootScripts() {
    document.querySelectorAll('script[type="text/boot"][src]').forEach((script) => {
      const link = document.createElement('link');
      link.rel = 'preload';
      link.as = 'script';
      link.href = script.getAttribute('src');
      document.head.appendChild(link);
    });
  }

  function runScripts() {
    return Array.from(document.querySelectorAll('script[type="text/boot"]')).reduce(
      (previous, old) => previous.then(() => new Promise((resolve, reject) => {
        const script = document.createElement('script');
        const src = old.getAttribute('src');
        if (src) {
          script.src = src;
          script.onload = resolve;
          script.onerror = () => reject(new Error(`Could not load ${src}.`));
          old.replaceWith(script);
        } else {
          script.textContent = old.textContent;
          old.replaceWith(script);   // inline scripts run on insertion
          resolve();
        }
      })),
      Promise.resolve()
    );
  }

  // The page's data could not be loaded: say so, in the page, and let the
  // scripts run so the rest of the page still works. No fallback data.
  function showLoadError(id, what, error) {
    const note = document.createElement('p');
    note.setAttribute('role', 'alert');
    note.id = id;
    note.textContent = `Could not load ${what}: ${error.message}`;
    (document.querySelector('main') || document.body).prepend(note);
  }

  function ready(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else fn();
  }

  // A spinner in the middle of the window while a page loads (dsgnrfhmn, 2026-10-25):
  // dis-system loading-veil, bare. It shows only when the load takes longer than
  // SPINNER_DELAY_MS, so a fast page never blinks, and goes when the page says it has
  // loaded. Not in print mode (the PDF export).
  const SPINNER_DELAY_MS = 200;
  let pageLoaded = false;
  let pageSpinner = null;

  function spinner(label) {
    const veil = document.createElement('div');
    veil.className = 'ds-loading-veil';
    veil.dataset.variant = 'bare';
    veil.setAttribute('role', 'status');
    veil.innerHTML = '<div class="ds-loading-veil__tip"><span class="ds-loading-veil__icon" aria-hidden="true"></span><span class="ds-loading-veil__label"></span></div>';
    veil.querySelector('.ds-loading-veil__label').textContent = label;
    return veil;
  }

  setTimeout(() => {
    if (pageLoaded || (window.ArtaPrint && window.ArtaPrint.active) || !document.body) return;
    pageSpinner = spinner('Loading the page');
    document.body.appendChild(pageSpinner);
  }, SPINNER_DELAY_MS);

  // An area that is being filled again (a preview redrawn for another set) shows the
  // same spinner at its middle after the same delay; busy(area) returns the call that
  // takes it away. Calls overlap (one spinner while any of them runs). The area must be
  // positioned.
  const busyAreas = new WeakMap();   // area -> { count, timer, veil }
  function busy(area, label = 'Loading') {
    let state = busyAreas.get(area);
    if (!state) {
      state = { count: 0, timer: 0, veil: null };
      busyAreas.set(area, state);
    }
    state.count += 1;
    if (state.count === 1) {
      state.timer = setTimeout(() => {
        state.veil = spinner(label);
        area.appendChild(state.veil);
      }, SPINNER_DELAY_MS);
    }
    let ended = false;
    return () => {
      if (ended) return;
      ended = true;
      state.count -= 1;
      if (state.count > 0) return;
      clearTimeout(state.timer);
      if (state.veil) state.veil.remove();
      state.veil = null;
    };
  }

  // First load: nothing is hidden. dis-system's fade-in (linked as a plain script in
  // each page's head) fades in what the page inserts while it loads; the page calls
  // dsFadeIn.settle() once its first data is in, so later re-renders stay still.
  function loaded() {
    pageLoaded = true;
    if (pageSpinner) pageSpinner.remove();
    if (window.dsSkeleton) window.dsSkeleton.clear();
    if (window.dsFadeIn) window.dsFadeIn.settle();
  }

  // Leaving a page through the nav bar fades its area out first (dis-system fade-in
  // leave(), 200 ms, faster than the 450 ms it came in with; dsgnrfhmn, 2026-10-24). A
  // modified or middle click, a new tab and the current page keep the browser's own
  // behaviour.
  document.addEventListener('click', (event) => {
    const link = event.target instanceof Element ? event.target.closest('.ds-nav-bar a[href]') : null;
    if (!link || !window.dsFadeIn || !window.dsFadeIn.leave) return;
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (link.target && link.target !== '_self') return;
    const url = new URL(link.href, window.location.href);
    if (url.origin !== window.location.origin || url.pathname === window.location.pathname) return;
    event.preventDefault();
    window.dsFadeIn.leave(url.href);
  });

  // Resolves when the pictures and fonts inside `root` are ready to paint, or after
  // `cap` ms, whichever comes first. Portfolio and CV hold their whole view
  // (data-fade-hold) until then, so it appears once, pictures included, instead of
  // in waves (dsgnrfhmn, 2026-10-24); the cap keeps a slow Drive thumbnail from holding
  // the page blank. Only the pictures in view count (dsgnrfhmn, 2026-10-25): the others
  // load as they are scrolled to (loading="lazy"), and decoding every picture of a
  // long preview at once took hundreds of megabytes.
  function inView(img, area) {
    const r = img.getBoundingClientRect();
    return r.bottom > area.top && r.top < area.bottom && r.right > area.left && r.left < area.right;
  }
  function whenDrawn(root, cap = 2000) {
    let images = [];
    if (root) {
      const box = root.getBoundingClientRect();
      const area = {
        top: Math.max(box.top, 0), bottom: Math.min(box.bottom, window.innerHeight),
        left: Math.max(box.left, 0), right: Math.min(box.right, window.innerWidth),
      };
      images = Array.from(root.querySelectorAll('img')).filter((img) => inView(img, area));
    }
    const ready = Promise.all([
      ...images.map((img) => (img.decode ? img.decode().catch(() => {}) : Promise.resolve())),
      document.fonts ? document.fonts.ready : Promise.resolve(),
    ]);
    return Promise.race([ready, new Promise((resolve) => setTimeout(resolve, cap))]);
  }

  // The orange dot on the For You link (spec 7.5): shown while any update is unread.
  // Every page's nav carries <span data-nav-dot hidden>. Pass the count when a call
  // just returned it; otherwise the server is asked. A failure leaves the dot as it
  // was: it is a hint, never the only place the information is.
  async function navDot(unread) {
    const dots = document.querySelectorAll('[data-nav-dot]');
    if (!dots.length) return;
    try {
      const count = unread === undefined ? (await window.ArtaApi.health()).unread : unread;
      dots.forEach((dot) => { dot.hidden = !(count > 0); });
      // A page that shows the count itself (For You's "Updates (N new)") listens.
      document.dispatchEvent(new CustomEvent('arta-unread', { detail: { unread: count } }));
    } catch (error) {
      // leave it
    }
  }

  // Inline dis-system icons ([data-icon] -> dis-system/icons/<name>.svg) under
  // root. dis-system's component.js files inline icons once, on load; markup a
  // page re-renders later (the set sidebars) calls this after each render.
  // Idempotent: an element already holding its icon is skipped.
  const iconCache = new Map();
  function icons(root) {
    root.querySelectorAll('[data-icon]').forEach(async (el) => {
      const name = el.dataset.icon;
      if (el.dataset.iconLoaded === name) return;
      if (!iconCache.has(name)) {
        iconCache.set(name, fetch(`dis-system/icons/${name}.svg`).then((res) => (res.ok ? res.text() : '')));
      }
      try {
        el.innerHTML = await iconCache.get(name);
        el.dataset.iconLoaded = name;
      } catch (error) {
        iconCache.delete(name);   // unreachable -- leave empty, retry next render
      }
    });
  }

  /*
    Drag-to-reorder, one implementation for every sortable list/table.
    The whole row is the drag source (handle, name, details alike); only
    controls inside it (buttons, links, inputs, an open editor) keep their own
    behavior. The caller supplies what differs per table:
      row      selector of a row inside container
      id(row)  the row's id
      image(row)  element used as the drag image
      onReorder(ids, dragId)  receives the new id order; may return a promise
      noDrag   selector of the parts that never start a drag (default: every control).
               The sidebar lists sets as buttons, so it names only its save toggle.
      zone     element that accepts the drop (default: container). The sidebar passes its
               whole panel, so releasing over the header, the create row or the empty
               space below the list still drops: above the first row places the dragged
               row first, below the last row places it last.
    While a row is dragged its own element is the placeholder: it moves to where
    it would land and the rows it passes slide aside (FLIP, 180ms), so the others
    open a gap at the drop position. Dropping keeps that order; cancelling puts
    the row back. The placeholder is data-dragging on the row (styled in
    dis-system). Rows that are display: contents (grid tables) animate through
    their cells.
  */
  function sortable(container, { row: rowSel, id, image, onReorder, noDrag, zone: zoneEl }) {
    const zone = zoneEl || container;
    const NO_DRAG = noDrag || 'button, a, input, textarea, select, label, [contenteditable="true"], [data-no-drag]';
    const MOVE_MS = 180;
    let dragId = null;
    let dragRow = null;
    let origNext = null;
    let dropped = false;
    let allowed = false;
    let lastMove = 0;
    const rowOf = (target) => (target instanceof Element ? target.closest(rowSel) : null);
    const rows = () => Array.from(container.querySelectorAll(rowSel));
    const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const boxesOf = (row) => (getComputedStyle(row).display === 'contents' ? Array.from(row.children) : [row]);

    // Move `row` before `ref` (null: to the end of ref's parent) and slide every
    // other row from where it was to where it is now.
    function moveWithFlip(row, parent, ref) {
      const animate = !reduced();
      const before = new Map();
      if (animate) rows().forEach((r) => boxesOf(r).forEach((box) => before.set(box, box.getBoundingClientRect())));
      parent.insertBefore(row, ref);
      if (!animate) return;
      rows().forEach((r) => boxesOf(r).forEach((box) => {
        const from = before.get(box);
        if (!from) return;
        const to = box.getBoundingClientRect();
        const dx = from.left - to.left;
        const dy = from.top - to.top;
        if (!dx && !dy) return;
        box.animate(
          [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }],
          { duration: MOVE_MS, easing: 'cubic-bezier(0.2, 0, 0, 1)' }
        );
      }));
    }

    // draggable is set on the row only while the pointer is down on a draggable
    // part of it, so text selection and controls inside the row keep working.
    container.addEventListener('pointerdown', (event) => {
      const row = rowOf(event.target);
      allowed = !!row && row.dataset.editing !== 'true' && !event.target.closest(NO_DRAG);
      container.querySelectorAll('[draggable="true"]').forEach((n) => { n.draggable = false; });
      if (!row || !allowed) return;
      // A native drag needs an element with a box. Rows that are display:contents
      // (grid tables) have none, so the element under the pointer carries it. Not an
      // SVG part (a drag handle's inlined icon): it cannot be draggable, so the drag
      // never started from the handle itself (dsgnrfhmn, 2026-10-25); its HTML element can.
      let source = event.target;
      while (source !== row && !(source instanceof HTMLElement)) source = source.parentElement;
      while (source !== row && getComputedStyle(source).display === 'contents') source = source.parentElement;
      if (getComputedStyle(row).display !== 'contents') source = row;
      else if (source === row) return;
      source.draggable = true;
    });
    container.addEventListener('dragstart', (event) => {
      const row = rowOf(event.target);
      if (!row || !allowed) { if (row) event.preventDefault(); return; }
      dragId = id(row);
      dragRow = row;
      origNext = row.nextSibling;
      dropped = false;
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', dragId);
      event.dataTransfer.setDragImage(image ? image(row) : row, 0, 0);
      // After the browser has taken the drag image, so the image is the row as it was.
      setTimeout(() => { if (dragRow === row) row.dataset.dragging = 'true'; }, 0);
    });
    // Where the dragged row belongs for the pointer at `event`: past the middle
    // of a row it is passing, it takes that row's place.
    function place(event, force) {
      const over = rowOf(event.target);
      if (over === dragRow) return;
      // Let the slide finish before the next move: a row under a moving pointer
      // would otherwise be judged against its in-flight position.
      const now = performance.now();
      if (!force && now - lastMove < MOVE_MS) return;
      if (!over) {
        // Not over a row (header, create row, space below the list): past either end
        // of the rows the dragged row becomes the first or the last one.
        const all = rows();
        if (!all.length) return;
        const first = all[0];
        const last = all[all.length - 1];
        const edge = (row, pick) => pick(...boxesOf(row).map((box) => pick === Math.min ? box.getBoundingClientRect().top : box.getBoundingClientRect().bottom));
        if (event.clientY < edge(first, Math.min)) {
          if (dragRow === first) return;
          moveWithFlip(dragRow, first.parentNode, first);
        } else if (event.clientY > edge(last, Math.max)) {
          if (dragRow === last) return;
          moveWithFlip(dragRow, last.parentNode, last.nextSibling);
        } else return;
        lastMove = now;
        return;
      }
      // A display: contents row has no box of its own (its rect is 0x0), so its middle
      // is taken from the cells it holds.
      const boxes = boxesOf(over).map((box) => box.getBoundingClientRect());
      const top = Math.min(...boxes.map((r) => r.top));
      const bottom = Math.max(...boxes.map((r) => r.bottom));
      const lower = event.clientY >= top + (bottom - top) / 2;
      const all = rows();
      const draggedFirst = all.indexOf(dragRow) < all.indexOf(over);
      if (draggedFirst && lower) moveWithFlip(dragRow, over.parentNode, over.nextSibling);
      else if (!draggedFirst && !lower) moveWithFlip(dragRow, over.parentNode, over);
      else return;
      lastMove = now;
    }
    zone.addEventListener('dragover', (event) => {
      if (!dragId) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      place(event, false);
    });
    zone.addEventListener('drop', (event) => {
      if (!dragId) return;
      event.preventDefault();
      place(event, true);
      dropped = true;
      if (origNext !== dragRow.nextSibling) onReorder(rows().map(id), dragId);
    });
    container.addEventListener('dragend', () => {
      // Cancelled (Escape, dropped outside): the row goes back where it was.
      if (dragRow && !dropped && origNext !== dragRow.nextSibling) {
        moveWithFlip(dragRow, container.contains(origNext) ? origNext.parentNode : dragRow.parentNode,
          container.contains(origNext) ? origNext : null);
      }
      if (dragRow) dragRow.removeAttribute('data-dragging');
      dragId = null;
      dragRow = null;
      origNext = null;
      allowed = false;
      container.querySelectorAll('[data-dragging]').forEach((r) => r.removeAttribute('data-dragging'));
      container.querySelectorAll('[draggable="true"]').forEach((n) => { n.draggable = false; });
    });
  }

  /*
    Last-wins queue for order requests. Requests go out one at a time; while one is in
    flight only the newest waiting order is kept. submit(ids) resolves to
    { ok, value | error, stale }: stale means a newer order was submitted meanwhile, so
    the caller must not repaint from this answer (it would roll back the newer drag).
  */
  function lastWins(send) {
    let running = false;
    let pending = null;
    async function pump() {
      if (running) return;
      running = true;
      while (pending) {
        const job = pending;
        pending = null;
        try {
          const value = await send(job.ids);
          job.resolve({ ok: true, value, stale: !!pending });
        } catch (error) {
          job.resolve({ ok: false, error, stale: !!pending });
        }
      }
      running = false;
    }
    return function submit(ids) {
      return new Promise((resolve) => {
        if (pending) pending.resolve({ ok: true, value: null, stale: true });
        pending = { ids, resolve };
        pump();
      });
    };
  }

  ready(preloadBootScripts);

  window.ArtaBoot = { prefetchIcons, runScripts, showLoadError, ready, loaded, busy, whenDrawn, navDot, icons, sortable, lastWins };
})();
