// LINKS-1: links in what agents send - which are web addresses, which are
// files on the computer, and where a `localhost` address really is.
//
// Leo (2026-10-04): a web address opens here, in a new tab; one that points
// at the computer itself (`localhost`, `127.0.0.1`, `0.0.0.0`, the host's own
// names) is rewritten to the road this browser came in on, keeping scheme,
// port, path, query and fragment. A file is never fetched: "Open on the
// computer" asks core to open it there (`POST /v1/host/open`), and the App's
// Remote is where it is watched.
//
// The detection and rewrite rules are core's `contracts/fixtures/links-detect.json`
// (its `notes`), which the App tests against too; `tests/links1.test.mjs`
// runs every vector here.

export const KNOWN_EXTENSIONS = ['md', 'markdown', 'txt', 'rst', 'org', 'py', 'js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx',
  'json', 'jsonl', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf', 'csv', 'tsv', 'log', 'html', 'htm', 'css', 'scss',
  'swift', 'kt', 'java', 'c', 'h', 'cc', 'cpp', 'hpp', 'rs', 'go', 'rb', 'php', 'lua', 'sql', 'sh', 'zsh', 'xml',
  'svg', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'heic', 'pdf', 'mp4', 'mov', 'mp3', 'wav', 'zip', 'gz', 'ipynb',
  'lock', 'diff', 'patch'];

const BOUNDARY = '(?:(?<=^)|(?<=[\\s(\\[{<"\'`=]))';
const URL_RE = 'https?://[^\\s<>"\'`]+';
// A path from the root needs a second / or a . in it, so `</b>` is not one.
const PATH_CHARS = '[A-Za-z0-9._~@%+=,/:#-]';
const PATH_RE = '(?:(?:~|\\.\\.?)/(?![/\\s])' + PATH_CHARS + '+|/(?![/\\s])(?=' + PATH_CHARS + '*[/.])' + PATH_CHARS + '+)';
const BARE_RE = '[A-Za-z0-9_][A-Za-z0-9._-]*\\.(?:'
  + [...KNOWN_EXTENSIONS].sort((a, b) => b.length - a.length).join('|') + ')(?![A-Za-z0-9_/-])';
const MIRROR = new RegExp(BOUNDARY + '(?:(' + URL_RE + ')|(' + PATH_RE + ')|(' + BARE_RE + '))', 'gi');
const PROSE = new RegExp(BOUNDARY + '(?:(' + URL_RE + ')|(' + PATH_RE + '))', 'g');
const CLOSERS = {')': '(', ']': '[', '}': '{'};

function count(text, ch) {
  let n = 0;
  for (const c of text) if (c === ch) n++;
  return n;
}

export function trimLink(text) {
  let value = text;
  for (;;) {
    const last = value.slice(-1);
    if (last && '.,;:!?'.includes(last)) value = value.slice(0, -1);
    else if (CLOSERS[last] && count(value, CLOSERS[last]) < count(value, last)) value = value.slice(0, -1);
    else return value;
  }
}

// `mode`: 'mirror' (terminal text: URLs, paths, bare file names) or 'prose'
// (an agent's words: URLs and paths that start with / ~/ ./ ../).
export function detect(text, mode = 'prose') {
  const pattern = mode === 'mirror' ? MIRROR : PROSE;
  pattern.lastIndex = 0;
  const found = [];
  let match;
  while ((match = pattern.exec(String(text || ''))) !== null) {
    const value = trimLink(match[0]);
    if (value) found.push({kind: match[1] ? 'url' : 'path', text: value, start: match.index, end: match.index + value.length});
  }
  return found;
}

// A whole string that is one link (inline code, a markdown target).
export function linkOf(text, mode = 'prose') {
  const value = String(text || '').trim();
  const [first] = detect(value, mode);
  return first && first.start === 0 && first.text === value ? first : null;
}

export function isURL(text) {
  return /^https?:\/\//i.test(String(text || ''));
}

// A markdown link target: a web address, an app link, or a path on the computer.
export function targetKind(target) {
  const value = String(target || '').trim();
  if (isURL(value)) return 'url';
  if (/^omodachi:/i.test(value)) return 'app';
  if (/^[a-z][a-z0-9+.-]*:/i.test(value) && !/^file:\/\//i.test(value)) return 'other';
  return value ? 'path' : 'other';
}

function isIP(host) {
  return /^\d{1,3}(\.\d{1,3}){3}$/.test(host) || host.includes(':');
}

function isV4(host) {
  return /^\d{1,3}(\.\d{1,3}){3}$/.test(host);
}

function bare(host) {
  return String(host || '').toLowerCase().replace(/^\[|\]$/g, '');
}

// The host's own names: every road, the MagicDNS name and its addresses, and
// the short name in front of `.local` / `.ts.net`.
export function hostNames(roads, extra = []) {
  const names = new Set();
  const add = (value) => { if (typeof value === 'string' && value) names.add(bare(value)); };
  for (const row of (roads && Array.isArray(roads.endpoints) ? roads.endpoints : [])) add(row && row.host);
  const tailnet = roads && roads.tailnet ? roads.tailnet : {};
  add(tailnet.dns_name);
  for (const ip of Array.isArray(tailnet.ips) ? tailnet.ips : []) add(ip);
  for (const value of extra) add(value);
  for (const name of [...names]) {
    if (name.endsWith('.local') || name.endsWith('.ts.net')) names.add(name.split('.')[0]);
  }
  return names;
}

export function hostLocal(url, roads, extra = []) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (!/^https?:$/.test(parsed.protocol)) return false;
  const host = bare(parsed.hostname);
  if (host === 'localhost' || host === '0.0.0.0' || host === '::1' || host.endsWith('.localhost')) return true;
  if (/^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
  return hostNames(roads, extra).has(host);
}

// What kind of road a host name is, for a browser that only knows where it is.
export function roadOf(hostname, roads) {
  const host = bare(hostname);
  const row = (roads && Array.isArray(roads.endpoints) ? roads.endpoints : []).find((r) => bare(r.host) === host);
  if (row) return {host, kind: row.kind};
  if (host.endsWith('.ts.net') || /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(host)) return {host, kind: 'tailnet'};
  if (host.endsWith('.local')) return {host, kind: 'mdns'};
  return {host, kind: 'lan'};
}

// The same address, on the road this device uses for the host. For http an
// IP road is preferred: it needs no name lookup and leaves a browser nothing
// to try upgrading to https first. (Measured on iOS 27: `http://<name>.ts.net:8000`
// loads too; this is a preference, not a workaround.)
export function rewrite(url, road, roads, extra = []) {
  if (!hostLocal(url, roads, extra) || !road || !road.host) return url;
  const parsed = new URL(url);
  let host = bare(road.host);
  if (parsed.protocol === 'http:' && !isIP(host)) {
    const endpoints = roads && Array.isArray(roads.endpoints) ? roads.endpoints : [];
    const first = (kind) => (endpoints.find((r) => r && r.kind === kind && isV4(bare(r.host))) || {}).host;
    host = (road.kind === 'tailnet' ? first('tailnet') : first('lan')) || host;
  }
  const port = parsed.port ? ':' + parsed.port : '';
  const shown = host.includes(':') ? '[' + host + ']' : host;
  return parsed.protocol + '//' + shown + port + parsed.pathname + parsed.search + parsed.hash;
}
