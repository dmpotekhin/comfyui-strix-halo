// The studio front end is plain ES modules with no build step, so its unit tests run on node's
// own test runner: `node --test studio/frontend/tests/`. The Python suite also runs this file
// (tests/test_frontend_uuid.py) so a regression fails the main gate, not just a manual run.
//
// Why these tests exist: over plain http:// on the home LAN (http://192.168.31.60:8000) the
// browser has no crypto.randomUUID — that method is exposed only in a secure context. The submit
// path called it directly and threw "crypto.randomUUID is not a function" before a job was ever
// sent, so the run button did nothing and the console showed a TypeError.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const ASSETS = join(HERE, '..', 'dist', 'assets');
const { randomId, api } = await import(join(ASSETS, 'api.js'));

const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

// A stand-in for window.crypto. Two flavours, because the whole bug is about the difference
// between them: secure context (both methods) and LAN plain HTTP (getRandomValues only).
function secureCrypto(native = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee') {
  return {
    randomUUID: () => native,
    getRandomValues: (buf) => {
      for (let i = 0; i < buf.length; i++) buf[i] = (i * 37 + 11) & 0xff;
      return buf;
    },
  };
}

function insecureCrypto(fill = null) {
  return {
    // randomUUID is deliberately absent, exactly as in a non-secure context.
    getRandomValues: (buf) => {
      if (fill) return fill(buf);
      for (let i = 0; i < buf.length; i++) buf[i] = Math.floor(Math.random() * 256);
      return buf;
    },
  };
}

test('uses crypto.randomUUID when the browser has it', () => {
  const sentinel = '01234567-89ab-4cde-8f01-234567890abc';
  assert.equal(randomId(secureCrypto(sentinel)), sentinel);
});

test('falls back to a v4 UUID built from getRandomValues when randomUUID is missing', () => {
  const id = randomId(insecureCrypto());
  assert.match(id, V4, `not an RFC 4122 v4 UUID: ${id}`);
});

test('the fallback sets the version and variant bits itself', () => {
  // All bytes 0x00 would otherwise give the version nibble 0 and variant 0.
  const id = randomId(insecureCrypto((buf) => buf.fill(0)));
  assert.match(id, V4);
  // All bytes 0xff must be masked down, not passed through.
  const all = randomId(insecureCrypto((buf) => buf.fill(0xff)));
  assert.match(all, V4);
  assert.equal(all.slice(14, 15), '4');
  assert.ok(['8', '9', 'a', 'b'].includes(all.slice(19, 20)));
});

test('the fallback is random, not a fixed shape', () => {
  const seen = new Set();
  for (let i = 0; i < 500; i++) seen.add(randomId(insecureCrypto()));
  assert.equal(seen.size, 500);
  for (const id of seen) assert.match(id, V4);
});

test('no Web Crypto means an honest error, never Math.random', () => {
  const realRandom = Math.random;
  let calls = 0;
  Math.random = () => { calls++; return 0.5; };
  try {
    assert.throws(() => randomId({}), /Web Crypto/);
    assert.throws(() => randomId(null), /Web Crypto/);
    // An omitted argument means "use the global", which node does provide — no throw, v4 back.
    assert.match(randomId(), V4);
    assert.equal(calls, 0, 'Math.random must never be used as a UUID source');
  } finally {
    Math.random = realRandom;
  }
});

test('submitting a job survives the insecure context', async () => {
  // The whole submit path, with fetch stubbed: this is what the run button does.
  const sent = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (path, options) => {
    sent.push({ path, options });
    return {
      status: 201,
      ok: true,
      text: async () => JSON.stringify({ created: true, job: { id: 'job-1' }, warnings: [] }),
    };
  };
  try {
    const token = randomId(insecureCrypto());
    const res = await api.submit('txt2img-turbo', { prompt: 'a horse' }, token);
    assert.equal(res.job.id, 'job-1');
    assert.equal(sent.length, 1);
    const body = JSON.parse(sent[0].options.body);
    assert.match(body.client_token, V4, 'the token must reach the server as a v4 UUID');
    assert.equal(body.workflow_id, 'txt2img-turbo');
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('the shipped app never calls crypto.randomUUID directly', () => {
  // The guard the Python suite repeats, kept here so a `node --test` run catches it too.
  // Without a call site outside this module the helper is the only path to a token.
  // Comments and strings mention the method on purpose, so strip them before matching —
  // otherwise a future edit could satisfy the guard by writing about the fix instead of applying it.
  const app = readFileSync(join(ASSETS, 'app.js'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
  assert.ok(!/crypto\.randomUUID\s*\(/.test(app), 'app.js must use randomId(), not the native generator');
  assert.ok(/randomId\s*\(/.test(app), 'app.js should import and call randomId()');
});
