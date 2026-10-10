// LITE-1a: what the CSP and the "text nodes only" rule need from the source
// and from the committed build.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, readdirSync, statSync} from 'node:fs';
import {join} from 'node:path';

const here = new URL('..', import.meta.url).pathname;
const built = join(here, '../src/omodachi_core/data/odomo');

function files(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

test('the source never renders HTML from a string, and never evaluates one', () => {
  for (const path of files(join(here, 'src')).filter((p) => /\.(jsx?|mjs)$/.test(p))) {
    const text = readFileSync(path, 'utf8');
    for (const banned of ['dangerouslySetInnerHTML', 'innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write',
      'eval(', 'new Function', 'localStorage.setItem(\'odomo.credential']) {
      assert.ok(!text.includes(banned), `${path} uses ${banned}`);
    }
  }
});

test('the token is kept in IndexedDB, never in localStorage', () => {
  const app = readFileSync(join(here, 'src/App.jsx'), 'utf8');
  assert.match(app, /storage\.set\('credential'/);
  assert.doesNotMatch(app, /setLocal\('credential'/);
});

test('the committed build has no inline script or style element and a standalone manifest', () => {
  const html = readFileSync(join(built, 'index.html'), 'utf8');
  assert.doesNotMatch(html, /<script(?![^>]*\bsrc=)[^>]*>/);
  assert.doesNotMatch(html, /<style/);
  assert.doesNotMatch(html, /\son[a-z]+=/i);
  for (const [, url] of html.matchAll(/(?:src|href)="([^"]+)"/g)) assert.match(url, /^\.\//, url);
  const manifest = JSON.parse(readFileSync(join(built, 'manifest.webmanifest'), 'utf8'));
  assert.equal(manifest.display, 'standalone');
  assert.equal(manifest.id, '/odomo/');
  assert.equal(manifest.scope, '/odomo/');
  assert.ok(manifest.icons.some((icon) => icon.sizes === '512x512'));
  for (const name of readdirSync(join(built, 'assets'))) assert.match(name, /-[A-Za-z0-9_-]{8}\.(js|css|woff2)$/, name);
});
