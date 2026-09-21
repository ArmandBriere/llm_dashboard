/**
 * Navigation: the view registry, the sidenav it generates and the hash router
 * that drives it.
 *
 * One view is visible at a time and only that view fetches. The registry is the
 * single source of truth for the route, the sidenav entry, the rail title, the
 * filters the view responds to and the loader that fills it, so those cannot
 * drift apart. Depends on app.js and usage.js for the loaders.
 */

/** Thin-stroke glyphs on a 16-unit box, to match the rest of the chrome. */
const NAV_ICONS = {
  overview:
    '<path d="M2.4 2.4h4.4v4.4H2.4zM9.2 2.4h4.4v4.4H9.2zM2.4 9.2h4.4v4.4H2.4zM9.2 9.2h4.4v4.4H9.2z"/>',
  burndown: '<path d="M2 2.4v11.2h12"/><path d="m4.4 10.8 2.6-3.6 2.4 2 3-4.8"/>',
  windows: '<circle cx="8" cy="8" r="5.8"/><path d="M8 4.4V8l2.4 1.6"/>',
  resets: '<path d="M13.5 8a5.5 5.5 0 1 1-1.7-3.97"/><path d="M13.6 2.2v3.1h-3.1"/>',
  models:
    '<path d="M8 1.8 14.2 5 8 8.2 1.8 5z"/><path d="m1.8 8 6.2 3.2L14.2 8"/><path d="m1.8 11 6.2 3.2L14.2 11"/>',
  activity: '<path d="M1.6 8.4h3L6.3 3.6l2.6 8.8 1.6-4h3.9"/>',
  projects:
    '<path d="M1.8 12.6V3.8a.8.8 0 0 1 .8-.8h3.2l1.4 1.8h6a.8.8 0 0 1 .8.8v7a.8.8 0 0 1-.8.8H2.6a.8.8 0 0 1-.8-.8z"/>',
  sessions:
    '<path d="M5.6 4h8.2M5.6 8h8.2M5.6 12h8.2"/><path d="M2.4 4h.02M2.4 8h.02M2.4 12h.02"/>',
  skills: '<path d="m8 1.8 1.7 4.5 4.5 1.7-4.5 1.7L8 14.2 6.3 9.7 1.8 8l4.5-1.7z"/>',
  plugins:
    '<path d="M4.6 1.8v3.2M11.4 1.8v3.2"/><path d="M2.8 5h10.4v3.4a5.2 5.2 0 0 1-10.4 0z"/><path d="M8 13.6v.6"/>',
  tools:
    '<path d="M10.2 2.2a3.6 3.6 0 0 0-4.3 4.3L2.4 10a1.5 1.5 0 0 0 2.1 2.1l3.5-3.5a3.6 3.6 0 0 0 4.3-4.3l-2 2-1.6-.4-.4-1.6z"/>',
};

/**
 * `load(force)` both fetches and renders, so re-running it without `force`
 * replays the view straight off the cache — which is what a theme switch or a
 * change of weighting needs. Which filters a view responds to is declared on
 * the controls themselves, in index.html's data-for attributes.
 */
const VIEWS = {
  overview: {
    label: "Overview",
    title: "Overview",
    load: (force) => Promise.all([loadSubscriptions(false), loadUsageSummary(force)]),
  },
  burndown: {
    label: "Burn-down",
    title: "Burn-down and resets",
    load: (force) => loadSnapshotsAndRenderChart(force),
  },
  windows: { label: "5-hour windows", title: "5-hour windows", load: loadWindowsView },
  resets: { label: "Reset log", title: "Reset log", load: loadEvents },
  models: { label: "Models & cache", title: "Models and cache", load: loadModelsView },
  activity: { label: "Activity", title: "Activity over time", load: loadActivityView },
  projects: { label: "Projects", title: "Projects", load: loadProjectsView },
  sessions: { label: "Sessions", title: "Sessions", load: loadSessionsView },
  skills: { label: "Skills", title: "Skills", load: loadSkillsView },
  plugins: { label: "Plugins", title: "Plugins", load: loadPluginsView },
  tools: { label: "Tools", title: "Tools", load: loadToolsView },
};

const NAV_GROUPS = [
  { label: null, items: ["overview"] },
  { label: "quota", items: ["burndown", "windows", "resets"] },
  { label: "usage", items: ["models", "activity", "projects", "sessions"] },
  { label: "surfaces", items: ["skills", "plugins", "tools"] },
];

const DEFAULT_VIEW = "overview";
const NAV_STORAGE_KEY = "llm-dashboard.nav.v1";

const navState = {
  current: null,
  // Guards against a poll landing on a view the user has already left.
  loadToken: 0,
};

// ---------- the sidenav ----------

