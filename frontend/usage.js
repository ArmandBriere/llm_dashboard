/**
 * Claude Code usage insights: model mix, 5-hour window attribution, cache
 * efficiency, activity timeline, weekday/hour heatmap, projects, skills,
 * plugins, tools and sessions.
 * Data comes from /api/usage/* (parsed local transcripts) and follows the same
 * account / date / hour filters as the quota chart. Depends on app.js for
 * state, account colours and helpers.
 */

const usageState = {
  timelineChart: null,
  // T3 Code and Claude Code spawn one-turn helper sessions (titles, branch
  // names). They are real usage but bury the sessions you actually worked in.
  hideHelperSessions: true,
};

function toggleHelperSessions(checked) {
  usageState.hideHelperSessions = !!checked;
  // The filter is client-side, so this re-renders from the cached payload.
  loadSessionsView(false);
}

function isHelperSession(s) {
  return (s.turns || 0) <= 1;
}

/**
 * One hue per model family, taken from the active theme. Fable is violet, Opus
 * cyan, Sonnet amber, Haiku green; a point release takes a lighter tint of the
 * same hue so the family still reads as one thing at a glance. Models the theme
 * has no slot for take the spare hues, assigned once and kept.
 */
const MODEL_FAMILY_SLOT = {
  "claude-fable-5-1": "fable",
  "claude-fable-5": "fableAlt",
  "claude-mythos-5-1": "fable",
  "claude-opus-5": "opus",
  "claude-opus-4-8": "opusAlt",
  "claude-opus-4-5": "opusAlt",
  "claude-sonnet-5": "sonnet",
  "claude-sonnet-4-5": "sonnetAlt",
  "claude-haiku-4-5-20251001": "haiku",
};
const modelSpareSlot = new Map();

function modelColor(model) {
  const palette = tokens().models;
  const slot = MODEL_FAMILY_SLOT[model];
  if (slot && palette[slot]) return palette[slot];

  const m = String(model || "").toLowerCase();
  if (m.includes("fable") || m.includes("mythos")) return palette.fable;
  if (m.includes("opus")) return palette.opus;
  if (m.includes("sonnet")) return palette.sonnet;
  if (m.includes("haiku")) return palette.haiku;

  if (!modelSpareSlot.has(model)) {
    modelSpareSlot.set(model, modelSpareSlot.size % palette.spare.length);
  }
  return palette.spare[modelSpareSlot.get(model)];
}

// ---------- formatting ----------

