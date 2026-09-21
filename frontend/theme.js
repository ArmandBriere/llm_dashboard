/**
 * Theme registry and switcher.
 *
 * A theme is a block of CSS custom properties under [data-theme] in style.css.
 * This module owns which one is active, persists the choice, renders the picker,
 * and exposes the resolved token values to the chart code — Chart.js paints on a
 * canvas and cannot read CSS variables itself, so anything drawn there has to
 * ask for concrete colours and repaint when the theme changes.
 */

const THEMES = [
  { id: "console", name: "Console", note: "warm graphite, ember signal" },
  { id: "abyss", name: "Abyss", note: "blue ink, seafoam signal" },
  { id: "oxide", name: "Oxide", note: "burnt plum, rust signal" },
  { id: "phosphor", name: "Phosphor", note: "true black, CRT green" },
];

const THEME_KEY = "llm-dashboard.theme.v1";
const DEFAULT_THEME = "console";

function storedTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    return THEMES.some((t) => t.id === saved) ? saved : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

let tokenCache = null;

/** Resolved values of the active theme's tokens, cached until the theme changes. */
function tokens() {
  if (tokenCache) return tokenCache;
  const cs = getComputedStyle(document.documentElement);
  const read = (name) => cs.getPropertyValue(name).trim();
  const rgb = (name) =>
    read(name)
      .split(/[\s,]+/)
      .map(Number);

  const account = (i) => ({
    key: `acct-${i}`,
    line: read(`--acct-${i}`),
    soft: read(`--acct-${i}-soft`),
    rgb: rgb(`--acct-${i}-rgb`),
  });

  tokenCache = {
    bg: read("--bg"),
    surface: read("--surface"),
    surfaceRaised: read("--surface-2"),
    ink: read("--ink"),
    ink2: read("--ink-2"),
    ink3: read("--ink-3"),
    rule: read("--rule"),
    ruleFaint: read("--rule-faint"),
    signal: read("--signal"),
    bad: read("--bad"),
    badSoft: read("--bad-soft"),
    heatRgb: rgb("--heat-rgb"),
    fontDisplay: read("--font-display"),
    fontMono: read("--font-mono"),
    accounts: [1, 2, 3, 4].map(account),
    models: {
      fable: read("--model-fable"),
      fableAlt: read("--model-fable-alt"),
      opus: read("--model-opus"),
      opusAlt: read("--model-opus-alt"),
      sonnet: read("--model-sonnet"),
      sonnetAlt: read("--model-sonnet-alt"),
      haiku: read("--model-haiku"),
      spare: [read("--model-spare-1"), read("--model-spare-2"), read("--model-spare-3")],
    },
  };
  return tokenCache;
}

/** rgba() string built from a token's stored "r g b" triplet. */
function alpha(triplet, a) {
  const [r, g, b] = triplet || [128, 128, 128];
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

function applyTheme(id, { persist = true } = {}) {
  const theme = THEMES.find((t) => t.id === id) || THEMES[0];
  document.documentElement.setAttribute("data-theme", theme.id);
  tokenCache = null;
  if (persist) {
    try {
      localStorage.setItem(THEME_KEY, theme.id);
    } catch (err) {
      console.warn("Could not save theme:", err);
    }
  }
  renderThemeMenu();
  document.dispatchEvent(new CustomEvent("themechange", { detail: { theme: theme.id } }));
}

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") || DEFAULT_THEME;
}

/**
 * The picker previews each theme with its own swatches. Rendering the swatches
 * needs each theme's variables, which only resolve under that theme's selector —
 * so the preview markup carries data-theme itself and inherits the right block.
 */
function renderThemeMenu() {
  const list = document.getElementById("theme-list");
  if (!list) return;
  const active = currentTheme();
  list.innerHTML = THEMES.map(
    (t) => `
    <button class="theme-option ${t.id === active ? "is-active" : ""}" role="menuitemradio"
            aria-checked="${t.id === active}" data-theme="${t.id}" onclick="selectTheme('${t.id}')">
      <span class="theme-swatches" aria-hidden="true">
        <span style="background: var(--bg)"></span>
        <span style="background: var(--signal)"></span>
        <span style="background: var(--acct-1)"></span>
        <span style="background: var(--acct-2)"></span>
      </span>
      <span class="theme-option-text">
        <span class="theme-option-name">${t.name}</span>
        <span class="theme-option-note">${t.note}</span>
      </span>
    </button>`,
  ).join("");

  const label = document.getElementById("theme-current");
  if (label) label.textContent = (THEMES.find((t) => t.id === active) || THEMES[0]).name;
}

function selectTheme(id) {
  applyTheme(id);
  closeThemeMenu();
}

function openThemeMenu() {
  const menu = document.getElementById("theme-menu");
  const btn = document.getElementById("theme-btn");
  if (!menu || !btn) return;
  menu.hidden = false;
  btn.setAttribute("aria-expanded", "true");
  document.addEventListener("click", onDocumentClick, true);
  document.addEventListener("keydown", onMenuKeydown);
}

function closeThemeMenu() {
  const menu = document.getElementById("theme-menu");
  const btn = document.getElementById("theme-btn");
  if (!menu || !btn) return;
  menu.hidden = true;
  btn.setAttribute("aria-expanded", "false");
  document.removeEventListener("click", onDocumentClick, true);
  document.removeEventListener("keydown", onMenuKeydown);
}

function toggleThemeMenu(event) {
  if (event) event.stopPropagation();
  const menu = document.getElementById("theme-menu");
  if (!menu) return;
  if (menu.hidden) openThemeMenu();
  else closeThemeMenu();
}

function onDocumentClick(event) {
  const wrap = document.getElementById("theme-picker");
  if (wrap && !wrap.contains(event.target)) closeThemeMenu();
}

function onMenuKeydown(event) {
  if (event.key === "Escape") {
    closeThemeMenu();
    const btn = document.getElementById("theme-btn");
    if (btn) btn.focus();
  }
}

// Applied before first paint so the page never flashes the default theme.
document.documentElement.setAttribute("data-theme", storedTheme());
document.addEventListener("DOMContentLoaded", () => renderThemeMenu());
