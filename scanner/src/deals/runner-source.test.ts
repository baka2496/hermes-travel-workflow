// Checks on the page-reading code that cannot run without Google (2026-11-20, both found in a live run).
import { test } from 'vitest';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./runner.ts', import.meta.url), 'utf8');
const body = (name: string) => {
  const start = source.indexOf(`export async function ${name}(`);
  assert.ok(start >= 0, name);
  const next = source.indexOf('\nexport ', start + 1);
  return source.slice(start, next < 0 ? undefined : next);
};

test('the hotel list reads "less than usual" on every page, not only the last', () => {
  const list = body('captureHotelList');
  const readLinks = list.slice(list.indexOf('const readLinks'), list.indexOf('await readLinks();'));
  const labelRead = "querySelectorAll('[aria-label]')";
  assert.ok(readLinks.includes(labelRead), 'labels are read inside readLinks, which runs after each page');
  assert.equal(list.split(labelRead).length - 1, 1, 'no second, last-page-only read');
});

test('a flight list with nothing at the right time is told apart from a page not read', () => {
  const book = body('bookFlight');
  assert.match(book, /throw new NoFittingFlight\('outbound'\)/);
  assert.match(book, /throw new NoFittingFlight\('inbound'\)/);
});