function renderNav() {
  const nav = document.getElementById("nav");
  if (!nav) return;
  nav.innerHTML = NAV_GROUPS.map((group) => {
    const items = group.items
      .map((id) => {
        const view = VIEWS[id];
        return `
        <a class="nav-item" href="#/${id}" data-nav="${id}" title="${escapeHtml(view.title)}">
          <svg class="icon nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor"
               stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${NAV_ICONS[id] || ""}</svg>
          <span class="nav-label">${escapeHtml(view.label)}</span>
        </a>`;
      })
      .join("");
    const head = group.label
      ? `<span class="label nav-group-head">${escapeHtml(group.label)}</span>`
      : "";
    return `<div class="nav-group">${head}${items}</div>`;
  }).join("");
}

function syncNavActive(id) {
  document.querySelectorAll("[data-nav]").forEach((el) => {
    const active = el.dataset.nav === id;
    el.classList.toggle("active", active);
    if (active) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
}

/** Dim the controls the active view ignores rather than reflowing the bar. */
function syncFilterBar(id = navState.current) {
  document.querySelectorAll("#filterbar .control").forEach((el) => {
    const scope = el.dataset.for || "*";
    el.classList.toggle("is-idle", scope !== "*" && !scope.split(/\s+/).includes(id));
  });
  // An hour range only means something inside a single day, so "All time"
  // leaves the slider idle whatever the view.
  const hours = document.getElementById("control-hours");
  if (hours && !getSelectedDateString()) hours.classList.add("is-idle");
}

// ---------- the router ----------

function routeFromHash() {
  const id = String(location.hash || "").replace(/^#\/?/, "");
  return VIEWS[id] ? id : null;
}

function readSavedRoute() {
  try {
    const saved = localStorage.getItem(NAV_STORAGE_KEY);
    return VIEWS[saved] ? saved : null;
  } catch {
    return null;
  }
}

function showView(id, { force = false } = {}) {
  const view = VIEWS[id];
  if (!view) return;

  const changed = navState.current !== id;
  navState.current = id;

  if (changed) {
    document.querySelectorAll("[data-view]").forEach((el) => {
      el.classList.toggle("is-active", el.dataset.view === id);
    });
    const titleEl = document.getElementById("view-title");
    if (titleEl) titleEl.textContent = view.title;
    document.title = `${view.title} · Quota Console`;
    syncNavActive(id);
    syncFilterBar(id);
    clearViewError();
    // A view is only measurable once visible, so both charts are drawn on
    // arrival rather than kept alive behind display:none.
    window.scrollTo({ top: 0 });
    try {
      localStorage.setItem(NAV_STORAGE_KEY, id);
    } catch {
      /* private mode: the hash still carries the route */
    }
  }

  return runViewLoad(id, changed || force);
}

async function runViewLoad(id, force) {
  const view = VIEWS[id];
  const token = ++navState.loadToken;
  try {
    await view.load(force);
    if (token === navState.loadToken) clearViewError();
  } catch (err) {
    console.error(`Failed to load the ${id} view:`, err);
    // A poll that resolves after the user has moved on must not shout about it.
    if (token === navState.loadToken) showViewError(err);
  }
}

/** Re-run the active view. Without `force` it replays from the cache. */
function refreshCurrentView({ force = false } = {}) {
  if (!navState.current) return Promise.resolve();
  return runViewLoad(navState.current, force);
}

function onHashChange() {
  const id = routeFromHash();
  if (!id) {
    location.replace(`#/${navState.current || DEFAULT_VIEW}`);
    return;
  }
  closeNavDrawer();
  showView(id);
}

function initNav() {
  renderNav();
  const id = routeFromHash() || readSavedRoute() || DEFAULT_VIEW;
  // replace, not assign: arriving at "/" should not leave an empty history entry.
  if (routeFromHash() !== id) location.replace(`#/${id}`);
  window.addEventListener("hashchange", onHashChange);
  restoreSidenavCollapsed();
  return showView(id);
}

// ---------- errors ----------

function showViewError(err) {
  const el = document.getElementById("view-error");
  if (!el) return;
  el.textContent = `Could not load this view: ${err && err.message ? err.message : err}`;
  el.hidden = false;
}

function clearViewError() {
  const el = document.getElementById("view-error");
  if (el) el.hidden = true;
}

// ---------- sidenav chrome ----------

const SIDENAV_COLLAPSED_KEY = "llm-dashboard.sidenav-collapsed.v1";

function toggleSidenav() {
  const collapsed = document.body.classList.toggle("nav-collapsed");
  try {
    localStorage.setItem(SIDENAV_COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    /* non-fatal */
  }
}

function restoreSidenavCollapsed() {
  try {
    if (localStorage.getItem(SIDENAV_COLLAPSED_KEY) === "1") {
      document.body.classList.add("nav-collapsed");
    }
  } catch {
    /* non-fatal */
  }
}

// On narrow screens the sidenav overlays the content as a drawer instead.
function openNavDrawer() {
  document.body.classList.add("nav-open");
  const scrim = document.getElementById("nav-scrim");
  if (scrim) scrim.hidden = false;
}

function closeNavDrawer() {
  document.body.classList.remove("nav-open");
  const scrim = document.getElementById("nav-scrim");
  if (scrim) scrim.hidden = true;
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeNavDrawer();
});
