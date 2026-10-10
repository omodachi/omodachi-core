// ATTACH-1: what the paperclip may offer, and an attachment as a chip.
//
// The host decides what it keeps (core `uploads.py`: images by their bytes,
// PDF by `%PDF-`, text and code by name with every byte checked, 25 MB). This
// file only decides what the picker offers and what is refused before a byte
// leaves the phone, so the person hears "too big" without waiting for it.
// Nothing here is a security boundary.
//
// The chip grammar is Collie's (ADR 0060, `web/src/lib/attachments.ts`, MIT):
// an upload puts `[Image #N]` or `[File #N]` into the draft where the caret
// was; Send carries the ids and the markers, and core swaps each marker for
// the file's path (Herdr) or for codex's own image input (Agent). The markers
// are English in every language - they are what Claude Code and codex write.

export const MAX_BYTES = 25 * 1024 * 1024;
export const MAX_MB = 25;
export const MAX_CHIPS = 10;

export const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'heic', 'heif'];
// core `uploads.TEXT_EXTS`, the same list.
export const TEXT_EXTS = [
  'md', 'markdown', 'txt', 'text', 'rst', 'org', 'adoc', 'tex',
  'json', 'jsonl', 'ndjson', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf',
  'csv', 'tsv', 'log', 'xml', 'html', 'htm', 'css', 'scss', 'sass', 'less',
  'js', 'jsx', 'mjs', 'cjs', 'ts', 'tsx', 'vue', 'svelte',
  'py', 'pyi', 'ipynb', 'go', 'rs', 'c', 'h', 'cc', 'cpp', 'cxx', 'hpp', 'hh',
  'java', 'kt', 'kts', 'swift', 'm', 'mm', 'rb', 'php', 'pl', 'lua', 'r', 'jl',
  'scala', 'dart', 'zig', 'nim', 'ex', 'exs', 'erl', 'hs', 'ml', 'clj',
  'sh', 'bash', 'zsh', 'fish', 'sql', 'graphql', 'gql', 'proto', 'diff', 'patch',
];

// A phone offers its photo library only when every entry in `accept` maps to
// one, so photos and files are two inputs (Collie's finding, ADR 0060).
export const PHOTO_ACCEPT = 'image/png,image/jpeg,image/gif,image/webp,image/heic,image/heif';
export const FILE_ACCEPT = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', '.pdf',
  ...IMAGE_EXTS.map((ext) => '.' + ext), ...TEXT_EXTS.map((ext) => '.' + ext)].join(',');

export function extensionOf(name) {
  const value = String(name || '');
  const dot = value.lastIndexOf('.');
  if (dot <= 0 || dot === value.length - 1) return null;
  const ext = value.slice(dot + 1).toLowerCase();
  return /^[a-z0-9]{1,10}$/.test(ext) ? ext : null;
}

// `null` means send it; otherwise the reason, keyed for `t.attach`.
export function rejectAttachment(file) {
  if (!file) return 'badType';
  if (file.size > MAX_BYTES) return 'tooLarge';
  if (file.size === 0) return 'empty';
  const ext = extensionOf(file.name);
  const type = String(file.type || '').toLowerCase();
  if (type === 'image/svg+xml' || ext === 'svg' || ext === 'svgz') return 'svg';
  if (type.startsWith('image/') && type !== 'image/avif') return null;
  if (ext !== null && (IMAGE_EXTS.includes(ext) || TEXT_EXTS.includes(ext) || ext === 'pdf')) return null;
  return 'badType';
}

export function attachmentKind(file) {
  const type = String((file && file.type) || '').toLowerCase();
  if (type.startsWith('image/')) return 'image';
  const ext = extensionOf(file && file.name);
  return ext !== null && IMAGE_EXTS.includes(ext) ? 'image' : 'file';
}

export function markerFor(chip) {
  return '[' + (chip.kind === 'image' ? 'Image' : 'File') + ' #' + chip.n + ']';
}

// The marker goes in at the caret (the end when there is none), never welded
// to a word. Returns the new text and where the caret goes.
export function insertMarker(text, caret, marker) {
  const value = String(text || '');
  const at = Math.min(Math.max(caret == null ? value.length : caret, 0), value.length);
  const before = value.slice(0, at);
  const after = value.slice(at);
  const lead = before !== '' && !/\s$/.test(before) ? ' ' : '';
  const trail = /^\s/.test(after) ? '' : ' ';
  const inserted = lead + marker + trail;
  return {text: before + inserted + after, caret: at + inserted.length + (trail === '' ? 1 : 0)};
}

// Every copy of a marker leaves, with one space beside it.
export function removeMarker(text, marker) {
  let out = String(text || '');
  for (let at = out.indexOf(marker); at !== -1; at = out.indexOf(marker, at)) {
    let start = at;
    let end = at + marker.length;
    if (out[end] === ' ') end += 1;
    else if (start > 0 && out[start - 1] === ' ') start -= 1;
    out = out.slice(0, start) + out.slice(end);
    at = start;
  }
  return out;
}

export function markerMissing(text, chip) {
  return !String(text || '').includes(markerFor(chip));
}

export function shortName(name, max = 14) {
  const value = String(name || '');
  return value.length > max ? value.slice(0, max - 1) + '…' : value;
}

// What a send carries: the uploaded chips, in order, with their markers.
export function sendRefs(chips) {
  return chips.filter((chip) => chip.status === 'ready' && chip.id).map((chip) => ({id: chip.id, marker: markerFor(chip)}));
}

export function uploading(chips) {
  return chips.some((chip) => chip.status === 'uploading');
}

// core's refusal codes, and the client's own, as `t.attach` keys.
const REFUSALS = {
  upload_too_large: 'tooLarge', upload_svg_refused: 'svg', upload_type_refused: 'badType',
  upload_not_text: 'notText', upload_heic_unconvertible: 'heic', upload_empty: 'empty',
  attachment_gone: 'gone',
};

export function refusalKey(code) {
  return REFUSALS[code] || (['tooLarge', 'svg', 'badType', 'notText', 'heic', 'empty', 'gone'].includes(code)
    ? code : 'failed');
}

// The upload target, as `POST /v1/uploads` takes it in its query.
export function uploadQuery(name, target) {
  const query = {name: String(name || 'file').slice(0, 200)};
  if (target && target.kind) {
    query.target = target.kind;
    if (target.session) query.session = target.session;
    if (target.pane) query.pane = target.pane;
  }
  return query;
}
