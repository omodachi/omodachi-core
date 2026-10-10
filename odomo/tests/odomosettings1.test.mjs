// ODOMO-SETTINGS-1: Odomo's Settings follow the App's row rule
// (SETTINGS-LAYOUT-1, SettingLine.swift). Each row says its arrangement -
// beside, fitting or under - and the device never decides it; the detail
// line wraps under the title; long values go under and are never cut; one
// title size; Language and Snooze have a title.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {runnerImport} from 'vite';
import {en, zh} from '../src/lib/i18n.js';

const opts = {root: new URL('..', import.meta.url).pathname, configFile: false, logLevel: 'silent'};
const {module: render} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname, opts);
const {module: ds} = await runnerImport(new URL('../src/ds.jsx', import.meta.url).pathname, opts);
const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
const h = React.createElement;

function rule(selector) {
  const at = css.indexOf('\n' + selector + ' {');
  assert.ok(at >= 0, 'no rule for ' + selector);
  return css.slice(at, css.indexOf('}', at));
}
// The opening tag of the element with this data-testid.
function tag(html, testid) {
  const at = html.indexOf('data-testid="' + testid + '"');
  assert.ok(at >= 0, 'no ' + testid);
  return html.slice(html.lastIndexOf('<', at), html.indexOf('>', at) + 1);
}

function settings(lang) {
  globalThis.location = {hostname: 'host.example.ts.net', host: 'host.example.ts.net'};
  globalThis.window = globalThis.window || {};
  try {
    return render.renderSettings({credential: {device_id: 'odomo-7f3a9c21-4b8e-4d2a-9e61-0c5b8f2d7a43', host_name: 'host',
      client_label: 'Chrome', expires_at: 1798761600}}, lang);
  } finally {
    delete globalThis.location;
  }
}

test('every Settings row says its arrangement; the old row shapes are gone', () => {
  const html = settings('en');
  assert.doesNotMatch(html, /class="(srow|trow|vline)[ "_]/);
  assert.match(tag(html, 'settings-tour-replay'), /class="sline sline--beside sline--tap"/);
  assert.match(tag(html, 'settings-licence'), /data-arrange="beside"/);
  assert.match(tag(html, 'settings-source-code'), /^<a [^>]*class="sline sline--beside/);
  for (const id of ['remote', 'agent', 'herdr', 'settings', 'notifications']) {
    assert.match(tag(html, 'settings-entry-' + id), /data-arrange="beside"/);
    assert.match(tag(html, 'settings-entry-' + id + '-toggle'), /role="switch"/);
  }
  // A long value goes under its title; short ones fit where they can.
  assert.match(tag(html, 'settings-device'), /class="sline sline--under"/);
  assert.match(tag(html, 'settings-build'), /data-arrange="fitting"/);
  assert.ok(html.includes('odomo-7f3a9c21-4b8e-4d2a-9e61-0c5b8f2d7a43'), 'the whole device id is in the page');
});

test('Language is a titled choice that fits beside its title or goes under (en, zh)', () => {
  for (const [lang, table] of [['en', en], ['zh-Hans', zh]]) {
    const html = settings(lang);
    assert.match(tag(html, 'settings-language-row'), /class="sline sline--fitting sline--choice"/);
    assert.ok(html.includes('data-testid="settings-language-title">' + table.settings.language + '<'));
    assert.match(tag(html, 'settings-language'), /role="radiogroup"/);
  }
  assert.equal(en.settings.language, 'Language');
  assert.equal(en.push.snooze, 'Snooze');
});

test('the primitives: a switch beside, a choice fitting, a value under, details under the title', () => {
  const toggle = renderToStaticMarkup(h(ds.SquareToggleRow, {title: 'T', detail: 'D', on: true, onChange() {}, testid: 'x'}));
  assert.match(toggle, /^<div class="sline sline--beside" data-arrange="beside">/);
  assert.match(toggle, /<span class="sline__tx"><span class="sline__t" data-testid="x-title">T<\/span><span class="sline__d">D<\/span><\/span>/);
  assert.match(toggle, /<span class="sline__end"><button [^>]*data-testid="x"/);
  const choice = renderToStaticMarkup(h(ds.SettingChoice, {title: 'Snooze', testid: 'push-snooze', rowTestid: 'push-snooze-row',
    value: 0, onChange() {}, options: [['Off', 0], ['1 hour', 1]]}));
  assert.match(choice, /data-testid="push-snooze-row" data-arrange="fitting"/);
  assert.match(choice, /aria-label="Snooze" data-testid="push-snooze"/);
  const value = renderToStaticMarkup(h(ds.ValueLine, {name: 'Device', value: 'v', testid: 'd', arrange: 'under'}));
  assert.match(value, /class="sline sline--under" data-testid="d"/);
  assert.match(value, /<span class="sline__v">v<\/span>/);
  const link = renderToStaticMarkup(h(ds.SettingAction, {title: 'Source', detail: 'https://x', href: 'https://x', testid: 's'}));
  assert.match(link, /^<a class="sline sline--beside sline--tap"[^>]*href="https:\/\/x"/);
});

test('the CSS: one title size, wrapping detail and values, measured fitting, equal segments', () => {
  assert.match(rule('.sline__t'), /font-size: 16px/);
  assert.equal((css.match(/\.sline__t \{/g) || []).length, 1, 'one title rule');
  for (const selector of ['.sline__d', '.sline__v']) {
    assert.doesNotMatch(rule(selector), /nowrap|ellipsis/, selector + ' is never cut');
    assert.match(rule(selector), /overflow-wrap: anywhere/);
  }
  assert.match(rule('.sline__d'), /contain: inline-size/, 'the detail never widens the row');
  assert.match(rule('.sline__v'), /user-select: text/);
  assert.match(rule('.sline--fitting'), /flex-wrap: wrap/);
  assert.match(rule('.sline--under'), /flex-direction: column/);
  assert.doesNotMatch(css, /\.sline--beside \{[^}]*flex-wrap: wrap/);
  assert.match(rule('.sline--choice .sline__end'), /max-width: 420px/);
  assert.match(rule('.segrow'), /grid-auto-columns: 1fr/);
  // The device never decides: no phone/pad override of a Settings row.
  assert.doesNotMatch(css, /data-dev="(phone|pad)"\][^{]*\.sline/);
});
