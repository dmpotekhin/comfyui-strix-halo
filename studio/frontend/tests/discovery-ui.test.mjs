// Guards for the discovery screen the audit found broken. Like uuid.test.mjs these are static
// checks against the shipped file: the view is rendered by a big template string inside app.js and
// cannot be imported into node without a DOM, so the honest way to keep the behaviour from
// regressing is to pin the shapes that make it work — and to prove them in a browser when the
// screen changes.
//
// Why each assertion exists:
// * a 400 from /api/discovery/search used to replace the whole section with the error text,
//   losing the filters and the results the person was looking at;
// * local catalogue rows were offered Analyze / Compare / Favourite, which all answered
//   "no such resource" because those endpoints only know discovered resources.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const app = readFileSync(join(here, '..', 'dist', 'assets', 'app.js'), 'utf8');
const css = readFileSync(join(here, '..', 'dist', 'assets', 'app.css'), 'utf8');

test('the discovery search is wrapped, so a rejection cannot take the section down', () => {
  const at = app.indexOf('discoverState.result = await api.discovery.search(');
  assert.ok(at > 0, 'the search call must still go through api.discovery.search');
  const before = app.slice(Math.max(0, at - 200), at);
  const after = app.slice(at, at + 2000);
  assert.match(before, /try\s*\{/, 'the search call must sit inside a try block');
  assert.match(after, /catch \(err\)[\s\S]*discoverState\.error = searchFailure\(err\)/,
    'a rejected search must be recorded as an error, not rethrown into the router');
});

test('every status the API can answer with has a sentence a person can act on', () => {
  const table = app.slice(app.indexOf('function searchFailure'), app.indexOf('function activeFilterCount'));
  for (const status of ['400', '401', '403', '429', '500']) {
    assert.match(table, new RegExp(`\\b${status}:`), `status ${status} has no message`);
  }
  assert.match(table, /status \? `The search failed \(HTTP \$\{status\}\)/) ;
  assert.match(table, /could not be reached/, 'a network failure needs its own message too');
});

test('a failed search keeps the results and offers a retry', () => {
  assert.match(app, /const result = discoverState\.result \|\| \{ total: 0, results: \[\], notes: \[\] \}/,
    'the last good result set must survive a failure');
  assert.match(app, /id="d-retry"/, 'the error must offer a retry');
  assert.match(app, /results of the last search that worked/,
    'the screen must say the visible results are older than the failed request');
});

test('the retry re-runs the same query instead of resetting it', () => {
  assert.match(app, /bind\('d-retry', 'click', \(\) => rerender\(\)\)/);
  const rerender = app.slice(app.indexOf('function rerender'), app.indexOf('function rerender') + 260);
  assert.doesNotMatch(rerender, /discoverState\.(q|sort|source|type|category|base_model)\s*=/,
    'a rerender must not clear what the person typed or picked');
});

test('filters collapse on a phone and stay untouched on a desktop', () => {
  assert.match(css, /@media \(max-width: 700px\)[\s\S]*\.disc-extra \{ display: none; \}/,
    'the secondary filters must collapse on a narrow screen');
  assert.match(css, /\.disc-filters\.is-open \.disc-extra/, 'collapsed filters must be openable');
  assert.match(css, /\.disc-extra \{ display: contents; \}/,
    'on a desktop the wrapper must not change the toolbar layout');
  assert.match(app, /discoverState\.filtersOpen === null \? !isNarrow\(\) : discoverState\.filtersOpen/,
    'a phone must start collapsed and the user choice must win afterwards');
});

test('local catalogue rows do not offer actions that only discovery resources have', () => {
  // The first `.row.wf-actions` in the file belongs to the workflows view, so anchor on the
  // discovery card markup itself.
  const cardAt = app.indexOf('article class="wf-card disc-card"');
  assert.ok(cardAt > 0, 'the discovery card markup must exist');
  const actionsAt = app.indexOf('<div class="row wf-actions">', cardAt);
  const block = app.slice(actionsAt, actionsAt + 1200);
  assert.match(block, /item\.origin === 'local'/, 'the actions must depend on the row origin');
  assert.match(block, /href="#\/library"/, 'a local row should point at the catalogue instead');
  assert.match(block, /data-act="analyze"/, 'discovered rows must keep Analyze');
});
