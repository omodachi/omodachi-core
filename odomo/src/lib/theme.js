// The host's Omarchy theme (`GET /v1/theme`, `theme.changed`) as the CSS
// variables styles.css is written against. The palette roles are core's
// (contracts/theme.schema.json); the variable names are omodachi-web's.

const ROLES = {
  '--om-bg': 'background',
  '--om-dark': 'dark_background',
  '--om-darker': 'darker_background',
  '--om-raised': 'lighter_background',
  '--om-fg': 'foreground',
  '--om-dim': 'muted',
  '--om-line': 'muted',
  '--om-bright': 'bright_foreground',
  '--om-sec': 'light_foreground',
  '--om-accent': 'accent',
  '--om-sel': 'selection',
  '--om-green': 'green',
  '--om-red': 'red',
  '--om-yellow': 'yellow',
  '--om-cyan': 'cyan',
};
const TRIPLES = {'--fgr': 'foreground', '--acr': 'accent', '--drk': 'darker_background', '--bgr': 'background'};
const HEX = /^#[0-9a-fA-F]{6}$/;

export function rgb(hex) {
  return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)).join(', ');
}

// Only well-formed colours get through; a role the host does not send keeps
// the stylesheet's Tokyo Night value.
export function themeVariables(theme) {
  const colors = theme && typeof theme === 'object' && theme.colors && typeof theme.colors === 'object' ? theme.colors : {};
  const out = {};
  for (const [name, role] of Object.entries(ROLES)) {
    if (HEX.test(colors[role] || '')) out[name] = colors[role];
  }
  for (const [name, role] of Object.entries(TRIPLES)) {
    if (HEX.test(colors[role] || '')) out[name] = rgb(colors[role]);
  }
  return out;
}

export function themeMode(theme) {
  return theme && theme.mode === 'light' ? 'light' : 'dark';
}

// CSSOM, not a <style> element: the CSP allows the first and not the second.
export function applyTheme(theme, root = document.documentElement) {
  for (const [name, value] of Object.entries(themeVariables(theme))) root.style.setProperty(name, value);
  root.style.colorScheme = themeMode(theme);
  const meta = document.querySelector('meta[name="theme-color"]');
  const bg = theme && theme.colors && theme.colors.background;
  if (meta && HEX.test(bg || '')) meta.setAttribute('content', bg);
}
