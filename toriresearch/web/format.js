/**
 * Pure presentation helpers for the dashboard.
 *
 * Nothing in this module may touch the DOM, `fetch`, `location` or any global
 * state. That is what makes it testable in plain Node, and it is the reason this
 * file exists separately from the view modules.
 *
 * The single most important rule enforced here: `null` and `0` render
 * differently. `null` means "not measured" and must never look like a number,
 * because a suppressed statistic rendered as `0` is indistinguishable from a
 * measured zero and that is exactly the failure this application exists to
 * prevent. See `tests/js/format.test.js`.
 */

export const DASH = '—';

/**
 * Format a number with Finnish digit grouping. `null` is never a number.
 *
 * `max` and `min` are separate because a money value needs both: without a
 * minimum, `eur(7)` renders as `7 €` and becomes visually indistinguishable from
 * a truncated `7,5 €`. Counts pass the default of 0 for both.
 */
export const num = (value, digits = 0, min = 0) =>
  value == null
    ? DASH
    : Number(value).toLocaleString('fi-FI', {
        minimumFractionDigits: min,
        maximumFractionDigits: digits,
      });

/** Format euros. Always two decimals. Absent is a dash, never `0,00 €`. */
export const eur = value => (value == null ? DASH : `${num(value, 2, 2)} €`);

/** Format a percentage already expressed in percent (not a ratio). One decimal. */
export const percent = value => (value == null ? DASH : `${num(value, 1, 1)}%`);

/**
 * A count with its unit written for that count: "1 category", not "1 categories".
 *
 * `word` is the plural, defaulting to the singular with the English rule this
 * app's units need ("category" -> "categories"), because "1 categories" is a
 * visible bug the moment a panel holds a single row. `null` counts stay a dash,
 * like every other suppressed value here.
 */
export const plural = (value, singular, word = /[^aeiou]y$/i.test(singular) ? `${singular.slice(0, -1)}ies` : `${singular}s`) =>
  value == null ? DASH : `${num(value)} ${Number(value) === 1 ? singular : word}`;

