// 2026-11-12: Osaka 04/22–04/25 read no flights, and the same search a minute later read 101.
import { describe, it, expect } from 'vitest';
import { captureExplorePage, retryIfEmpty, sortedByLowestPrice } from './runner';

describe('an empty flight page is tried once more', () => {
  it('returns the second try when the first is empty, without waiting for real', async () => {
    const answers = [null, 'flights'];
    const waits: number[] = [];
    const found = await retryIfEmpty(async () => answers.shift() ?? null, 8_000, async ms => { waits.push(ms); });
    expect(found).toBe('flights');
    expect(waits).toEqual([8_000]);
  });
  it('does not retry a page that had results, and gives up after one retry', async () => {
    let calls = 0;
    expect(await retryIfEmpty(async () => { calls++; return 'ok'; }, 0, async () => {})).toBe('ok');
    expect(calls).toBe(1);
    calls = 0;
    expect(await retryIfEmpty(async () => { calls++; return null; }, 0, async () => {})).toBeNull();
    expect(calls).toBe(2);
  });
});

// 2026-11-21: Japan two-week scans failed twice in a row; Google had shown "Oops, something went wrong."
// in place of the cards after the trip-length switch, and loading the page again read 24 cities.
describe('an Explore page without prices is loaded once more', () => {
  const cards = 'Osaka\nDec 26, 2026 – Jan 11, 2027\nNonstop\n2 hr 30 min\nNT$6,565';
  const oops = '2-week trip in the next 6 months\nOops, something went wrong.';
  const target = { origin: 'Taipei', region: 'Japan', currency: 'TWD', country: 'TW', tripLength: 'two_weeks' };
  it('loads again after Google\'s error page and returns the cards', async () => {
    const pages = [oops, cards];
    const waits: number[] = [];
    expect(await captureExplorePage(target, async () => pages.shift()!, async ms => { waits.push(ms); })).toBe(cards);
    expect(waits).toEqual([8_000]);
  });
  it('loads once when the cards are there, and returns the error page after a second failure', async () => {
    let loads = 0;
    expect(await captureExplorePage(target, async () => { loads++; return cards; }, async () => {})).toBe(cards);
    expect(loads).toBe(1);
    loads = 0;
    expect(await captureExplorePage(target, async () => { loads++; return oops; }, async () => {})).toBe(oops);
    expect(loads).toBe(2);
  });
});

// 2026-11-21: Enka Nanba 1 (NT$1,130, 51% below usual) was missing from relevance-ordered lists.
describe('the hotel list asks Google for lowest price first', () => {
  it('reads the sort control as Google labels it', () => {
    expect(sortedByLowestPrice('Lowest price, Sort by, Selected')).toBe(true);
    expect(sortedByLowestPrice('Sort by, Not selected')).toBe(false);
    expect(sortedByLowestPrice('Price, Not selected')).toBe(false);
    expect(sortedByLowestPrice(null)).toBe(false);
  });
});
