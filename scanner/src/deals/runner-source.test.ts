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

test('Google\'s error page on Explore is not waited out', () => {
  const wait = source.slice(source.indexOf('async function waitForCards'), source.indexOf('const GOOGLE_ERROR'));
  assert.match(wait, /if \(GOOGLE_ERROR\.test\(text\)\) return text;/);
});

test('Google\'s error page on Explore is reloaded in place before giving up', () => {
  const once = source.slice(source.indexOf('async function captureExploreOnce'), source.indexOf('function context('));
  assert.match(once, /reload < EXPLORE_RELOADS && GOOGLE_ERROR\.test\(text\)/);
  assert.match(once, /page\.reload\(/);
});

// 2026-11-21: three `plan` runs of some 90 s each read no flights; Google had shown "Oops, something
// went wrong." with a Reload button, and both flight readers waited out a fixed 35 s instead.
test('both flight readers wait through waitForFlightResults, which presses Reload on Google\'s error', () => {
  assert.match(body('searchFlights'), /await waitForFlightResults\(page\);/);
  assert.doesNotMatch(body('searchFlights'), /timeout: 35_000/);
  const book = body('bookFlight');
  assert.ok(book.indexOf('await waitForFlightResults(page)') >= 0 && book.indexOf('await waitForFlightResults(page)') < book.indexOf('await readRows([])'));
  const wait = source.slice(source.indexOf('async function waitForFlightResults'), source.indexOf('/**\n * Google Flights for a route'));
  assert.match(wait, /getByRole\('button', \{ name: 'Reload' \}\)/);
  assert.match(wait, /page\.reload\(/);
  assert.match(wait, /state === 'ready'\) return;/);
});

test('Google\'s error after its own Reload is a limit; on the booking page it only leaves that page unread', () => {
  const wait = source.slice(source.indexOf('async function waitForFlightResults'), source.indexOf('/**\n * Google Flights for a route'));
  assert.match(wait, /state === 'google-error' && reloads >= 1 && Date\.now\(\) - loadedAt >= FLIGHT_RELOAD_SETTLE_MS\) throw new GoogleLimitedError\(\)/);
  assert.match(wait, /state === 'google-error' \? reloads === 0 :/);
  assert.match(source, /export class GoogleLimitedError extends GoogleBlockedError/);
  assert.match(body('bookFlight'), /error instanceof GoogleLimitedError\) return true; throw error; \}\)\) return null;/);
});