function fmtTokens(n) {
  const v = Number(n) || 0;
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(v >= 1e8 ? 0 : 1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(v >= 1e5 ? 0 : 1)}k`;
  return String(Math.round(v));
}

function fmtInt(n) {
  return (Number(n) || 0).toLocaleString();
}

function fmtUSD(n) {
  const v = Number(n) || 0;
  if (v >= 1000) return `$${(v / 1000).toFixed(1)}k`;
  if (v >= 100) return `$${v.toFixed(0)}`;
  if (v >= 1) return `$${v.toFixed(2)}`;
  return `$${v.toFixed(3)}`;
}

function fmtPct(v, digits = 1) {
  return `${(Number(v) || 0).toFixed(digits)}%`;
}

function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  if (h < 24) return rem ? `${h}h ${rem}m` : `${h}h`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

function fmtTime(iso) {
  const d = parseIsoDate(iso);
  return d ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "–";
}

function fmtDateTime(iso) {
  const d = parseIsoDate(iso);
  if (!d) return "–";
  const today = getLocalDateString(new Date()) === getLocalDateString(d);
  return today
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fmtRelative(iso) {
  const d = parseIsoDate(iso);
  if (!d) return "";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  return `${Math.round(diff / 86400)} d ago`;
}

function weightOf(row) {
  return state.usageWeight === "tokens" ? Number(row.total_tokens) || 0 : Number(row.est_cost_usd) || 0;
}

function fmtWeight(v) {
  return state.usageWeight === "tokens" ? fmtTokens(v) : fmtUSD(v);
}

function weightLabel() {
  return state.usageWeight === "tokens" ? "tokens" : "est. cost";
}

function subById(id) {
  return state.subscriptions.find((s) => String(s.id) === String(id)) || null;
}

function accountSwatch(subId) {
  const sub = subById(subId);
  const color = getAccountColor(sub || subId);
  const name = sub ? accountName(sub) : "Unmapped";
  return `<span class="acct-tag" style="--account:${color.line}; --account-tint:${color.tint}">${escapeHtml(name)}</span>`;
}

function modelChip(model, label) {
  return `<span class="model-chip" style="--model:${modelColor(model)}">${escapeHtml(label || model)}</span>`;
}

// ---------- controls ----------

function syncUsageControls() {
  const costBtn = document.getElementById("usage-weight-cost");
  const tokBtn = document.getElementById("usage-weight-tokens");
  if (costBtn) costBtn.classList.toggle("active", state.usageWeight !== "tokens");
  if (tokBtn) tokBtn.classList.toggle("active", state.usageWeight === "tokens");
}

function setUsageWeight(mode) {
  state.usageWeight = mode === "tokens" ? "tokens" : "cost";
  syncUsageControls();
  saveViewState();
  // Both weightings ship in every row, so this is a re-render, not a refetch.
  refreshCurrentView();
}

// ---------- loading ----------

function usageQuery(extra = {}) {
  const params = new URLSearchParams();
  const ids = selectedIdsParam();
  if (ids) params.set("subscription_ids", ids);
  const dateStr = getSelectedDateString();
  if (dateStr) {
    params.set("date", dateStr);
    // Hours only make sense inside a single day.
    params.set("start_hour", state.startHour);
    params.set("end_hour", state.endHour);
  }
  Object.entries(extra).forEach(([k, v]) => params.set(k, v));
  return params.toString();
}

/**
 * Per-view loaders, wired to the routes in nav.js. Each both fetches and
 * renders, so a re-render off the cache is the same call with force unset.
 * Fetches go through cachedJson() in app.js, keyed by URL.
 */

async function loadUsageSummary(force) {
  const summary = await cachedJson(`/api/usage/summary?${usageQuery()}`, force);
  renderUsageMeta(summary);
  renderUsageStats(summary);
}

async function loadModelsView(force) {
  const [models, summary] = await Promise.all([
    cachedJson(`/api/usage/models?${usageQuery()}`, force),
    cachedJson(`/api/usage/summary?${usageQuery()}`, force),
  ]);
  renderModelMix(models);
  renderCache(summary);
}

async function loadActivityView(force) {
  // Hours only exist inside a single day; "all time" buckets by day instead.
  const bucket = getSelectedDateString() ? "hour" : "day";
  const [timeline, heatmap] = await Promise.all([
    cachedJson(`/api/usage/timeline?${usageQuery({ bucket })}`, force),
    cachedJson(`/api/usage/heatmap?${usageQuery()}`, force),
  ]);
  renderTimeline(timeline, bucket);
  renderHeatmap(heatmap);
}

async function loadWindowsView(force) {
  renderWindows(await cachedJson(`/api/usage/windows?${usageQuery({ limit: 12 })}`, force));
}

async function loadProjectsView(force) {
  renderProjects(await cachedJson(`/api/usage/projects?${usageQuery({ limit: 12 })}`, force));
}

async function loadSessionsView(force) {
  renderSessions(await cachedJson(`/api/usage/sessions?${usageQuery({ limit: 80 })}`, force));
}

async function loadSkillsView(force) {
  renderSkills(await cachedJson(`/api/usage/skills?${usageQuery()}`, force));
}

// Plugins are rolled up from the same payload the skills view draws, so the two
// share a cache entry and the second of them costs nothing.
async function loadPluginsView(force) {
  renderPlugins(await cachedJson(`/api/usage/skills?${usageQuery()}`, force));
}

async function loadToolsView(force) {
  renderTools(await cachedJson(`/api/usage/tools?${usageQuery({ limit: 14 })}`, force));
}

// ---------- meta + stats ----------

function scopeLabel() {
  const dateStr = getSelectedDateString();
  if (!dateStr) return "all time";
  const today = dateStr === getLocalDateString(new Date());
  const hours = state.startHour === 0 && state.endHour === 23
    ? ""
    : `, ${formatHourLabel(state.startHour)} – ${formatHourLabel(state.endHour + 1)}`;
  return `${today ? "today" : dateStr}${hours}`;
}

function renderUsageMeta(summary) {
  const el = document.getElementById("usage-meta");
  if (!el) return;
  const parts = [];
  if (summary.files_indexed) parts.push(`${fmtInt(summary.files_indexed)} transcripts indexed`);
  if (summary.last_scanned_at) parts.push(`scanned ${fmtRelative(summary.last_scanned_at)}`);
  if (summary.first_turn) {
    const first = parseIsoDate(summary.first_turn);
    if (first) parts.push(`history since ${first.toLocaleDateString([], { month: "short", day: "numeric" })}`);
  }
  el.textContent = parts.join(" · ");
}

function statTile(label, value, hint, extraClass = "") {
  return `
    <div class="stat ${extraClass}">
      <div class="stat-tile-label">${escapeHtml(label)}</div>
      <div class="stat-tile-value">${value}</div>
      ${hint ? `<div class="stat-tile-hint">${hint}</div>` : ""}
    </div>`;
}

function renderUsageStats(summary) {
  const el = document.getElementById("usage-stats");
  if (!el) return;
  if (!summary || !summary.turns) {
    el.innerHTML = `<div class="empty-state">No Claude Code activity for ${escapeHtml(scopeLabel())}.</div>`;
    return;
  }
  const perAccount = (summary.per_account || []).map((a) => {
    const sub = subById(a.subscription_id);
    const color = getAccountColor(sub || a.subscription_id);
    return `<span class="stat-split" style="--account:${color.line}">${escapeHtml(sub ? accountName(sub) : a.account_key)} ${fmtWeight(weightOf(a))}</span>`;
  }).join("");
  const outputShare = summary.total_tokens ? (summary.output_tokens / summary.total_tokens) * 100 : 0;
  const thinkShare = summary.output_tokens ? (summary.thinking_tokens / summary.output_tokens) * 100 : 0;
  const perSession = summary.sessions ? summary.total_tokens / summary.sessions : 0;

  el.innerHTML = [
    statTile("Sessions", fmtInt(summary.sessions), `${fmtInt(summary.turns)} model turns · ${fmtInt(summary.sidechain_turns)} by subagents`),
    statTile("Tokens processed", fmtTokens(summary.total_tokens), `${fmtTokens(perSession)} per session · ${fmtPct(outputShare)} output`),
    statTile("Output tokens", fmtTokens(summary.output_tokens), `${fmtPct(thinkShare, 0)} of it thinking`),
    statTile("Cache hit rate", fmtPct(summary.cache_hit_ratio * 100, 1), `${fmtTokens(summary.cache_read_tokens)} read from cache`),
    statTile("Tool calls", fmtInt(summary.tool_calls), `${(summary.turns ? summary.tool_calls / summary.turns : 0).toFixed(1)} per turn`),
    statTile("Est. API cost", fmtUSD(summary.est_cost_usd), perAccount || "what this would cost on pay-as-you-go", "stat-wide"),
  ].join("");
}

// ---------- 5-hour windows ----------

function renderWindows(windows) {
  const el = document.getElementById("usage-windows");
  if (!el) return;
  if (!windows || !windows.length) {
    el.innerHTML = `<div class="empty-state">No 5-hour windows observed for ${escapeHtml(scopeLabel())}. Windows appear once the poller has seen a reset deadline.</div>`;
    return;
  }
  const byCost = state.usageWeight !== "tokens";
  el.innerHTML = windows.map((w) => {
    const sub = subById(w.subscription_id);
    const color = getAccountColor(sub || w.subscription_id);
    const shareKey = byCost ? "window_pct_by_cost" : "window_pct_by_tokens";
    const models = (w.models || []).slice().sort((a, b) => b[shareKey] - a[shareKey]);
    rememberModelLabels(models);
    const segments = models.map((m) => `<div class="win-seg" style="width:${Math.max(0, Math.min(100, m[shareKey]))}%; background:${modelColor(m.model)}" title="${escapeHtml(m.model_label)} · ${fmtPct(m[shareKey])} of the window"></div>`).join("");
    const unattributed = w.peak_pct > 0 && !models.length
      ? `<div class="win-seg win-seg-unknown" style="width:${w.peak_pct}%" title="Utilisation with no matching local turns"></div>`
      : "";
    const rows = models.map((m) => {
      const share = byCost ? m.share_cost : m.share_tokens;
      return `
        <div class="win-row">
          <span class="dot" style="background:${modelColor(m.model)}"></span>
          <span class="win-model">${escapeHtml(m.model_label)}</span>
          <span class="win-pct">${fmtPct(m[shareKey])}<small> of window</small></span>
          <span class="win-detail">${fmtPct(share * 100, 0)} of ${weightLabel()} · ${fmtInt(m.turns)} turns · ${fmtTokens(m.total_tokens)} tok</span>
        </div>`;
    }).join("");
    const status = w.is_active
      ? `<span class="win-status win-status-live">live · ${fmtPct(w.peak_pct, 0)} so far</span>`
      : `<span class="win-status">peaked at ${fmtPct(w.peak_pct, 0)}</span>`;
    const dateStr = getSelectedDateString();
    const startLabel = dateStr ? fmtTime(w.starts_at) : fmtDateTime(w.starts_at);
    return `
      <div class="win" style="--account:${color.line}">
        <div class="win-head">
          <div class="win-title">
            ${accountSwatch(w.subscription_id)}
            <span class="win-range">${startLabel} → ${fmtTime(w.resets_at)}</span>
          </div>
          ${status}
        </div>
        <div class="win-bar" title="Peak 5-hour utilisation ${fmtPct(w.peak_pct, 0)}">
          ${segments}${unattributed}
        </div>
        <div class="win-rows">
          ${rows || `<div class="win-row win-row-empty">No local transcript turns fall inside this window.</div>`}
        </div>
        <div class="win-foot">${fmtInt(w.sessions)} sessions · ${fmtInt(w.turns)} turns · ${fmtTokens(w.total_tokens)} tokens · ${fmtUSD(w.est_cost_usd)} est. · ${fmtInt(w.readings)} readings</div>
      </div>`;
  }).join("");
}

// ---------- model mix ----------

function renderModelMix(models) {
  rememberModelLabels(models);
  const bar = document.getElementById("usage-model-bar");
  const table = document.getElementById("usage-model-table");
  const caption = document.getElementById("usage-model-caption");
  if (!bar || !table) return;
  if (!models || !models.length) {
    bar.innerHTML = "";
    table.innerHTML = `<div class="empty-state">No turns in this range.</div>`;
    if (caption) caption.textContent = "";
    return;
  }
  const total = models.reduce((acc, m) => acc + weightOf(m), 0) || 1;
  const sorted = models.slice().sort((a, b) => weightOf(b) - weightOf(a));
  bar.innerHTML = sorted.map((m) => {
    const pct = (weightOf(m) / total) * 100;
    return `<div class="share-seg" style="width:${pct}%; background:${modelColor(m.model)}" title="${escapeHtml(m.model_label)} · ${fmtPct(pct)}"></div>`;
  }).join("");
  if (caption) caption.textContent = `Share of ${weightLabel()} · ${sorted[0].model_label} leads at ${fmtPct((weightOf(sorted[0]) / total) * 100, 0)}`;
  table.innerHTML = `
    <table class="usage-table">
      <thead><tr><th>Model</th><th class="num">Share</th><th class="num">Turns</th><th class="num">Sessions</th><th class="num">Tokens</th><th class="num">Output</th><th class="num">Est. cost</th></tr></thead>
      <tbody>
        ${sorted.map((m) => `
          <tr>
            <td>${modelChip(m.model, m.model_label)}</td>
            <td class="num strong">${fmtPct((weightOf(m) / total) * 100)}</td>
            <td class="num">${fmtInt(m.turns)}</td>
            <td class="num">${fmtInt(m.sessions)}</td>
            <td class="num">${fmtTokens(m.total_tokens)}</td>
            <td class="num">${fmtTokens(m.output_tokens)}</td>
            <td class="num">${fmtUSD(m.est_cost_usd)}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

// ---------- cache ----------

function renderCache(summary) {
  const el = document.getElementById("usage-cache");
  if (!el) return;
  if (!summary || !summary.total_tokens) {
    el.innerHTML = `<div class="empty-state">No turns in this range.</div>`;
    return;
  }
  const palette = tokens().models;
  const rows = [
    { label: "Cache reads", value: summary.cache_read_tokens, color: palette.opus, hint: "Context replayed from cache (cheapest)" },
    { label: "Cache writes", value: summary.cache_creation_tokens, color: palette.opusAlt, hint: `${fmtTokens(summary.cache_1h_tokens)} with 1h TTL · ${fmtTokens(summary.cache_5m_tokens)} with 5m TTL` },
    { label: "Fresh input", value: summary.input_tokens, color: palette.sonnet, hint: "Uncached prompt tokens" },
    { label: "Output", value: summary.output_tokens, color: palette.fable, hint: `${fmtTokens(summary.thinking_tokens)} thinking` },
  ];
  const total = rows.reduce((a, r) => a + (Number(r.value) || 0), 0) || 1;
  el.innerHTML = `
    <div class="share-bar">
      ${rows.map((r) => `<div class="share-seg" style="width:${(r.value / total) * 100}%; background:${r.color}" title="${escapeHtml(r.label)} · ${fmtPct((r.value / total) * 100)}"></div>`).join("")}
    </div>
    <div class="cache-rows">
      ${rows.map((r) => `
        <div class="cache-row">
          <span class="dot" style="background:${r.color}"></span>
          <span class="cache-label">${escapeHtml(r.label)}</span>
          <span class="cache-value">${fmtTokens(r.value)}</span>
          <span class="cache-pct">${fmtPct((r.value / total) * 100)}</span>
          <span class="cache-hint">${escapeHtml(r.hint)}</span>
        </div>`).join("")}
    </div>
    <div class="cache-summary">
      Each turn resent on average <strong>${fmtTokens(summary.turns ? summary.total_input_tokens / summary.turns : 0)}</strong> tokens of context and produced <strong>${fmtTokens(summary.turns ? summary.output_tokens / summary.turns : 0)}</strong>.
      ${summary.cache_hit_ratio >= 0.9 ? "Caching is doing its job." : summary.cache_hit_ratio >= 0.7 ? "Decent caching; long pauses between turns let the cache expire." : "Low cache reuse: many turns are paying full price for their context."}
    </div>`;
}

// ---------- timeline ----------

function renderTimeline(rows, bucket) {
  rememberModelLabels(rows);
  const canvas = document.getElementById("usageTimelineChart");
  const caption = document.getElementById("usage-timeline-caption");
  if (!canvas) return;
  if (usageState.timelineChart) {
    usageState.timelineChart.destroy();
    usageState.timelineChart = null;
  }
  const byBucket = new Map();
  const modelsSeen = new Map();
  (rows || []).forEach((r) => {
    if (!byBucket.has(r.bucket)) byBucket.set(r.bucket, {});
    byBucket.get(r.bucket)[r.model] = weightOf(r);
    modelsSeen.set(r.model, (modelsSeen.get(r.model) || 0) + weightOf(r));
  });

  // Fill empty buckets so quiet hours/days show as gaps rather than vanishing.
  let labels = Array.from(byBucket.keys()).sort();
  if (bucket === "hour") {
    const dateStr = getSelectedDateString();
    if (dateStr) {
      labels = [];
      for (let h = state.startHour; h <= state.endHour; h += 1) {
        labels.push(`${dateStr}T${String(h).padStart(2, "0")}:00:00`);
      }
    }
  } else if (labels.length > 1) {
    const filled = [];
    const start = new Date(`${labels[0]}T00:00:00`);
    const end = new Date(`${labels[labels.length - 1]}T00:00:00`);
    for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) filled.push(getLocalDateString(d));
    labels = filled;
  }

  const models = Array.from(modelsSeen.entries()).sort((a, b) => b[1] - a[1]).map(([m]) => m);
  if (!models.length) {
    if (caption) caption.textContent = `Nothing recorded for ${scopeLabel()}`;
    return;
  }
  if (caption) caption.textContent = `${weightLabel()} per ${bucket}, stacked by model`;

  const labelFor = (b) => {
    if (bucket === "hour") {
      const h = parseInt(b.slice(11, 13), 10);
      return formatHourLabel(h);
    }
    const d = new Date(`${b}T00:00:00`);
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  };

  const isTokens = state.usageWeight === "tokens";
  const t = tokens();
  usageState.timelineChart = new Chart(canvas.getContext("2d"), {
    type: "bar",
    data: {
      labels: labels.map(labelFor),
      datasets: models.map((m) => ({
        label: pretty(m),
        data: labels.map((b) => (byBucket.get(b) || {})[m] || 0),
        backgroundColor: modelColor(m),
        borderRadius: 0,
        stack: "usage",
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 250 },
      plugins: {
        legend: { position: "bottom", labels: { color: t.ink2, boxWidth: 10, boxHeight: 10, padding: 14, font: { size: 10, family: t.fontMono } } },
        tooltip: {
          backgroundColor: t.surfaceRaised,
          titleColor: t.ink,
          bodyColor: t.ink2,
          footerColor: t.ink,
          borderColor: t.rule,
          borderWidth: 1,
          cornerRadius: 2,
          titleFont: { family: t.fontMono, size: 11 },
          bodyFont: { family: t.fontMono, size: 11 },
          footerFont: { family: t.fontMono, size: 11 },
          callbacks: {
            label: (ctx) => ` ${ctx.dataset.label}: ${isTokens ? fmtTokens(ctx.parsed.y) : fmtUSD(ctx.parsed.y)}`,
            footer: (items) => {
              const total = items.reduce((a, i) => a + (i.parsed.y || 0), 0);
              return `Total: ${isTokens ? fmtTokens(total) : fmtUSD(total)}`;
            },
          },
        },
      },
      scales: {
        x: { stacked: true, border: { color: t.rule }, grid: { display: false }, ticks: { color: t.ink3, font: { size: 10, family: t.fontMono }, maxRotation: 0, autoSkip: true } },
        y: {
          stacked: true,
          beginAtZero: true,
          border: { color: t.rule },
          grid: { color: t.ruleFaint },
          ticks: { color: t.ink3, font: { size: 10, family: t.fontMono }, callback: (v) => (isTokens ? fmtTokens(v) : fmtUSD(v)) },
        },
      },
    },
  });
}

/**
 * Display labels for model ids, learned from whichever rows have been rendered.
 * Views load independently now, so a label seen in one is remembered for the
 * others rather than re-derived from a single shared payload.
 */
const PRETTY_CACHE = new Map();

function rememberModelLabels(rows) {
  (rows || []).forEach((r) => {
    if (r && r.model && r.model_label) PRETTY_CACHE.set(r.model, r.model_label);
  });
}

function pretty(model) {
  return PRETTY_CACHE.get(model) || model;
}

// ---------- heatmap ----------

function renderHeatmap(cells) {
  const el = document.getElementById("usage-heatmap");
  const caption = document.getElementById("usage-heatmap-caption");
  if (!el) return;
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const grid = Array.from({ length: 7 }, () => Array(24).fill(0));
  let max = 0;
  let busiest = null;
  (cells || []).forEach((c) => {
    const v = weightOf(c);
    grid[c.weekday][c.hour] = v;
    if (v > max) {
      max = v;
      busiest = c;
    }
  });
  if (!max) {
    el.innerHTML = `<div class="empty-state">No activity to map.</div>`;
    if (caption) caption.textContent = "";
    return;
  }
  if (caption && busiest) caption.textContent = `Busiest: ${days[busiest.weekday]} ${formatHourLabel(busiest.hour)} · by ${weightLabel()}`;

  // Monday-first display order.
  const order = [1, 2, 3, 4, 5, 6, 0];
  const heat = tokens().heatRgb;
  const hourHeader = Array.from({ length: 24 }, (_, h) => `<div class="hm-hour">${h % 3 === 0 ? formatHourLabel(h).replace(" ", "") : ""}</div>`).join("");
  const body = order.map((d) => `
    <div class="hm-day">${days[d]}</div>
    ${grid[d].map((v, h) => {
      const t = v / max;
      const a = v ? 0.12 + Math.sqrt(t) * 0.78 : 0;
      const inRange = getSelectedDateString() ? h >= state.startHour && h <= state.endHour : true;
      return `<div class="hm-cell ${inRange ? "" : "hm-cell-out"}" style="background: ${a ? `rgba(${heat[0]}, ${heat[1]}, ${heat[2]}, ${a.toFixed(3)})` : "transparent"}" title="${days[d]} ${formatHourLabel(h)} · ${fmtWeight(v)}"></div>`;
    }).join("")}`).join("");
  el.innerHTML = `<div class="hm-grid"><div class="hm-corner"></div>${hourHeader}${body}</div>`;
}

// ---------- projects ----------

function renderProjects(projects) {
  const el = document.getElementById("usage-projects");
  if (!el) return;
  if (!projects || !projects.length) {
    el.innerHTML = `<div class="empty-state">No projects in this range.</div>`;
    return;
  }
  const max = Math.max(...projects.map(weightOf)) || 1;
  el.innerHTML = `
    <table class="usage-table">
      <thead><tr><th>Project</th><th class="num">Sessions</th><th class="num">Turns</th><th class="num">Tokens</th><th class="num">Est. cost</th></tr></thead>
      <tbody>
        ${projects.map((p) => `
          <tr title="${p.directories > 1 ? `${p.directories} working directories (worktrees) collapsed into this project` : ""}">
            <td>
              <div class="proj-name">${escapeHtml(p.project_name)}${p.directories > 1 ? `<span class="proj-dirs">${p.directories} worktrees</span>` : ""}</div>
              <div class="proj-bar"><div style="width:${(weightOf(p) / max) * 100}%"></div></div>
            </td>
            <td class="num">${fmtInt(p.sessions)}</td>
            <td class="num">${fmtInt(p.turns)}</td>
            <td class="num">${fmtTokens(p.total_tokens)}</td>
            <td class="num">${fmtUSD(p.est_cost_usd)}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

// ---------- skills, plugins and tools ----------

/**
 * One hue per plugin, taken from the same theme palette as the models so the
 * page reads as one system. Assigned on first sight and kept for the session.
 */
const pluginSlot = new Map();

function pluginColor(name) {
  const palette = tokens().models;
  const wheel = [palette.opus, palette.sonnet, palette.fable, palette.haiku, palette.opusAlt, palette.sonnetAlt, palette.fableAlt, ...palette.spare];
  if (!pluginSlot.has(name)) pluginSlot.set(name, pluginSlot.size % wheel.length);
  return wheel[pluginSlot.get(name)];
}

function skillChip(row, extra = "") {
  const color = pluginColor(row.plugin || "(none)");
  const label = row.plugin ? `${row.plugin}:${row.skill_name}` : row.skill_name || row.skill;
  return `<span class="model-chip" style="--model:${color}">${escapeHtml(label)}${extra}</span>`;
}

// Where a skill came from, spelled out for the source column.
const SKILL_SOURCE_LABEL = {
  plugin: "plugin",
  personal: "personal",
  project: "project",
  bundled: "bundled",
  unknown: "—",
};

function renderSkills(data) {
  const statsEl = document.getElementById("usage-skills-stats");
  const el = document.getElementById("usage-skills");
  const caption = document.getElementById("usage-skills-caption");
  if (!el) return;
  const skills = (data && data.skills) || [];
  const totals = (data && data.totals) || {};
  if (!skills.length) {
    if (statsEl) statsEl.innerHTML = "";
    if (caption) caption.textContent = "";
    el.innerHTML = `<div class="empty-state">No skill was invoked in ${escapeHtml(scopeLabel())}.</div>`;
    return;
  }

  const attributed = totals.attributed || {};
  const unattributed = totals.unattributed || {};
  const totalWeight = weightOf(attributed) + weightOf(unattributed);
  const share = totalWeight ? (weightOf(attributed) / totalWeight) * 100 : 0;
  const perRun = totals.invocations ? weightOf(attributed) / totals.invocations : 0;

  if (caption) {
    caption.textContent = `${fmtInt(totals.invocations)} invocations of ${fmtInt(totals.distinct_skills)} skills` +
      (totals.errors ? ` · ${fmtInt(totals.errors)} failed to load` : "");
  }
  if (statsEl) {
    statsEl.innerHTML = [
      statTile("Invocations", fmtInt(totals.invocations), `${fmtInt(totals.distinct_skills)} skills from ${fmtInt(totals.distinct_plugins)} plugins`),
      statTile("Sessions with a skill", fmtInt(totals.sessions_with_skills), `of ${fmtInt(totals.sessions_total)} sessions in range`),
      statTile("Context injected", fmtTokens(totals.payload_tokens), `≈${fmtTokens(totals.invocations ? totals.payload_tokens / totals.invocations : 0)} per load, estimated`),
      statTile("Attributed work", fmtWeight(weightOf(attributed)), `${fmtPct(share, 0)} of ${weightLabel()} · ${fmtWeight(perRun)} per invocation`),
    ].join("");
  }

  const max = Math.max(...skills.map((s) => weightOf(skillWeightRow(s)))) || 1;
  el.innerHTML = `
    <table class="usage-table">
      <thead>
        <tr>
          <th>Skill</th><th>Source</th><th class="num">Runs</th><th class="num">Sessions</th>
          <th class="num">Context</th><th class="num">Turns</th><th class="num">Tokens</th><th class="num">Est. cost</th><th class="num">Per run</th>
        </tr>
      </thead>
      <tbody>
        ${skills.map((s) => {
          const w = weightOf(skillWeightRow(s));
          const version = s.latest_version
            ? ` <span class="skill-version">v${escapeHtml(s.latest_version)}${s.version_count > 1 ? ` <small>+${s.version_count - 1}</small>` : ""}</span>`
            : "";
          const failed = s.errors ? `<span class="skill-fail" title="${s.errors} invocation(s) failed to load">${s.errors} failed</span>` : "";
          return `
            <tr title="${escapeHtml(s.skill)}\nlast used ${escapeHtml(fmtDateTime(s.last_used))}">
              <td>
                <div class="skill-name">${skillChip(s)}${failed}</div>
                <div class="proj-bar"><div style="width:${(w / max) * 100}%; background:${pluginColor(s.plugin || "(none)")}"></div></div>
              </td>
              <td class="skill-source">${escapeHtml(SKILL_SOURCE_LABEL[s.source] || s.source || "—")}${version}</td>
              <td class="num strong">${fmtInt(s.invocations)}</td>
              <td class="num">${fmtInt(s.sessions)}</td>
              <td class="num" title="Estimated tokens of instructions this skill injects each time it loads">${s.payload_tokens_each ? fmtTokens(s.payload_tokens_each) : "–"}</td>
              <td class="num">${fmtInt(s.attributed_turns)}</td>
              <td class="num">${fmtTokens(s.attributed_tokens)}</td>
              <td class="num">${fmtUSD(s.attributed_cost_usd)}</td>
              <td class="num">${fmtWeight(s.invocations ? w / s.invocations : 0)}</td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>
    <p class="table-note">
      Turns, tokens and cost are the work done <em>after</em> a skill loaded: each turn counts for the
      skill most recently loaded in its transcript, so the rows partition the window rather than
      double-counting sessions that load several skills. Context is the instruction text the skill
      injects, estimated at four characters per token.
      ${unattributed.turns ? `${fmtInt(unattributed.turns)} turns (${fmtWeight(weightOf(unattributed))}) ran with no skill loaded.` : ""}
    </p>`;
}

// The attribution fields are named differently from the model/project rows, so
// weightOf() gets the shape it expects.
function skillWeightRow(s) {
  return { total_tokens: s.attributed_tokens, est_cost_usd: s.attributed_cost_usd };
}

function renderPlugins(data) {
  const el = document.getElementById("usage-plugins");
  const caption = document.getElementById("usage-plugins-caption");
  if (!el) return;
  const plugins = (data && data.plugins) || [];
  if (!plugins.length) {
    el.innerHTML = `<div class="empty-state">No plugin activity in this range.</div>`;
    if (caption) caption.textContent = "";
    return;
  }
  if (caption) caption.textContent = `skills and MCP tools, by ${weightLabel()}`;
  const max = Math.max(...plugins.map((p) => weightOf(skillWeightRow(p)))) || 1;
  el.innerHTML = `
    <table class="usage-table">
      <thead><tr><th>Plugin</th><th class="num">Skills</th><th class="num">Runs</th><th class="num">MCP calls</th><th class="num">Tokens</th><th class="num">Est. cost</th></tr></thead>
      <tbody>
        ${plugins.map((p) => {
          const w = weightOf(skillWeightRow(p));
          const servers = p.mcp_servers.map((m) => `${m.server} ×${fmtInt(m.calls)}`).join(", ");
          const meta = [
            p.latest_version ? `v${p.latest_version}` : null,
            p.marketplace,
            servers || null,
          ].filter(Boolean).join(" · ");
          return `
            <tr>
              <td>
                <div class="skill-name">
                  <span class="model-chip" style="--model:${pluginColor(p.plugin)}">${escapeHtml(p.plugin)}</span>
                </div>
                ${meta ? `<div class="sess-meta">${escapeHtml(meta)}</div>` : ""}
                <div class="proj-bar"><div style="width:${(w / max) * 100}%; background:${pluginColor(p.plugin)}"></div></div>
              </td>
              <td class="num">${fmtInt(p.skill_count)}</td>
              <td class="num strong">${fmtInt(p.invocations)}</td>
              <td class="num">${p.mcp_calls ? fmtInt(p.mcp_calls) : "–"}</td>
              <td class="num">${fmtTokens(p.attributed_tokens)}</td>
              <td class="num">${fmtUSD(p.attributed_cost_usd)}</td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}

// Tool kinds share the model palette: one hue each, fixed so the bar keeps its
// reading between refreshes.
const TOOL_KIND_SLOT = { builtin: "opus", mcp: "sonnet", skill: "fable", agent: "haiku" };
const TOOL_KIND_HINT = {
  builtin: "shipped with Claude Code",
  mcp: "from an MCP server",
  skill: "Skill tool invocations",
  agent: "subagents launched",
};

function renderTools(data) {
  const el = document.getElementById("usage-tools");
  const caption = document.getElementById("usage-tools-caption");
  if (!el) return;
  const tools = (data && data.tools) || [];
  if (!tools.length) {
    el.innerHTML = `<div class="empty-state">No tool calls in this range.</div>`;
    if (caption) caption.textContent = "";
    return;
  }
  const palette = tokens().models;
  const kinds = data.kinds || [];
  const total = data.total_calls || 1;
  if (caption) caption.textContent = `${fmtInt(total)} calls · ${tools.length} busiest tools`;

  const max = Math.max(...tools.map((t) => t.calls)) || 1;
  el.innerHTML = `
    <div class="share-bar">
      ${kinds.map((k) => `<div class="share-seg" style="width:${(k.calls / total) * 100}%; background:${palette[TOOL_KIND_SLOT[k.tool_kind]] || palette.spare[0]}" title="${escapeHtml(k.tool_kind)} · ${fmtInt(k.calls)} calls"></div>`).join("")}
    </div>
    <div class="cache-rows">
      ${kinds.map((k) => `
        <div class="cache-row">
          <span class="dot" style="background:${palette[TOOL_KIND_SLOT[k.tool_kind]] || palette.spare[0]}"></span>
          <span class="cache-label">${escapeHtml(k.tool_kind)}</span>
          <span class="cache-value">${fmtInt(k.calls)}</span>
          <span class="cache-pct">${fmtPct((k.calls / total) * 100)}</span>
          <span class="cache-hint">${escapeHtml(`${k.tools} distinct · ${TOOL_KIND_HINT[k.tool_kind] || ""}`)}</span>
        </div>`).join("")}
    </div>
    <div class="tool-list">
      ${tools.map((t) => `
        <div class="tool-row" title="${escapeHtml(t.tool_name)}${t.server ? `\nserver: ${escapeHtml(t.server)}` : ""}">
          <span class="tool-name">${escapeHtml(toolLabel(t))}</span>
          <span class="tool-bar"><i style="width:${(t.calls / max) * 100}%; background:${palette[TOOL_KIND_SLOT[t.tool_kind]] || palette.spare[0]}"></i></span>
          <span class="tool-count">${fmtInt(t.calls)}</span>
        </div>`).join("")}
    </div>`;
}

function toolLabel(t) {
  if (t.tool_kind !== "mcp") return t.tool_name;
  const short = t.tool_name.split("__").pop();
  return `${t.plugin ? `${t.plugin}/` : ""}${t.server}: ${short}`;
}

// ---------- sessions ----------

function renderSessions(sessions) {
  const el = document.getElementById("usage-sessions");
  const caption = document.getElementById("usage-sessions-caption");
  if (!el) return;
  if (!sessions || !sessions.length) {
    el.innerHTML = `<div class="empty-state">No sessions for ${escapeHtml(scopeLabel())}.</div>`;
    if (caption) caption.textContent = "";
    return;
  }
  const helpers = sessions.filter(isHelperSession).length;
  const shown = usageState.hideHelperSessions ? sessions.filter((s) => !isHelperSession(s)) : sessions;
  if (caption) {
    caption.innerHTML = `${shown.length} of the ${sessions.length} most recent sessions in ${escapeHtml(scopeLabel())}` +
      (helpers ? ` · <label class="inline-toggle"><input type="checkbox" ${usageState.hideHelperSessions ? "checked" : ""} onchange="toggleHelperSessions(this.checked)"> hide ${helpers} one-turn helper session${helpers === 1 ? "" : "s"}</label>` : "");
  }
  const dateStr = getSelectedDateString();
  if (!shown.length) {
    el.innerHTML = `<div class="empty-state">Only one-turn helper sessions in this range.</div>`;
    return;
  }
  el.innerHTML = `
    <table class="usage-table sessions-table">
      <thead>
        <tr>
          <th>Session</th><th>Account</th><th>Started</th><th class="num">Duration</th>
          <th class="num">Turns</th><th class="num">Tools</th><th class="num">Tokens</th><th class="num">Output</th><th>Models</th><th>Skills</th><th class="num">Est. cost</th>
        </tr>
      </thead>
      <tbody>
        ${shown.map((s) => {
          const title = s.title || s.first_prompt || s.session_id.slice(0, 8);
          const meta = [s.project_name, s.worktree, s.git_branch].filter(Boolean).join(" · ");
          const modelsHtml = (s.models || []).slice(0, 3).map((m) => modelChip(m.model, `${m.model_label} ×${m.turns}`)).join("");
          const skillsHtml = (s.skills || []).slice(0, 3)
            .map((k) => skillChip(k, k.invocations > 1 ? ` ×${k.invocations}` : ""))
            .join("") + ((s.skills || []).length > 3 ? `<span class="skill-more">+${s.skills.length - 3}</span>` : "");
          return `
            <tr title="${escapeHtml(s.first_prompt || "")}\n${escapeHtml(s.session_id)}">
              <td class="sess-cell">
                <div class="sess-title">${escapeHtml(title)}</div>
                <div class="sess-meta">${escapeHtml(meta)}</div>
              </td>
              <td>${accountSwatch(s.subscription_id)}</td>
              <td class="nowrap">${dateStr ? fmtTime(s.started_at) : fmtDateTime(s.started_at)}</td>
              <td class="num">${fmtDuration(s.duration_s)}</td>
              <td class="num">${fmtInt(s.turns)}</td>
              <td class="num">${fmtInt(s.tool_calls)}</td>
              <td class="num">${fmtTokens(s.total_tokens)}</td>
              <td class="num">${fmtTokens(s.output_tokens)}</td>
              <td class="models-cell">${modelsHtml}</td>
              <td class="models-cell">${skillsHtml || "<span class=\"skill-none\">–</span>"}</td>
              <td class="num">${fmtUSD(s.est_cost_usd)}</td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}
