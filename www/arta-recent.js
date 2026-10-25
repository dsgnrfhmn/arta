/*
  arta-recent.js — the "Recent Activity" list: the last 5 Portfolio / CV sets
  the user opened or edited, newest first. Kept in this browser's localStorage
  (dsgnrfhmn, 2026-10-20: per browser is enough, no server table); losing it only
  empties the menu. Portfolio and CV call touch() whenever a set is shown or
  changed; For You calls fill() to build the menu's links.

  Entries are {type: 'portfolio' | 'cv', id, name}. The name is a snapshot, so
  a renamed set shows its new name from the next touch on.
*/
(function () {
  const KEY = 'arta-recent-activity';
  const MAX = 5;
  const PAGES = { portfolio: 'portfolio.html', cv: 'cv.html' };
  const LABELS = { portfolio: 'Portfolio', cv: 'CV' };

  function read() {
    try {
      const list = JSON.parse(localStorage.getItem(KEY) || '[]');
      return Array.isArray(list) ? list.filter((e) => e && PAGES[e.type] && e.id && e.name).slice(0, MAX) : [];
    } catch (err) {
      return [];
    }
  }

  function touch(type, set) {
    if (!PAGES[type] || !set || !set.id || !set.name) return;
    const list = read().filter((e) => !(e.type === type && e.id === set.id));
    list.unshift({ type, id: set.id, name: set.name });
    try {
      localStorage.setItem(KEY, JSON.stringify(list.slice(0, MAX)));
    } catch (err) {
      // storage blocked -- the list just does not persist
    }
  }

  // Fills a dropdown-menu with one link per entry; the link opens that set.
  function fill(menu) {
    const list = read();
    menu.textContent = '';
    if (!list.length) {
      const empty = document.createElement('button');
      empty.type = 'button';
      empty.className = 'ds-dropdown-menu__item';
      empty.disabled = true;
      empty.textContent = 'Nothing yet. Open a Portfolio or CV set.';
      menu.appendChild(empty);
      return;
    }
    list.forEach((entry) => {
      const link = document.createElement('a');
      link.className = 'ds-dropdown-menu__item';
      link.setAttribute('role', 'option');
      link.href = `${PAGES[entry.type]}?set=${encodeURIComponent(entry.id)}`;
      link.textContent = `${LABELS[entry.type]}: ${entry.name}`;
      menu.appendChild(link);
    });
  }

  window.ArtaRecent = { touch, read, fill };
})();
