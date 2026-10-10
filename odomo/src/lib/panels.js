// The bar's entries, in the App's order (omodachi-ios `PanelRegistry.builtIn`,
// A-50: Remote / Agent / Herdr / SSH / Settings / Notifications) less what
// Odomo does not have. SSH is not here: a browser cannot open SSH, and core
// has no path that would stand in for it (ODOMO-2 report, a product decision).
export const PANELS = ['remote', 'agent', 'herdr', 'settings', 'notifications'];
// Settings can never switch itself off: it is the way back to the switches.
export const LOCKED = ['settings'];

// N-38: the entries this browser has switched off are left off the bar.
export function barPanels(off) {
  const hidden = new Set(Array.isArray(off) ? off : []);
  return PANELS.filter((id) => LOCKED.includes(id) || !hidden.has(id));
}
