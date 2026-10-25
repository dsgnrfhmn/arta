// Theme flash-fix: stamp data-theme before any stylesheet or paint (loaded
// synchronously in <head>; in a file so the CSP needs no inline script).
document.documentElement.dataset.theme = (function () {
  try {
    var stored = localStorage.getItem('ds-theme');
    return stored === 'dark' ? 'dark' : 'light';
  } catch (err) {
    return 'light';
  }
})();
