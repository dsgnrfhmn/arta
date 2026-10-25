// Print mode for the PDF export (web/pdf_export.py opens <page>.html?set=<id>&print=1 in
// headless Chromium). Loaded in <head> after arta-theme.js. Outside print mode it only
// defines ArtaPrint.active = false.
//
// In print mode the page draws the set as usual, with thumbnails, then calls
// ArtaPrint.finish(): every picture is swapped for its original at the size its place on
// the sheet needs at 300 dpi (a picture in a grid needs far fewer pixels than a full-bleed
// one, and a set of hundreds of pictures has to fit in the browser's memory), the page
// keeps only its sheets, at their own size (842 x 599 px, arta.css), without the editing
// tools, and reports in window.__artaPrint, which the exporter waits for:
//   { state: 'loading', phase: 'drawing' }
//   -> { state: 'loading', phase: 'pictures', done, total }   (the originals arriving)
//   -> { state: 'ready', pages: n }  or  { state: 'error', message }
(function () {
  const params = new URLSearchParams(window.location.search);
  const active = params.has('print');
  window.ArtaPrint = { active };
  if (!active) return;

  // Paper is printed light whatever theme the dashboard was left in.
  document.documentElement.dataset.theme = 'light';
  document.documentElement.setAttribute('data-print', '');
  window.__artaPrint = { state: 'loading', phase: 'drawing' };

  function fail(message) {
    if (window.__artaPrint.state === 'loading') window.__artaPrint = { state: 'error', message };
  }

  // What only the dashboard shows: the layout menu, edit states, drag handles, the
  // fade-in's in-between states. The sheets themselves stay as drawn.
  function clean(root) {
    root.querySelectorAll('.ds-layout-picker, .ds-cv-sheet__actions').forEach((n) => n.remove());
    root.querySelectorAll('[contenteditable]').forEach((n) => n.removeAttribute('contenteditable'));
    root.querySelectorAll('[draggable]').forEach((n) => n.removeAttribute('draggable'));
    root.querySelectorAll('[data-ds-fade-pending], [data-fade-hold], [data-editing]').forEach((n) => {
      n.removeAttribute('data-ds-fade-pending');
      n.removeAttribute('data-fade-hold');
      n.removeAttribute('data-editing');
    });
    root.querySelectorAll('.ds-fade-in').forEach((n) => n.classList.remove('ds-fade-in'));
    root.removeAttribute('data-sheet-tools');
    root.removeAttribute('data-click-to-edit');
    root.querySelectorAll('[style*="opacity"]').forEach((n) => n.style.removeProperty('opacity'));
  }

  const SHEET_W = 842;          // a sheet's width in its own units, which are points in the PDF
  const DPI = 300;
  const MIN_PX = 200;
  const MAX_PX = 3600;

  // The pixels a picture needs on its long side: the long side of the picture as drawn
  // (all of it, also what object-fit: cover cuts away), in sheet units, at DPI.
  function printPixels(img) {
    const sheet = img.closest('.ds-portfolio-sheet');
    const box = img.getBoundingClientRect();
    if (!sheet || !box.width || !img.naturalWidth) return MAX_PX;
    const unit = SHEET_W / sheet.getBoundingClientRect().width;
    const fit = getComputedStyle(img).objectFit === 'contain' ? Math.min : Math.max;
    const scale = fit(box.width / img.naturalWidth, box.height / img.naturalHeight);
    const long = Math.max(img.naturalWidth, img.naturalHeight) * scale * unit;
    return Math.min(MAX_PX, Math.max(MIN_PX, Math.ceil(long * DPI / 72)));
  }

  // Swaps the thumbnails and counts the originals in as they arrive (the progress).
  function toOriginals(root) {
    const pictures = Array.from(root.querySelectorAll('img'))
      .filter((img) => (img.getAttribute('src') || '').startsWith('/api/thumbs/'));
    const progress = { state: 'loading', phase: 'pictures', done: 0, total: pictures.length };
    window.__artaPrint = progress;
    const arrived = () => { progress.done += 1; };
    pictures.forEach((img) => {
      const src = img.getAttribute('src');
      const px = printPixels(img);
      img.addEventListener('load', arrived, { once: true });
      img.addEventListener('error', arrived, { once: true });
      img.src = `${src.replace('/api/thumbs/', '/api/print/images/')}?px=${px}`;
    });
  }

  function decoded(root) {
    return Promise.all(Array.from(root.querySelectorAll('img')).map((img) => {
      if (img.complete && img.naturalWidth > 0) return img.decode ? img.decode().catch(() => {}) : null;
      return new Promise((resolve, reject) => {
        img.addEventListener('load', resolve, { once: true });
        img.addEventListener('error', () => reject(new Error('A picture could not be loaded from Drive.')), { once: true });
      });
    }));
  }

  /** container: the element holding the drawn sheets; sheet: their selector;
      setId: the set the page opened, which must be the one asked for. */
  async function finish({ container, sheet, setId }) {
    try {
      if (setId !== params.get('set')) throw new Error('That set was not found.');
      const count = container ? container.querySelectorAll(sheet).length : 0;
      if (!count) throw new Error('This set has no pages to print.');
      // The thumbnails first, to measure what each picture needs; then the originals, all in
      // before the copy: a caption over a picture takes its tone when the picture loads
      // (portfolio-sheet), and the copy carries what was measured.
      await decoded(container);
      toOriginals(container);
      await decoded(container);
      const copy = container.cloneNode(true);
      copy.removeAttribute('id');
      copy.removeAttribute('hidden');
      clean(copy);
      if (window.dsFadeIn) window.dsFadeIn.settle();   // nothing inserted from here fades in
      const print = document.createElement('div');
      print.className = 'arta-print';
      print.appendChild(copy);
      document.body.replaceChildren(print);
      await decoded(print);
      if (document.fonts) await document.fonts.ready;
      window.__artaPrint = { state: 'ready', pages: count };
    } catch (error) {
      fail(error.message || 'The pages could not be drawn.');
    }
  }

  window.ArtaPrint = { active, finish, fail };
})();
