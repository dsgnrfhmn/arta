/*
  Sticky offsets (see the "Sticky chrome" comment in the stylesheet): the
  bulk-action row sticks below the nav bar, the column labels below the
  bulk-action row. Measured, not hard-coded, because the nav bar's height
  follows its content and the font. Re-run when any of them resizes and
  once the fonts have loaded.
*/
(function () {
  const root = document.documentElement;
  const nav = document.querySelector('.ds-nav-bar');
  if (!nav) return;

  function measure() {
    const navH = nav.getBoundingClientRect().height;
    const block = document.querySelector('.foryou-panel:not([hidden]) .ds-header-block');
    const blockH = block ? block.getBoundingClientRect().height : 0;
    root.style.setProperty('--foryou-sticky-block', `${navH}px`);
    root.style.setProperty('--foryou-sticky-thead', `${navH + blockH}px`);
  }

  measure();
  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(measure);
    observer.observe(nav);
    document.querySelectorAll('.foryou-panel .ds-header-block').forEach((el) => observer.observe(el));
  }
  window.addEventListener('resize', measure);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(measure);
})();
