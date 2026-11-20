// Price-drop watches: when an alert goes out and how the bar moves (out of cli.ts, 2026-11-20).
import { test } from 'vitest';
import assert from 'node:assert/strict';
import { FLIGHT_DROP, HOTEL_DROP, checkFlight, checkHotel, findOrAddWatch, upcoming, type TripWatch } from './trip-watches';

const stay = { origin: 'Taipei', currency: 'TWD', country: 'TW', city: 'Osaka', outboundDate: '2026-12-20', returnDate: '2026-12-25' };

test('the same dates are one watch; new dates are numbered after the highest', () => {
  const watches: TripWatch[] = [];
  const first = findOrAddWatch(watches, stay, new Date('2026-11-20T00:00:00Z'));
  assert.equal(first.added, true);
  assert.equal(first.watch.id, 1);
  assert.equal(first.watch.createdAt, '2026-11-20T00:00:00.000Z');
  assert.equal(findOrAddWatch(watches, stay).added, false);
  watches[0]!.id = 5;
  assert.equal(findOrAddWatch(watches, { ...stay, returnDate: '2026-12-27' }).watch.id, 6);
  assert.equal(watches.length, 2);
});

test('trips that have started are dropped', () => {
  const watches = [{ outboundDate: '2026-11-20' }, { outboundDate: '2026-11-21' }] as TripWatch[];
  assert.deepEqual(upcoming(watches, '2026-11-20').map(w => w.outboundDate), ['2026-11-21']);
});

test(`a flight alert needs a drop of ${FLIGHT_DROP} or more, and the bar keeps the lowest fare`, () => {
  assert.deepEqual(checkFlight(10000, 10000 - FLIGHT_DROP), { alert: { from: 10000, to: 9700 }, bar: 9700 });
  assert.deepEqual(checkFlight(10000, 9800), { alert: null, bar: 9800 });
  assert.deepEqual(checkFlight(10000, 10500), { alert: null, bar: 10000 });
  assert.deepEqual(checkFlight(null, 9000), { alert: null, bar: 9000 });
});

test(`a hotel alert needs a drop of ${HOTEL_DROP} or more, quoted in the price the list showed (2026-10-19)`, () => {
  // Listed at NT$1,801 (a site's price) while Google's list price was NT$1,586; Google now NT$1,386.
  const { alert, hotel } = checkHotel({ name: 'Hotel', nightly: 1586, shown: 1801 }, 1386);
  assert.deepEqual(alert, { from: 1801, to: 1601, drop: 200 });
  assert.deepEqual(hotel, { name: 'Hotel', nightly: 1386, shown: 1601 });
});

test('a smaller fall moves the bar without an alert; a rise changes nothing', () => {
  assert.deepEqual(checkHotel({ name: 'H', nightly: 2000, shown: 2200 }, 1900), { alert: null, hotel: { name: 'H', nightly: 1900, shown: 2100 } });
  assert.deepEqual(checkHotel({ name: 'H', nightly: 2000 }, 1900), { alert: null, hotel: { name: 'H', nightly: 1900 } });
  assert.deepEqual(checkHotel({ name: 'H', nightly: 2000 }, 2300), { alert: null, hotel: { name: 'H', nightly: 2000 } });
});