/** Escape a value for interpolation into HTML text or a quoted attribute. */
export const esc = value =>
  String(value ?? '').replace(
    /[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );

/** Operator-facing timestamp. Helsinki is the only timezone the UI speaks. */
export const date = value =>
  value
    ? new Date(value).toLocaleString('fi-FI', {
        day: '2-digit',
        month: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        timeZone: 'Europe/Helsinki',
      })
    : 'Never';

/**
 * Coarse relative age. `null` is "never observed", which is not the same as old.
 *
 * Buckets deliberately keep a variable decimal count, so a whole hour reads
 * "1 h ago" rather than "1,0 h ago". This is the one place where trailing
 * zeros would hurt readability rather than help it.
 */
export const ago = minutes =>
  minutes == null
    ? 'Never'
    : minutes < 1
      ? 'Just now'
      : minutes < 60
        ? `${num(minutes)} min ago`
        : minutes < 1440
          ? `${num(minutes / 60, 1)} h ago`
          : `${num(minutes / 1440, 1)} d ago`;

/** Human labels for the platform's own enums. Unknown codes stay visible verbatim. */
export const LABELS = {
  standard: 'Standard',
  diili: 'Tori Diili',
  tori: 'Tori',
  ebay: 'eBay',
  new_unused: 'New / unused',
  like_new: 'Like new',
  very_good: 'Very good',
  good: 'Good',
  fair: 'Fair',
  poor: 'Poor',
  for_parts: 'For parts',
  refurbished: 'Refurbished',
  private: 'Private',
  professional: 'Professional',
  business: 'Business',
  unknown: 'Unknown',
};

export const label = code => (code == null || code === '' ? DASH : LABELS[code] ?? code);

const ICONS = Object.assign(Object.create(null), {
  overview: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  list: 'M8 5h13 M8 12h13 M8 19h13 M3 5h.01 M3 12h.01 M3 19h.01',
  collect: 'M12 3v12 M7 10l5 5 5-5 M4 16v5h16v-5',
  wallet: 'M20 7V5H4v14h16v-8h-6v4h6 M4 7h16',
  price: 'M20 13l-7 7-10-10V3h7z M7 7h.01',
  chart: 'M4 20V10 M10 20V4 M16 20v-8 M22 20H2',
  globe: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z M3 12h18 M12 3c5 5 5 13 0 18-5-5-5-13 0-18z',
  refresh: 'M20 7a8 8 0 1 0 1 8 M20 3v5h-5',
  plus: 'M12 5v14 M5 12h14',
  menu: 'M4 6h16 M4 12h16 M4 18h16',
  filter: 'M4 5h16 M7 12h10 M10 19h4',
  search: 'M16 16l5 5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0z',
  spark: 'M12 3l3 6 6 3-6 3-3 6-3-6-6-3 6-3z',
  file: 'M14 3H5v18h14V8z M14 3v5h5 M8 13h8 M8 17h5',
});

/**
 * Inline SVG for a named icon. `aria-hidden` because every icon in this UI sits
 * next to a text label and repeating it aloud is noise.
 *
 * The lookup table has a null prototype so that an unexpected `name` — including
 * `constructor` or `__proto__` — falls through to the `list` fallback instead of
 * stringifying an inherited property.
 */
export function icon(name, size = 16) {
  const d = ICONS[name] ?? ICONS.list;
  return (
    `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor"` +
    ` stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `<path d="${d}"/></svg>`
  );
}

/**
 * One CSV field.
 *
 * Spreadsheet programs execute cell contents that begin with `=`, `+`, `-` or
 * `@`. A listing title is attacker-controlled text, so a title of
 * `=cmd|'/c calc'!A1` exported to a CSV the operator opens would run on their
 * machine. Prefixing an apostrophe defuses it. Leading whitespace is stripped
 * for the test because Excel and LibreOffice both ignore it when deciding
 * whether a cell is a formula.
 */
export const csvCell = value => {
  let text = value == null ? '' : String(value);
  if (typeof value === 'string' && /^[\s]*[=+\-@]/.test(text)) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
};

/** A complete CSV document: BOM for Excel, CRLF rows, every field quoted. */
export const csvDocument = (header, rows) =>
  `﻿${[header, ...rows].map(row => row.map(csvCell).join(',')).join('\r\n')}`;

/**
 * Build the `filters` query object sent to every read endpoint.
 *
 * Only allowlisted keys with non-empty values are emitted, so the request cannot
 * drift from the filter chips the operator can actually see. Platform and market
 * are omitted when they equal their default, because `platform=tori` and no
 * parameter at all must select the same cohort.
 */
export const FILTER_KEYS = [
  'platform',
  'subplatform',
  'category_root_norm',
  'municipality_norm',
  'condition_norm',
  'seller_type_norm',
  'category_norm',
  'focus_path',
];

const FILTER_DEFAULTS = { platform: 'tori', subplatform: 'standard' };

/** Which filters are meaningfully set, given that two of them have defaults. */
export const activeFilterKeys = filters =>
  FILTER_KEYS.filter(
    key => filters[key] != null && filters[key] !== '' && filters[key] !== FILTER_DEFAULTS[key],
  );

/** Serialise the filter state to a query string for a read endpoint. */
export const filtersToQuery = filters => {
  const params = new URLSearchParams();
  for (const key of FILTER_KEYS) {
    const value = filters[key];
    if (value != null && value !== '') params.set(key, value);
  }
  return params;
};

/**
 * Parse a URL query string into filter state.
 *
 * Unknown keys are ignored rather than passed through: a hand-edited URL must
 * not be able to smuggle a parameter the UI does not display.
 */
export const filtersFromQuery = search => {
  const params = new URLSearchParams(search);
  const filters = { ...FILTER_DEFAULTS };
  for (const key of FILTER_KEYS) {
    const value = params.get(key);
    if (value != null && value !== '') filters[key] = value;
  }
  return filters;
};

/**
 * Parse the operator's refresh cadence in seconds.
 *
 * Clamped to 5..300 seconds. The lower bound stops a `?refresh=0` from becoming
 * a request loop against the collector's own database.
 */
export const refreshMs = (requested, documentSeconds) => {
  const raw = Number(requested) || Number(documentSeconds) || 30;
  return Math.min(300, Math.max(5, raw)) * 1000;
};
