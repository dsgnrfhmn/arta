// Download PDF (web/exports.py), followed in a toast that stays across pages
// (dsgnrfhmn, 2026-10-25). An export runs on the server and does not stop when a page is left,
// so it is shown by the dashboard, not by one page: every page (For You, Portfolio, CV)
// asks the server for the exports on load and follows each in a toast with no timer,
// whose text names the step and whose line fills with the pictures read. When one is
// done, the tab that started it saves the file (the job ids it started are kept in
// sessionStorage, so this survives moving between pages); any other tab offers a
// "Download" action instead. A failure says why. Starting a set that is already being
// exported only follows that export. The toast's close button asks first (dsgnrfhmn,
// 2026-10-25) and, if the answer is yes, stops the export on the server.
//
// The toast-follows-a-server-job part is written for any long task, so Prepare
// Application or Analyse can use it later; for now only the PDF export does.
(function () {
  const POLL_MS = 1500;
  const MINE_KEY = 'arta-pdf-mine';
  const followed = new Map();   // job id -> { job, kind, set_id, name, status, ..., toast }
  const buttons = [];           // header buttons that show their set's export

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // sessionStorage can be missing or throw (private window, blocked storage): then a
  // finished export is offered with "Download" instead of saved on its own.
  function mine() {
    try { return new Set(JSON.parse(sessionStorage.getItem(MINE_KEY) || '[]')); } catch (e) { return new Set(); }
  }
  function setMine(ids) {
    try { sessionStorage.setItem(MINE_KEY, JSON.stringify(Array.from(ids))); } catch (e) { /* see mine() */ }
  }
  function remember(job) { const ids = mine(); ids.add(job); setMine(ids); }
  function forget(job) { const ids = mine(); ids.delete(job); setMine(ids); }

  // ---- a followed job in a toast --------------------------------------------------

  function percent(item) {
    if (item.phase === 'pictures' && item.total > 0) return Math.round(5 + 85 * (item.done / item.total));
    return null;
  }

  function text(item) {
    const name = item.name;
    if (item.phase === 'stopping') return `Stopping the PDF of ${name}...`;
    if (item.phase === 'pictures' && item.total > 0) return `Preparing the PDF of ${name}: picture ${item.done} of ${item.total}, ${percent(item)}%.`;
    if (item.phase === 'printing') return `Writing the PDF of ${name}...`;
    if (item.phase === 'drawing') return `Preparing the PDF of ${name}...`;
    return `The PDF of ${name} waits for the one before it.`;
  }

  function save(job, name) {
    const link = document.createElement('a');
    link.href = ArtaApi.exports.fileUrl(job);
    link.download = `${name}.pdf`;
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  function finish(item, gone) {
    if (item.toast) item.toast.dismiss();
    followed.delete(item.job);
    refreshButtons();
    if (gone) {
      forget(item.job);
      window.dsToast.show({ message: `The PDF of ${item.name} was interrupted. Start it again.`, status: 'error' });
    } else if (item.status === 'stopped') {
      forget(item.job);
      window.dsToast.show({ message: `The PDF of ${item.name} was stopped.` });
    } else if (item.status === 'error') {
      forget(item.job);
      window.dsToast.show({ message: `Could not make the PDF of ${item.name}: ${item.message}`, status: 'error' });
    } else if (mine().has(item.job)) {
      forget(item.job);
      save(item.job, item.name);
      window.dsToast.show({ message: `${item.name}.pdf saved, ${item.pages} ${item.pages === 1 ? 'page' : 'pages'}.` });
    } else {
      // Finished while no page of this tab was following it: the reader chooses.
      const offer = window.dsToast.show({
        message: `${item.name}.pdf is ready.`,
        duration: 0,
        actions: [
          { label: 'Download', onClick: () => { save(item.job, item.name); offer.dismiss(); } },
          { label: 'Later', onClick: () => offer.dismiss() },
        ],
      });
    }
  }

  async function poll(item) {
    for (;;) {
      if (item.status !== 'running') return finish(item, false);
      await wait(POLL_MS);
      let state;
      try {
        state = await ArtaApi.exports.state(item.job);
      } catch (error) {
        if (error.status === 404) return finish(item, true);   // the server restarted
        continue;                                                // a missed poll: ask again
      }
      ['phase', 'done', 'total'].forEach((k) => delete item[k]);
      Object.assign(item, state);
      if (item.toast && item.status === 'running') item.toast.update({ message: text(item), progress: percent(item) });
    }
  }

  function follow(item) {
    if (followed.has(item.job)) return;
    followed.set(item.job, item);
    if (item.status === 'running') {
      item.toast = window.dsToast.show({
        message: text(item), duration: 0, progress: percent(item), onClose: () => askStop(item),
      });
    }
    refreshButtons();
    poll(item);
  }

  // ---- stopping, after a confirmation ----------------------------------------------------
  // One confirmation popup for every page, built here before the page's components boot,
  // so dis-system's popup wires it like the pages' own (Esc and the backdrop keep going).
  let stopping = null;   // the export the open confirmation is about
  const stopPopup = document.createElement('div');
  stopPopup.innerHTML = `
    <div class="ds-popup-overlay" id="arta-pdf-stop-popup" data-placement="anchored" hidden>
      <div class="ds-popup" data-size="small" role="dialog" aria-labelledby="arta-pdf-stop-title" tabindex="-1">
        <div class="ds-popup__title" id="arta-pdf-stop-title">Stop the PDF</div>
        <div class="ds-popup__body" data-type="text"><p id="arta-pdf-stop-body"></p></div>
        <div class="ds-popup__bottom">
          <button type="button" class="ds-text-button" data-priority="secondary" data-status="active" data-underline="dotted" data-popup-dismiss>Keep preparing</button>
          <button type="button" class="ds-text-button" data-priority="primary" data-status="active" data-underline="dotted" id="arta-pdf-stop-ok" data-popup-dismiss data-popup-confirm>Stop</button>
        </div>
      </div>
    </div>
    <button type="button" hidden id="arta-pdf-stop-trigger" data-popup-open="arta-pdf-stop-popup" tabindex="-1" aria-hidden="true"></button>`;
  document.body.append(...stopPopup.children);

  function askStop(item) {
    if (item.status !== 'running' || item.phase === 'stopping') return;
    stopping = item;
    document.getElementById('arta-pdf-stop-body').textContent =
      `The PDF of ${item.name} is not finished yet. Stop preparing it?`;
    document.getElementById('arta-pdf-stop-trigger').click();
  }

  document.getElementById('arta-pdf-stop-ok').addEventListener('click', async () => {
    const item = stopping;
    stopping = null;
    if (!item || item.status !== 'running') return;
    try {
      const state = await ArtaApi.exports.stop(item.job);
      if (state.status === 'running' && item.toast) {
        item.phase = 'stopping';
        item.toast.update({ message: text(item), progress: null });
      }
    } catch (error) {
      window.dsToast.show({ message: `Could not stop the PDF: ${error.message}`, status: 'error' });
    }
  });

  // ---- header buttons -----------------------------------------------------------------

  function running(kind, setId) {
    for (const item of followed.values()) {
      if (item.kind === kind && item.set_id === setId && item.status === 'running') return true;
    }
    return false;
  }

  function setLabel(button, label) {
    const span = button.querySelector(':scope > .ds-text-button__text, :scope > .ds-text-button__label');
    (span || button).textContent = label;
  }

  /** The page's own Download PDF button: it reads "Preparing PDF..." and is off while
      the set it would export (current()) is being exported. */
  function bindButton(button, kind, current) {
    buttons.push({ button, kind, current, label: button.textContent.trim() });
    refreshButtons();
  }

  function refreshButtons() {
    buttons.forEach(({ button, kind, current, label }) => {
      const set = current();
      const busy = !!set && running(kind, set.id);
      setLabel(button, busy ? 'Preparing PDF...' : label);
      button.dataset.status = busy ? 'loading' : 'active';
      button.disabled = busy;
    });
  }

  // ---- starting, and picking up on a page load ---------------------------------------

  /** kind: 'portfolio' | 'cv'; set: { id, name }. */
  async function download(kind, set) {
    if (running(kind, set.id)) return;
    let started;
    try {
      started = await ArtaApi.exports.start(kind, set.id);
    } catch (error) {
      window.dsToast.show({ message: `Could not start the PDF: ${error.message}`, status: 'error' });
      return;
    }
    remember(started.job);
    follow({ ...started, kind, set_id: set.id, name: set.name });
  }

  const toastsReady = () => new Promise((resolve) => {
    const check = () => (window.dsToast ? resolve() : setTimeout(check, 100));
    check();
  });

  async function resume() {
    await toastsReady();
    let items;
    try {
      items = (await ArtaApi.exports.list()).items;
    } catch (error) {
      return;   // no exporter here (503) or the server is unreachable: nothing to follow
    }
    items.forEach(follow);
  }

  window.ArtaPdf = { download, bindButton, refreshButtons };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', resume);
  else resume();
})();
