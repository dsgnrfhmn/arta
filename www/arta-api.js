/*
  arta-api.js — the single place that calls /api/*. Pages never use fetch()
  against the API directly; they call ArtaApi. Same origin, so no CORS is
  involved, and the browser sends the Origin the server checks on writes.

  Fail loud: any non-2xx response, network failure or non-JSON reply throws
  an ArtaApiError with the server's code and message. Nothing is swallowed
  and nothing falls back to stale data; the caller decides what the user
  sees (toast, unchanged row).

  Mock labelling: health() exposes `llm`. A page shows "mock" wherever the
  server says so, so a mock result can never pass as a real analysis.
*/
(function () {
  class ArtaApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.name = 'ArtaApiError';
      this.status = status;
      this.code = code;
    }
  }

  async function request(method, path, body) {
    const init = { method, headers: {}, credentials: 'same-origin', cache: 'no-store' };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(`/api${path}`, init);
    } catch (err) {
      throw new ArtaApiError(0, 'network', 'Could not reach the server.');
    }
    let payload = null;
    try {
      payload = await res.json();
    } catch (err) {
      // non-JSON reply; handled below
    }
    if (!res.ok) {
      const e = payload && payload.error;
      throw new ArtaApiError(
        res.status,
        e ? e.code : 'http_' + res.status,
        e ? e.message : 'The server returned an error.'
      );
    }
    if (payload === null) {
      throw new ArtaApiError(res.status, 'bad_response', 'The server sent an unreadable reply.');
    }
    return payload;
  }

  window.ArtaApi = {
    ArtaApiError,
    health: () => request('GET', '/health'),
    opportunities: {
      // params: { stage, archived, applied, tracker_archived }. Only the keys given are sent.
      // The Tracker's tabs: { applied: true, tracker_archived: false } and { ..., true }.
      list: (params = {}) => {
        const q = Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== null)
          .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
          .join('&');
        return request('GET', '/opportunities' + (q ? `?${q}` : ''));
      },
      get: (id) => request('GET', `/opportunities/${encodeURIComponent(id)}`),
      // { description } (For Review only), { notes }, { saved }
      patch: (id, changes) => request('PATCH', `/opportunities/${encodeURIComponent(id)}`, changes),
      // Bulk calls are one operation: one bad id changes nothing.
      archive: (ids) => request('POST', '/opportunities/archive', { ids }),
      unarchive: (ids) => request('POST', '/opportunities/unarchive', { ids }),
      promote: (ids) => request('POST', '/opportunities/promote', { ids }),
      setSaved: (ids, saved) => request('POST', '/opportunities/saved', { ids, saved }),
      // The figure for the cost confirmation (SS3.7): { usd, mocked, estimate, basis, items, ... }.
      // action: analyse | prepare. Makes no model call.
      costEstimate: (action, ids) => request('POST', '/cost-estimate', { action, ids }),
      // One operation: every row is analysed or none is. Answers { items: [records] }; the rows
      // are now in Analysed, labelled with where the analysis came from (mock or live).
      analyse: (ids) => request('POST', '/opportunities/analyse', { ids }),
      // A CV set (from the master blocks) and an empty portfolio set per row. One operation.
      // Answers { items: [{ opportunity, cv_set_id, portfolio_set_id }] }.
      prepare: (ids) => request('POST', '/opportunities/prepare', { ids }),
      // Mark Applied: the rows get an application and stay in Analysed.
      apply: (ids) => request('POST', '/opportunities/apply', { ids }),
      // status: applied | approved | declined
      setStatus: (id, status) => request('PUT', `/opportunities/${encodeURIComponent(id)}/application`, { status }),
      // Replies matched to one applied row, newest first. Bodies are text, never markup.
      updates: (id) => request('GET', `/opportunities/${encodeURIComponent(id)}/updates`),
      // Opening the popup: marks the row's replies read. Answers { opportunity, unread }.
      readUpdates: (id) => request('POST', `/opportunities/${encodeURIComponent(id)}/updates/read`, {}),
    },
    tracker: {
      // Asks the mailbox for new replies to applied rows (the hero's "Updates" button).
      // No Claude call, no cost popup. Answers { matched, new, unread }.
      refresh: () => request('POST', '/tracker/refresh', {}),
    },
    // "Load opportunities": reads every connected source. Answers { added, filtered, sources }.
    intake: {
      run: () => request('POST', '/intake/run', {}),
    },
    // Download PDF (web/exports.py): start answers { job, status } (the running export of
    // that set, if there is one); state answers
    // { status: running | done | error, pages?, message? }; fileUrl is the finished file.
    exports: {
      start: (kind, setId) => request('POST', `/sets/${kind}/${encodeURIComponent(setId)}/pdf`, {}),
      // Every export a page should follow or offer: { items: [{ job, kind, set_id, name, status, ... }] }.
      list: () => request('GET', '/exports'),
      state: (job) => request('GET', `/exports/${encodeURIComponent(job)}`),
      // Stop an export (the toast's close button, after a confirmation). Answers its state.
      stop: (job) => request('DELETE', `/exports/${encodeURIComponent(job)}`),
      fileUrl: (job) => `/api/exports/${encodeURIComponent(job)}/file`,
    },
    projects: {
      list: () => request('GET', '/projects'),
      // Read the projects anew from Drive (listing and texts). Only Add project and
      // Create new set call it; nothing refreshes on a timer (dsgnrfhmn, 2026-10-22).
      refresh: () => request('POST', '/projects/refresh', {}),
      get: (id) => request('GET', `/projects/${encodeURIComponent(id)}`),
      // Cached thumbnail, never an original. Used as an <img> or CSS url(). (The PDF export
      // swaps these for originals sized to their place, arta-print.js.)
      thumbUrl: (projectId, imageId) => `/api/thumbs/${encodeURIComponent(projectId)}/${encodeURIComponent(imageId)}`,
      // Page 1 of the portfolio PDF, the cover of every set; the fingerprint busts the cache.
      coverUrl: (fingerprint) => `/api/portfolio/cover?v=${encodeURIComponent(fingerprint)}`,
      coverPhotoUrl: (fingerprint) => `/api/portfolio/cover/photo?v=${encodeURIComponent(fingerprint)}`,
    },
    sets: (() => {
      // What both kinds share: the sidebar, save, archive, delete, undo, reset.
      const shared = (kind) => {
        const base = `/sets/${kind}`;
        const one = (id) => `${base}/${encodeURIComponent(id)}`;
        return {
          one,
          list: () => request('GET', base),
          get: (id) => request('GET', one(id)),
          create: (name) => request('POST', base, name === undefined ? {} : { name }),
          // The sidebar order: the ids of the sets shown, top to bottom. Answers the list.
          orderSidebar: (ids) => request('PUT', `${base}/order`, { ids }),
          setSaved: (id, saved) => request('PATCH', one(id), { saved }),
          // Only the name: not a save, not an undo step.
          rename: (id, name) => request('PATCH', one(id), { name }),
          // A named, saved copy of the set; the original is untouched. Answers the copy.
          duplicate: (id, name) => request('POST', `${one(id)}/duplicate`, { name }),
          save: (id, name) => request('POST', `${one(id)}/save`, { name }),
          archive: (id) => request('POST', `${one(id)}/archive`, {}),
          unarchive: (id) => request('POST', `${one(id)}/unarchive`, {}),
          remove: (id) => request('DELETE', one(id)),
          undo: (id) => request('POST', `${one(id)}/undo`, {}),
          redo: (id) => request('POST', `${one(id)}/redo`, {}),
          reset: (id) => request('POST', `${one(id)}/reset`, {}),
          preview: (id) => request('GET', `${one(id)}/preview`),
        };
      };
      const portfolio = shared('portfolio');
      const cv = shared('cv');
      return {
        portfolio: {
          ...portfolio,
          // New Full Set: a new set holding every project, all pictures, full text (the
          // projects are read anew from Drive first). Answers the set.
          createFull: () => request('POST', `/sets/portfolio/full`, {}),
          // entry: { project_id, works: [image_id], exhibitions: [image_id], text_amount }
          addEntry: (id, entry) => request('POST', `${portfolio.one(id)}/entries`, entry),
          editEntry: (id, entryId, entry) => request('PUT', `${portfolio.one(id)}/entries/${encodeURIComponent(entryId)}`, entry),
          // The set's own sheet layouts for one entry: [opening, then one per works sheet]; [] is automatic.
          setLayout: (id, entryId, plan, part = 'works') => request('PUT', `${portfolio.one(id)}/entries/${encodeURIComponent(entryId)}/layout`, { plan, part }),
          // The set's own texts for one entry: { title?, subtitle?, text?, captions?: {image_id: text} };
          // "" or null goes back to the project's own.
          setTexts: (id, entryId, texts) => request('PUT', `${portfolio.one(id)}/entries/${encodeURIComponent(entryId)}/texts`, texts),
          // The set's own cover texts: { title?, name?, details?, heading?, paragraphs?, contacts? }, each
          // one text (details and contacts a line each, paragraphs apart by a blank line); "" or null
          // goes back to the cover's own.
          setCoverTexts: (id, texts) => request('PUT', `${portfolio.one(id)}/cover`, texts),
          // Delete one page of an entry from this set: part "works" (by plan_index) or "exhibitions"
          // (by exhibition_index); its pictures, and on the opening its text, leave the set. One undo step.
          deleteSheet: (id, entryId, part, index) => request('DELETE', `${portfolio.one(id)}/entries/${encodeURIComponent(entryId)}/sheets/${part}/${index}`),
          deleteEntry: (id, entryId) => request('DELETE', `${portfolio.one(id)}/entries/${encodeURIComponent(entryId)}`),
          // Several entries at once, one undo step (the table's bulk Delete).
          deleteEntries: (id, entryIds) => request('POST', `${portfolio.one(id)}/entries/delete`, { entry_ids: entryIds }),
          order: (id, entryIds) => request('PUT', `${portfolio.one(id)}/order`, { entry_ids: entryIds }),
          // Edit with AI. Both send the artist's request as { prompt }.
          // The figure for the cost popup: { usd, mocked, estimate, basis, ... }. No model call.
          aiEditEstimate: (id, prompt) => request('POST', '/cost-estimate', { action: 'edit_portfolio', set_id: id, prompt }),
          // One model call. Answers the whole set plus ai: { origin: 'mock' | 'live', changed }.
          // The change is one undo step.
          aiEdit: (id, prompt) => request('POST', `${portfolio.one(id)}/ai-edit`, { prompt }),
        },
        cv: {
          ...cv,
          // Edit with AI, as on the Portfolio set: the figure for the cost popup, then one model call
          // that answers the whole set plus ai: { origin, changed } (one undo step).
          aiEditEstimate: (id, prompt) => request('POST', '/cost-estimate', { action: 'edit_cv', set_id: id, prompt }),
          aiEdit: (id, prompt) => request('POST', `${cv.one(id)}/ai-edit`, { prompt }),
          // Copies the block from the referent, with the id it has there.
          addBlock: (id, referentId, blockId) => request('POST', `${cv.one(id)}/blocks`, { referent_id: referentId, block_id: blockId }),
          editBlock: (id, blockId, text) => request('PUT', `${cv.one(id)}/blocks/${encodeURIComponent(blockId)}`, { text }),
          deleteBlock: (id, blockId) => request('DELETE', `${cv.one(id)}/blocks/${encodeURIComponent(blockId)}`),
          // Every block of the referent not in the set yet; then the set emptied. One undo step each.
          addAll: (id, referentId) => request('POST', `${cv.one(id)}/blocks/all`, { referent_id: referentId }),
          removeAll: (id) => request('DELETE', `${cv.one(id)}/blocks`),
          order: (id, blockIds) => request('PUT', `${cv.one(id)}/order`, { block_ids: blockIds }),
        },
      };
    })(),
    cv: {
      // The source of the left pane: Short, Middle, Full, then the saved CV sets.
      referents: () => request('GET', '/cv-referents'),
      blocks: (referentId) => request('GET', `/cv-referents/${encodeURIComponent(referentId)}/blocks`),
    },
  };
})();
