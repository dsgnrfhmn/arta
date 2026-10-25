// Boots the CV page: the nav dot, then the component scripts. The referent pane always
// shows the Full CV (dsgnrfhmn, 2026-10-25), so nothing is fetched for a chooser here.
ArtaBoot.ready(async () => {
  ArtaBoot.navDot();
  try {
    await ArtaBoot.runScripts();
  } catch (error) {
    ArtaBoot.loaded();   // a script failed to load: stop fading, the page works
    throw error;
  }
});
