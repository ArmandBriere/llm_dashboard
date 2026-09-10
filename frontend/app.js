/**
 * LLM Quota Tracker - Frontend JavaScript
 * High-fidelity interactive charts, live card meters, robust ISO date parsing,
 * and background auto-sync.
 */

let state = {
  subscriptions: [],
  // null = every account; otherwise a Set of subscription ids
  selectedSubIds: null,
  selectedDate: "today",
  startHour: 7,
  endHour: 21,
  displayMode: "used", // "used" (0->100%) or "remaining" (100->0%)
  usageWeight: "cost", // how model shares are weighted: "cost" or "tokens"
  chart: null,
  events: [],
  pollingInterval: null,
};

// Robust ISO date parser that handles +00:00, Z, and local timestamps
function parseIsoDate(str) {
  if (!str) return null;
  let s = String(str).trim();
  // Strip duplicate trailing Z if someone passed +00:00Z
  if (s.endsWith("+00:00Z")) s = s.replace("+00:00Z", "Z");
  // If no timezone indicator, append Z to treat as UTC
  if (!s.endsWith("Z") && !/[+-]\d{2}:\d{2}$/.test(s)) {
    s += "Z";
  }
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

/**
 * One hue per account, fixed by identity so a filter never repaints the
 * survivors. The first two match the T3 Code provider accents: Labs is green
 * (lifted from #1d700e to a step that reads on the dark surface), Vooban is
 * blue. Extra accounts take the next free slot in a fixed order. The palette was
 * validated for colour-vision deficiency on this surface.
 */
const ACCOUNT_PALETTE = [
  { key: "green", line: "#34a832", soft: "#6fcf6b", fill: "rgba(52, 168, 50, 0.14)", tint: "rgba(52, 168, 50, 0.16)", border: "rgba(52, 168, 50, 0.45)" },
  { key: "blue", line: "#3c74d5", soft: "#82a9ea", fill: "rgba(60, 116, 213, 0.14)", tint: "rgba(60, 116, 213, 0.16)", border: "rgba(60, 116, 213, 0.45)" },
  { key: "amber", line: "#d97706", soft: "#f0ad4e", fill: "rgba(217, 119, 6, 0.14)", tint: "rgba(217, 119, 6, 0.16)", border: "rgba(217, 119, 6, 0.45)" },
  { key: "pink", line: "#db2777", soft: "#ec6ea6", fill: "rgba(219, 39, 119, 0.14)", tint: "rgba(219, 39, 119, 0.16)", border: "rgba(219, 39, 119, 0.45)" },
];

// Accounts whose colour should match the rest of the user's tooling.
const ACCOUNT_COLOR_BY_NAME = { voobanlabs: "green", labs: "green", vooban: "blue" };

function nameColorKey(sub) {
  const n = String((sub && (sub.organization_name || sub.email)) || "").toLowerCase().replace(/\s+/g, "");
  return ACCOUNT_COLOR_BY_NAME[n] || null;
}

function getAccountColor(subOrId) {
  const sub = typeof subOrId === "object" && subOrId !== null
    ? subOrId
    : state.subscriptions.find((s) => String(s.id) === String(subOrId));

  const byName = nameColorKey(sub);
  if (byName) return ACCOUNT_PALETTE.find((p) => p.key === byName);

  // Unnamed accounts take the free hues in a fixed order by subscription id,
  // so the colour follows the account rather than its position in a filter.
  const claimed = new Set(state.subscriptions.map(nameColorKey).filter(Boolean));
  const free = ACCOUNT_PALETTE.filter((p) => !claimed.has(p.key));
  const unnamed = state.subscriptions
    .filter((s) => !nameColorKey(s))
    .map((s) => String(s.id))
    .sort((a, b) => Number(a) - Number(b));
  const id = sub ? String(sub.id) : String(subOrId);
  const idx = Math.max(0, unnamed.indexOf(id));
  return free[idx % free.length] || ACCOUNT_PALETTE[0];
}

function accountName(sub) {
  return (sub && (sub.organization_name || sub.email)) || "Claude";
}

// Comma-separated ids for the API, or null when every account is selected.
function selectedIdsParam() {
  if (!state.selectedSubIds || state.selectedSubIds.size === 0) return null;
  return Array.from(state.selectedSubIds).join(",");
}

function formatHourLabel(hour) {
  const d = new Date(2000, 0, 1, hour, 0, 0, 0);
  return d.toLocaleTimeString([], { hour: "numeric" });
}

function getLocalDateString(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

// Resolve the active date filter to a concrete YYYY-MM-DD, or null for "All Time".
function getSelectedDateString() {
  if (state.selectedDate === "all") return null;
  if (state.selectedDate === "today") return getLocalDateString(new Date());
  return state.selectedDate || null;
}

/**
 * The local-time window the chart should span, derived from the selected date and
 * hour range. The backend treats end_hour inclusively (end_hour=21 keeps 21:59),
 * so the window closes at the top of the following hour. Returns null for
 * "All Time", where the axis is free to fit the data.
 */
function getWindowBounds() {
  const dateStr = getSelectedDateString();
  if (!dateStr) return null;

  const [year, month, day] = dateStr.split("-").map(Number);
  if (!year || !month || !day) return null;

  const startHour = state.startHour != null ? state.startHour : 0;
  const endHour = state.endHour != null ? state.endHour : 23;

  const min = new Date(year, month - 1, day, startHour, 0, 0, 0);
  const max = new Date(year, month - 1, day, endHour + 1, 0, 0, 0);
  return max > min ? { min, max } : null;
}

function pickTimeUnit(spanMs) {
  const spanHours = spanMs / (1000 * 60 * 60);
  if (spanHours > 48) return "day";
  if (spanHours > 3) return "hour";
  return "minute";
}

// Highlight the preset pills only when the current filters actually match them.
function syncFilterButtons() {
  const setActive = (id, isActive) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("active", isActive);
  };

  const isAllTime = state.selectedDate === "all";
  setActive("btn-today", !isAllTime && getSelectedDateString() === getLocalDateString(new Date()));
  setActive("btn-all-dates", isAllTime);
  document.querySelectorAll("#hour-presets .pill-btn").forEach((btn) => {
    const [a, b] = String(btn.dataset.hours || "").split("-").map(Number);
    btn.classList.toggle("active", a === state.startHour && b === state.endHour);
  });
  syncHourSlider();
}

// Keep the slider thumbs, the filled track and the text label in step with state.
function syncHourSlider() {
  const startInput = document.getElementById("filter-start-hour");
  const endInput = document.getElementById("filter-end-hour");
  const fill = document.getElementById("hour-range-fill");
  const label = document.getElementById("hour-range-label");
  if (startInput && Number(startInput.value) !== state.startHour) startInput.value = state.startHour;
  if (endInput && Number(endInput.value) !== state.endHour) endInput.value = state.endHour;
  if (fill) {
    const left = (state.startHour / 23) * 100;
    const right = (state.endHour / 23) * 100;
    fill.style.left = `${left}%`;
    fill.style.width = `${Math.max(0, right - left)}%`;
  }
  if (label) {
    const isFullDay = state.startHour === 0 && state.endHour === 23;
    label.textContent = isFullDay
      ? "Full 24 hours"
      : `${formatHourLabel(state.startHour)} – ${formatHourLabel(state.endHour + 1)}`;
  }
}

// Live feedback while dragging; the fetch happens on change (thumb release).
function onHourSlider(which) {
  const startInput = document.getElementById("filter-start-hour");
  const endInput = document.getElementById("filter-end-hour");
  if (!startInput || !endInput) return;
  let start = clampHour(startInput.value, 0);
  let end = clampHour(endInput.value, 23);
  // Thumbs may not cross: push the other one along.
  if (start > end) {
    if (which === "start") end = start; else start = end;
  }
  state.startHour = start;
  state.endHour = end;
  startInput.value = start;
  endInput.value = end;
  syncFilterButtons();
  renderWindowLabel();
}

function renderWindowLabel() {
  const el = document.getElementById("chart-range-label");
  if (!el) return;

  const bounds = getWindowBounds();
  if (!bounds) {
    el.textContent = "Showing all recorded history.";
    return;
  }

  const dateLabel = bounds.min.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  const timeOpts = { hour: "numeric", minute: "2-digit" };
  const isFullDay = state.startHour === 0 && state.endHour === 23;
  el.textContent = isFullDay
    ? `Showing ${dateLabel}, full 24 hours.`
    : `Showing ${dateLabel}, ${bounds.min.toLocaleTimeString([], timeOpts)} – ${bounds.max.toLocaleTimeString([], timeOpts)}.`;
}

document.addEventListener("DOMContentLoaded", () => {
  initDashboard();
});

/**
 * The view settings (accounts, date, hours, mode) survive a page refresh via
 * localStorage. "today" is stored as the sentinel, not the resolved date, so a
 * saved "Today" view still means today tomorrow.
 */
const VIEW_STORAGE_KEY = "llm-dashboard.view.v1";

function saveViewState() {
  try {
    localStorage.setItem(VIEW_STORAGE_KEY, JSON.stringify({
      selectedSubIds: state.selectedSubIds ? Array.from(state.selectedSubIds) : null,
      selectedDate: state.selectedDate,
      startHour: state.startHour,
      endHour: state.endHour,
      displayMode: state.displayMode,
      usageWeight: state.usageWeight,
    }));
  } catch (err) {
    console.warn("Could not save view settings:", err);
  }
}

function restoreViewState() {
  let saved;
  try {
    saved = JSON.parse(localStorage.getItem(VIEW_STORAGE_KEY) || "null");
  } catch {
    saved = null;
  }
  if (!saved || typeof saved !== "object") return;

  if (Array.isArray(saved.selectedSubIds) && saved.selectedSubIds.length > 0) {
    state.selectedSubIds = new Set(saved.selectedSubIds.map(String));
  }
  if (saved.selectedDate === "all" || saved.selectedDate === "today" || /^\d{4}-\d{2}-\d{2}$/.test(saved.selectedDate || "")) {
    state.selectedDate = saved.selectedDate;
  }
  if (Number.isInteger(saved.startHour) && Number.isInteger(saved.endHour)) {
    state.startHour = clampHour(saved.startHour, 7);
    state.endHour = Math.max(state.startHour, clampHour(saved.endHour, 21));
  }
  if (saved.displayMode === "used" || saved.displayMode === "remaining") {
    state.displayMode = saved.displayMode;
  }
  if (saved.usageWeight === "cost" || saved.usageWeight === "tokens") {
    state.usageWeight = saved.usageWeight;
  }
}

// Push state into the controls (date box, slider, mode pills) without fetching.
function syncControlsFromState() {
  const dateInput = document.getElementById("filter-date");
  if (dateInput) {
    const dateStr = getSelectedDateString();
    dateInput.value = dateStr || "";
  }
  const usedBtn = document.getElementById("mode-used");
  const remBtn = document.getElementById("mode-remaining");
  if (usedBtn) usedBtn.classList.toggle("active", state.displayMode === "used");
  if (remBtn) remBtn.classList.toggle("active", state.displayMode === "remaining");
  syncFilterButtons();
  renderWindowLabel();
  if (typeof syncUsageControls === "function") syncUsageControls();
}

async function initDashboard() {
  restoreViewState();
  syncControlsFromState();

  await loadSubscriptions();
  await loadEvents();
  await loadSnapshotsAndRenderChart();
  loadUsage();

  // Auto-sync every 30 seconds
  if (state.pollingInterval) clearInterval(state.pollingInterval);
  state.pollingInterval = setInterval(async () => {
    await loadSubscriptions(false);
    await loadSnapshotsAndRenderChart(false);
    await loadEvents();
    loadUsage();
  }, 30000);
}

async function loadSubscriptions(updateDropdown = true) {
  try {
    const res = await fetch("/api/subscriptions");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.subscriptions = data;
    renderSubscriptionCards(data);
    if (updateDropdown) {
      updateSubscriptionDropdown(data);
    }
    updateLastPolledTimestamp();
  } catch (err) {
    console.error("Failed to load subscriptions:", err);
  }
}

function updateLastPolledTimestamp() {
  const el = document.getElementById("last-updated");
  if (el) {
    const now = new Date();
    el.textContent = `Synced: ${now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
  }
}

function renderSubscriptionCards(subs) {
  const container = document.getElementById("subscriptions-grid");
  const countEl = document.getElementById("subscription-count");
  if (!container) return;

  if (countEl) {
    countEl.textContent = `${subs.length} subscription${subs.length !== 1 ? "s" : ""} active`;
  }

  if (!subs || subs.length === 0) {
    container.innerHTML = `<div class="empty-state">No active subscriptions found.</div>`;
    return;
  }

  container.innerHTML = subs
    .map((sub) => {
      const snap = sub.latest_snapshot || {};
      const fiveHourPct = snap.five_hour_pct != null ? snap.five_hour_pct : 0;
      const sevenDayPct = snap.seven_day_pct != null ? snap.seven_day_pct : 0;

      const fiveHourCountdown = snap.five_hour_countdown || formatCountdown(snap.five_hour_resets_at);
      const sevenDayCountdown = snap.seven_day_countdown || formatCountdown(snap.seven_day_resets_at);

      const color = getAccountColor(sub);

      // Org initials avatar
      const orgName = sub.organization_name || "Claude";
      const initials = orgName.split(" ").map((w) => w[0]).join("").slice(0, 2).toUpperCase();

      // Spend details
      let spendHtml = `<span class="spend-badge">💳 Spend: None</span>`;
      if (snap.spend_used != null) {
        const curr = snap.spend_currency || "USD";
        const isLimit = snap.spend_limit != null && snap.spend_used >= snap.spend_limit;
        if (snap.spend_limit != null) {
          spendHtml = `<span class="spend-badge ${isLimit ? "spend-critical" : ""}">💳 $${snap.spend_used.toFixed(2)} / $${snap.spend_limit.toFixed(2)} ${curr}</span>`;
        } else {
          spendHtml = `<span class="spend-badge">💳 $${snap.spend_used.toFixed(2)} ${curr}</span>`;
        }
      }

      // Scoped model text
      const scopedHtml = snap.scoped_model
        ? `<span>⚡ Model ${escapeHtml(snap.scoped_model)}: <strong>${snap.scoped_pct}%</strong></span>`
        : `<span>⚡ Standard Quota</span>`;

      // A cached reading, or one whose reset has already passed, no longer
      // describes the current window — show it as unverified rather than fact.
      // The 5-hour quota is a rolling window: it opens on the first request of
      // a session and expires 5h later. Between sessions there is no window,
      // which the API reports as 0% with no reset time. That is a real
      // reading, so only a cached or already-reset one counts as unverified.
      const isIdleWindow = snap.five_hour_pct != null && !snap.five_hour_resets_at;
      const isUnverified = !isIdleWindow && !!(snap.is_stale || snap.is_expired);
      const isExhausted5h = fiveHourPct >= 100 && !isUnverified;
      const statColor5h = isUnverified
        ? "stat-unknown"
        : (isExhausted5h ? "stat-exhausted" : (fiveHourPct >= 80 ? "stat-warning" : "stat-normal"));

      let tag5h;
      if (isIdleWindow) {
        tag5h = `<span class="quota-status-tag tag-idle" title="No 5-hour window is currently open. It starts on your next request and runs for 5 hours.">💤 Idle</span>`;
      } else if (snap.is_expired) {
        tag5h = `<span class="quota-status-tag tag-amber" title="This quota window already reset. The figure below predates the reset and is not current — waiting on a live reading.">⏳ Awaiting refresh</span>`;
      } else if (snap.is_stale) {
        tag5h = `<span class="quota-status-tag tag-amber" title="Anthropic's usage API rate-limited the last poll, so this is a cached reading rather than a live one.">⚠️ Cached</span>`;
      } else if (isExhausted5h) {
        tag5h = `<span class="quota-status-tag tag-red">⚠️ Exhausted</span>`;
      } else {
        tag5h = `<span class="quota-status-tag tag-cyan">Normal</span>`;
      }

      return `
      <div class="sub-card" style="--account: ${color.line}; --account-soft: ${color.soft}; --account-tint: ${color.tint}; --account-border: ${color.border};">
        <div class="sub-header">
          <div class="sub-org-group">
            <div class="sub-avatar">${initials}</div>
            <div>
              <div class="sub-name">${escapeHtml(orgName)}</div>
              <div class="sub-email">${escapeHtml(sub.email)}</div>
            </div>
          </div>
        </div>

        <!-- Metric Stat Blocks Grid -->
        <div class="quota-blocks-grid">
          <!-- 5-Hour Block -->
          <div class="quota-block">
            <div class="quota-block-title">
              <span>5-Hour Quota</span>
              ${tag5h}
            </div>
            <div class="quota-hero-stat">
              <span class="quota-hero-num ${statColor5h}">${isUnverified ? "—" : fiveHourPct.toFixed(1) + "%"}</span>
            </div>
            <div class="progress-track">
              <div class="progress-bar bar-5h ${isExhausted5h ? "exhausted" : ""} ${isUnverified ? "unverified" : ""}" style="width: ${isUnverified ? 100 : Math.min(fiveHourPct, 100)}%"></div>
            </div>
            <div class="quota-time-meta">
              ${isIdleWindow
                ? `<span>🕒 No active session — a window opens on your next request</span>`
                : snap.is_expired
                ? `<span>🕒 Was ${fiveHourPct.toFixed(1)}% before the reset — awaiting a live reading</span>`
                : snap.is_stale
                  ? `<span>🕒 Last known ${fiveHourPct.toFixed(1)}% · resets ${escapeHtml(fiveHourCountdown)}</span>`
                  : `<span>🕒 Resets ${fiveHourCountdown}</span>`}
            </div>
          </div>

          <!-- 7-Day Block -->
          <div class="quota-block">
            <div class="quota-block-title">
              <span>7-Day Quota</span>
              <span class="quota-status-tag tag-purple">Weekly</span>
            </div>
            <div class="quota-hero-stat">
              <span class="quota-hero-num stat-normal">${sevenDayPct.toFixed(1)}%</span>
            </div>
            <div class="progress-track">
              <div class="progress-bar bar-7d" style="width: ${Math.min(sevenDayPct, 100)}%"></div>
            </div>
            <div class="quota-time-meta">
              <span>📅 Resets ${sevenDayCountdown}</span>
            </div>
          </div>
        </div>

        <div class="sub-card-footer">
          ${spendHtml}
          ${scopedHtml}
        </div>
      </div>
    `;
    })
    .join("");
}

function updateSubscriptionDropdown(subs) {
  renderAccountChips(subs);
}

function renderAccountChips(subs) {
  const group = document.getElementById("account-chips");
  if (!group) return;

  // Drop selections for accounts that no longer exist.
  if (state.selectedSubIds) {
    const known = new Set(subs.map((s) => String(s.id)));
    state.selectedSubIds = new Set(Array.from(state.selectedSubIds).filter((id) => known.has(id)));
    if (state.selectedSubIds.size === 0 || state.selectedSubIds.size === subs.length) state.selectedSubIds = null;
  }

  const allActive = !state.selectedSubIds;
  group.innerHTML =
    `<button class="chip ${allActive ? "active" : ""}" data-sub-id="all" onclick="toggleAccount('all')" title="Show every account">All</button>` +
    subs
      .map((sub) => {
        const color = getAccountColor(sub);
        const on = allActive || state.selectedSubIds.has(String(sub.id));
        return `<button class="chip chip-account ${on ? "active" : ""}" data-sub-id="${sub.id}"
                  style="--account: ${color.line}; --account-tint: ${color.tint}; --account-border: ${color.border};"
                  onclick="toggleAccount('${sub.id}')" aria-pressed="${on}"
                  title="Click to show only this account; click again to add or remove it">
                  <span class="chip-swatch"></span>${escapeHtml(accountName(sub))}
                </button>`;
      })
      .join("");
}

/**
 * Account chips behave like a multi-select: from "All", clicking an account
 * isolates it; further clicks add or remove accounts. Removing the last one, or
 * selecting every account, falls back to "All".
 */
function toggleAccount(id) {
  const ids = state.subscriptions.map((s) => String(s.id));
  if (id === "all") {
    state.selectedSubIds = null;
  } else {
    const key = String(id);
    if (!state.selectedSubIds) {
      state.selectedSubIds = new Set([key]);
    } else if (state.selectedSubIds.has(key)) {
      state.selectedSubIds.delete(key);
    } else {
      state.selectedSubIds.add(key);
    }
    if (state.selectedSubIds && (state.selectedSubIds.size === 0 || state.selectedSubIds.size === ids.length)) {
      state.selectedSubIds = null;
    }
  }
  renderAccountChips(state.subscriptions);
  applyFilters();
}

async function loadEvents() {
  try {
    let url = "/api/events?limit=50";
    const idsParam = selectedIdsParam();
    if (idsParam) {
      url += `&subscription_ids=${idsParam}`;
    }
    const dateStr = getSelectedDateString();
    if (dateStr) {
      url += `&date=${dateStr}`;
    }

    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const events = await res.json();
    state.events = events;
    renderEvents(events);
  } catch (err) {
    console.error("Failed to load events:", err);
  }
}

function renderEvents(events) {
  const container = document.getElementById("events-list");
  const countEl = document.getElementById("events-count");
  if (!container) return;

  if (countEl) {
    countEl.textContent = `${events.length} event${events.length !== 1 ? "s" : ""}`;
  }

  if (!events || events.length === 0) {
    container.innerHTML = `<div class="empty-state">No refresh events recorded in this time window. Refreshes will automatically log here when detected.</div>`;
    return;
  }

  container.innerHTML = events
    .map((e) => {
      let icon = "⚡";
      let iconClass = "icon-5h";
      if (e.event_type === "seven_day_reset") {
        icon = "📅";
        iconClass = "icon-7d";
      } else if (e.event_type === "quota_exhausted") {
        icon = "🛑";
        iconClass = "icon-exhausted";
      }
      const color = getAccountColor(e.subscription_id);

      const d = parseIsoDate(e.timestamp);
      const timeStr = d ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "Recently";
      const dateStr = d ? d.toLocaleDateString([], { month: "short", day: "numeric" }) : "";

      return `
      <div class="event-row" style="--account: ${color.line}; --account-tint: ${color.tint}; --account-border: ${color.border};">
        <div class="event-left">
          <div class="event-icon-badge ${iconClass}">${icon}</div>
          <div class="event-details">
            <span class="event-desc">${escapeHtml(e.description)}</span>
            <span class="event-sub-label"><span class="chip-swatch"></span>${escapeHtml(e.organization_name || e.email)}</span>
          </div>
        </div>
        <div class="event-time">${dateStr} ${timeStr}</div>
      </div>
    `;
    })
    .join("");
}

async function loadSnapshotsAndRenderChart(showLoading = true) {
  try {
    let url = "/api/snapshots?";
    const params = [];

    const idsParam = selectedIdsParam();
    if (idsParam) {
      params.push(`subscription_ids=${idsParam}`);
    }

    const dateStr = getSelectedDateString();
    if (dateStr) {
      params.push(`date=${dateStr}`);
    }

    if (state.startHour !== null) {
      params.push(`start_hour=${state.startHour}`);
    }
    if (state.endHour !== null) {
      params.push(`end_hour=${state.endHour}`);
    }

    url += params.join("&");

    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const snapshots = await res.json();
    renderChart(snapshots);
  } catch (err) {
    console.error("Failed to load snapshots:", err);
  }
}

function renderChart(snapshots) {
  const ctx = document.getElementById("quotaChart");
  if (!ctx) return;

  // Group snapshots by subscription_id
  const grouped = {};
  snapshots.forEach((snap) => {
    const subId = snap.subscription_id;
    if (!grouped[subId]) grouped[subId] = [];
    grouped[subId].push(snap);
  });

  const isRemaining = state.displayMode === "remaining";
  const datasets = [];

  const subMap = {};
  state.subscriptions.forEach((s) => {
    subMap[s.id] = s;
  });

  // Render accounts in a stable order so the legend never reshuffles.
  const orderedIds = Object.keys(grouped).sort((a, b) => Number(a) - Number(b));
  for (const subId of orderedIds) {
    const snaps = grouped[subId];
    const sub = subMap[subId] || { id: subId, organization_name: `Account #${subId}` };
    const color = getAccountColor(sub);
    const name = accountName(sub);

    // Build { x: Date, y: value } data points for time scale
    const fiveHourData = [];
    const sevenDayData = [];

    snaps.forEach((s) => {
      const d = parseIsoDate(s.timestamp);
      if (!d) return;

      if (s.five_hour_pct != null) {
        fiveHourData.push({
          x: d,
          y: isRemaining ? Math.max(0, 100 - s.five_hour_pct) : s.five_hour_pct,
        });
      }
      if (s.seven_day_pct != null) {
        sevenDayData.push({
          x: d,
          y: isRemaining ? Math.max(0, 100 - s.seven_day_pct) : s.seven_day_pct,
        });
      }
    });

    // Same hue for both quotas of an account; line style tells them apart.
    datasets.push({
      label: `${name} · 5-hour`,
      data: fiveHourData,
      borderColor: color.line,
      backgroundColor: color.fill,
      borderWidth: 2.5,
      tension: 0.35,
      pointRadius: 2.5,
      pointHoverRadius: 6,
      pointBackgroundColor: color.line,
      pointBorderColor: "#0f131d",
      pointBorderWidth: 1,
      fill: true,
      spanGaps: true,
    });

    datasets.push({
      label: `${name} · 7-day`,
      data: sevenDayData,
      borderColor: color.soft,
      backgroundColor: "transparent",
      borderWidth: 2,
      borderDash: [6, 4],
      tension: 0.35,
      pointRadius: 0,
      pointHoverRadius: 5,
      pointBackgroundColor: color.soft,
      fill: false,
      spanGaps: true,
    });
  }

  // One marker per detected event, in the owning account's hue. Labels are
  // staggered per account so simultaneous resets on two accounts both stay legible.
  const annotations = {};
  if (state.events && state.events.length > 0) {
    const accountRank = {};
    orderedIds.forEach((id, i) => { accountRank[id] = i; });
    state.subscriptions.forEach((sub, i) => {
      if (accountRank[String(sub.id)] == null) accountRank[String(sub.id)] = i;
    });

    state.events.forEach((ev, idx) => {
      const d = parseIsoDate(ev.timestamp);
      if (!d) return;
      const color = getAccountColor(ev.subscription_id);
      const name = accountName(subMap[ev.subscription_id] || { organization_name: ev.organization_name || ev.email });
      const isWeekly = ev.event_type.includes("seven");
      const isExhausted = ev.event_type === "quota_exhausted";
      const kind = isWeekly ? "weekly reset" : isExhausted ? "exhausted" : "5h reset";
      const rank = accountRank[String(ev.subscription_id)] || 0;

      annotations[`event_line_${idx}`] = {
        type: "line",
        xMin: d,
        xMax: d,
        borderColor: isExhausted ? "#ef4444" : color.line,
        borderWidth: 2,
        borderDash: isWeekly ? [2, 4] : [4, 4],
        label: {
          display: true,
          content: `${name} · ${kind}`,
          position: "end",
          yAdjust: 8 + rank * 24,
          backgroundColor: "rgba(15, 19, 29, 0.92)",
          color: isExhausted ? "#fca5a5" : color.soft,
          borderColor: isExhausted ? "rgba(239, 68, 68, 0.5)" : color.border,
          borderWidth: 1,
          padding: { x: 6, y: 3 },
          borderRadius: 4,
          font: { size: 10, weight: "bold" },
        },
      };
    });
  }

  if (state.chart) {
    state.chart.destroy();
  }

  // The axis spans the selected window, not just the range the data happens to
  // cover — otherwise every timeframe renders identically when all the samples
  // sit in one short burst. "All Time" has no window, so it fits the data.
  const bounds = getWindowBounds();
  let timeUnit;
  if (bounds) {
    timeUnit = pickTimeUnit(bounds.max.getTime() - bounds.min.getTime());
  } else if (snapshots.length >= 2) {
    const first = parseIsoDate(snapshots[0].timestamp);
    const last = parseIsoDate(snapshots[snapshots.length - 1].timestamp);
    timeUnit = first && last ? pickTimeUnit(Math.abs(last.getTime() - first.getTime())) : "minute";
  } else {
    timeUnit = "minute";
  }

  state.chart = new Chart(ctx, {
    type: "line",
    data: {
      datasets: datasets.length > 0 ? datasets : [{ label: "No Activity", data: [] }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: "nearest",
        intersect: false,
      },
      plugins: {
        legend: {
          position: "top",
          labels: {
            color: "#cbd5e1",
            boxWidth: 26,
            boxHeight: 2,
            padding: 16,
            font: { size: 12, family: "-apple-system, Inter, sans-serif" },
          },
        },
        tooltip: {
          backgroundColor: "rgba(15, 23, 42, 0.95)",
          titleColor: "#ffffff",
          bodyColor: "#f1f5f9",
          borderColor: "rgba(255, 255, 255, 0.12)",
          borderWidth: 1,
          padding: 12,
          boxPadding: 6,
          usePointStyle: true,
          callbacks: {
            title: function (items) {
              if (!items.length) return "";
              const d = new Date(items[0].parsed.x);
              return d.toLocaleString([], {
                month: "short", day: "numeric",
                hour: "2-digit", minute: "2-digit",
              });
            },
            label: function (context) {
              const label = context.dataset.label || "";
              const val = context.parsed.y;
              return ` ${label}: ${val != null ? val.toFixed(1) + "%" : "N/A"}`;
            },
          },
        },
        annotation: {
          annotations: annotations,
        },
      },
      scales: {
        x: {
          type: "time",
          min: bounds ? bounds.min.getTime() : undefined,
          max: bounds ? bounds.max.getTime() : undefined,
          time: {
            unit: timeUnit,
            displayFormats: {
              minute: "hh:mm a",
              hour: "hh:mm a",
              day: "MMM d",
            },
            tooltipFormat: "MMM d, hh:mm a",
          },
          grid: {
            color: "rgba(255, 255, 255, 0.04)",
          },
          ticks: {
            color: "#64748b",
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: 14,
            font: { size: 11 },
          },
        },
        y: {
          min: 0,
          max: 100,
          grid: {
            color: "rgba(255, 255, 255, 0.05)",
          },
          ticks: {
            color: "#64748b",
            font: { size: 11 },
            callback: function (val) {
              return val + "%";
            },
          },
          title: {
            display: true,
            text: isRemaining ? "Remaining Quota %" : "Quota Used %",
            color: "#64748b",
            font: { size: 12, weight: 600 },
          },
        },
      },
    },
  });
}

function clampHour(rawValue, fallback) {
  const parsed = parseInt(rawValue, 10);
  if (isNaN(parsed)) return fallback;
  return Math.min(23, Math.max(0, parsed));
}

function applyFilters() {
  const dateInput = document.getElementById("filter-date");
  const startHourInput = document.getElementById("filter-start-hour");
  const endHourInput = document.getElementById("filter-end-hour");

  // An empty date box means "All Time"; the picker and the pills are one filter.
  // Today's date is kept as the "today" sentinel so a long-running tab follows
  // the clock past midnight instead of pinning to yesterday.
  if (dateInput) {
    if (!dateInput.value) {
      state.selectedDate = "all";
    } else if (dateInput.value === getLocalDateString(new Date())) {
      state.selectedDate = "today";
    } else {
      state.selectedDate = dateInput.value;
    }
  }

  if (startHourInput) {
    state.startHour = clampHour(startHourInput.value, 0);
    startHourInput.value = state.startHour;
  }
  if (endHourInput) {
    state.endHour = clampHour(endHourInput.value, 23);
    // An inverted range would filter out everything, so pin the end to the start.
    if (state.endHour < state.startHour) state.endHour = state.startHour;
    endHourInput.value = state.endHour;
  }

  syncFilterButtons();
  renderWindowLabel();
  saveViewState();

  loadEvents();
  loadSnapshotsAndRenderChart();
  loadUsage();
}

function setFilterDate(mode) {
  const dateInput = document.getElementById("filter-date");
  if (dateInput) {
    dateInput.value = mode === "today" ? getLocalDateString(new Date()) : "";
  }
  applyFilters();
}

function setHours(start, end) {
  const startInput = document.getElementById("filter-start-hour");
  const endInput = document.getElementById("filter-end-hour");

  if (startInput) startInput.value = start;
  if (endInput) endInput.value = end;

  applyFilters();
}

function setDisplayMode(mode) {
  state.displayMode = mode;
  const usedBtn = document.getElementById("mode-used");
  const remBtn = document.getElementById("mode-remaining");

  if (mode === "used") {
    if (usedBtn) usedBtn.classList.add("active");
    if (remBtn) remBtn.classList.remove("active");
  } else {
    if (usedBtn) usedBtn.classList.remove("active");
    if (remBtn) remBtn.classList.add("active");
  }
  saveViewState();
  loadSnapshotsAndRenderChart();
}

async function forceRefresh() {
  const btn = document.getElementById("refresh-btn");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="btn-icon">⏳</span> Polling...`;
  }
  try {
    const res = await fetch("/api/refresh", { method: "POST" });
    if (res.ok) {
      await loadSubscriptions();
      await loadEvents();
      await loadSnapshotsAndRenderChart();
    }
  } catch (err) {
    console.error("Manual refresh failed:", err);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<span class="btn-icon">↻</span> Poll Now`;
    }
  }
}

function formatCountdown(isoString) {
  const target = parseIsoDate(isoString);
  if (!target) return "Unknown";
  try {
    const diffMs = target.getTime() - Date.now();
    if (diffMs <= 0) return "Refreshing now";

    const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
    const diffMins = Math.floor((diffMs % (1000 * 60 * 60)) / (1000 * 60));
    const timeStr = target.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

    if (diffHours >= 24) {
      const days = Math.floor(diffHours / 24);
      const remHours = diffHours % 24;
      return `in ${days}d ${remHours}h (${timeStr})`;
    }
    return `in ${diffHours}h ${diffMins}m (${timeStr})`;
  } catch {
    return "Unknown";
  }
}

function escapeHtml(text) {
  if (!text) return "";
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
