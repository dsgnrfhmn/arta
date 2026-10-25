ArtaBoot.ready(async () => {
  try {
    const { items } = await ArtaApi.projects.list();
    const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    document.querySelector('#ap-dropdown .ds-dropdown-menu').innerHTML = items
      .map((p) => `<button type="button" class="ds-dropdown-menu__item" role="option" data-value="${esc(p.id)}">${esc(p.name)}</button>`)
      .join('');
  } catch (error) {
    ArtaBoot.showLoadError('portfolio-projects-error', 'the projects', error);
  }
  ArtaBoot.navDot();
  try {
    await ArtaBoot.runScripts();
  } catch (error) {
    ArtaBoot.loaded();   // a script failed to load: stop fading, the page works
    throw error;
  }
});
