// The party's room and cost rules, each on its own (taken out of cli.ts, 2026-11-20). The cases are
// the ones the user found in October 2026.
import { test } from 'vitest';
import assert from 'node:assert/strict';
import type { BookingRoom } from './deals/booking-rooms';
import type { RoomCheck, RoomOffer } from './deals/hotel-page';
import { bedsSleep, bedsTooFew, bookingRoomsForParty, nightsOf, partyCost, roomForParty, roomTypeLabel, roomsNeeded, sortRooms } from './party-rules';

const two = { travelers: 2, nonSmoking: true };
const one = { travelers: 1, nonSmoking: false };

const room = (name: string, extra: Partial<BookingRoom> = {}): BookingRoom => ({
  name, guests: null, stayTotal: null, freeCancelUntil: null, sizeM2: null, beds: [], bathroom: null, photos: [],
  bunkInPhotos: false, resale: false, ...extra,
});
const privateCheck = (options: RoomOffer[], alsoShared = false): RoomCheck =>
  ({ status: 'private', room: options[0]!, options, alsoShared });

test('rooms and costs for the party', () => {
  assert.deepEqual([1, 2, 3, 4, 5].map(roomsNeeded), [1, 1, 2, 2, 3]);
  // 3 people: 2 rooms; fares are per person.
  assert.deepEqual(partyCost(5000, 2000, 3, 3), { stay: 12000, total: 27000, each: 9000 });
  assert.equal(nightsOf({ outboundDate: '2026-12-20', returnDate: '2026-12-25' }), 5);
});

test('two people are never given a single room, priced as one (2026-10-18)', () => {
  const check = privateCheck([{ name: 'Single Room', nightly: 1200 }, { name: 'Double Room', nightly: 2100 }]);
  assert.deepEqual(roomForParty(check, 1200, two), { room: { name: 'Double Room', nightly: 2100 }, nightly: 2100, bedsUnconfirmed: false });
  assert.equal(roomForParty(privateCheck([{ name: 'Single Room', nightly: 1200 }]), 1200, two), null);
});

test('two people get no shared bathroom (2026-10-18)', () => {
  const check = privateCheck([{ name: 'Twin Room with Shared Bathroom', nightly: 1500 }, { name: 'Twin Room', nightly: 2300 }]);
  assert.equal(roomForParty(check, 1500, two)?.room.name, 'Twin Room');
  assert.equal(roomForParty(privateCheck([{ name: 'Double Room - Shared Bathroom', nightly: 1500 }]), 1500, two), null);
});

test('smoking rooms go when the user minds the smell, and a hotel with nothing else goes', () => {
  assert.equal(roomForParty(privateCheck([{ name: 'Smoking Double Room', nightly: 1800 }]), 1800, two), null);
  assert.equal(roomForParty(privateCheck([{ name: 'Smoking Double Room', nightly: 1800 }]), 1800, { travelers: 2, nonSmoking: false })?.nightly, 1800);
});

test('the price is never under the room offered', () => {
  // A site quotes less than the room for two costs: the room's price.
  assert.equal(roomForParty(privateCheck([{ name: 'Double Room', nightly: 2600 }]), 2000, two)?.nightly, 2600);
  // One traveller: the listed price, unless dorm beds are sold too or the cheapest room was left out.
  assert.equal(roomForParty(privateCheck([{ name: 'Double Room', nightly: 2600 }]), 2000, one)?.nightly, 2000);
  assert.equal(roomForParty(privateCheck([{ name: 'Double Room', nightly: 2600 }], true), 2000, one)?.nightly, 2600);
  assert.equal(roomForParty({ status: 'shared' }, 2000, one), null);
  assert.equal(roomForParty({ status: 'unknown' }, 2000, one), null);
});

test('what a Booking room is', () => {
  assert.equal(roomTypeLabel(room('Standard Room (for 1 Adult)', { guests: 1, beds: ['1 double bed'] })), '單人房');
  assert.equal(roomTypeLabel(room('Superior Twin Room', { beds: ['1 bunk bed'] })), '上下鋪房');
  assert.equal(roomTypeLabel(room('Superior Twin Room', { bunkInPhotos: true })), '上下鋪房');
  assert.equal(roomTypeLabel(room('Standard Room (for 2 Adults)', { beds: ['2 small double beds'] })), '雙床房');
  assert.equal(roomTypeLabel(room('Standard Room', { beds: ['1 queen bed'] })), '雙人房');
  assert.equal(roomTypeLabel(room('Standard Room')), '私人房');
  assert.equal(roomTypeLabel(room('Deluxe Twin Room', { guests: 2 })), '雙床房');
});

test('the room-photo list leaves out dorm beds, smoking and too-small rooms', () => {
  const rooms = [room('Bed in 6-Bed Dormitory Room'), room('Smoking Double Room'), room('Single Room', { guests: 1 }), room('Double Room', { guests: 2 })];
  const sorted = sortRooms(rooms, two);
  assert.deepEqual(sorted.shown.map(r => r.name), ['Double Room']);
  assert.equal(sorted.dormBeds.length, 1);
  assert.equal(sorted.smoking.length, 1);
  assert.equal(sorted.tooSmall.length, 1);
  // One traveller who does not mind smoking sees the single and the smoking room.
  assert.deepEqual(sortRooms(rooms, one).shown.map(r => r.name), ['Smoking Double Room', 'Single Room', 'Double Room']);
});

test("a hotel whose Booking rooms for two are all bunks or shared bathrooms is left out (Minn Kamata, DOYANEN BAKURO)", () => {
  const bunks = [room('Quadruple Room', { guests: 4, beds: ['2 bunk beds'] }), room('Twin Room', { guests: 2, bathroom: 'shared' })];
  assert.equal(bookingRoomsForParty(bunks, two).keep, false);
  const withGood = [...bunks, room('Double Room', { guests: 2, bathroom: 'private' })];
  assert.deepEqual(bookingRoomsForParty(withGood, two).forParty.map(r => r.name), ['Double Room']);
  // Booking showing no room that fits says nothing: the hotel stays.
  assert.equal(bookingRoomsForParty([], two).keep, true);
  assert.equal(bookingRoomsForParty([room('Single Room', { guests: 1 })], two).keep, true);
});

// 2026-11-20: Google listed Enka Nanba 1's "Basic Twin Room" with "1 single bed" and the list told two
// people ✅ 雙床房. A room whose stated beds sleep fewer than the party is not confirmed.
test('beds that sleep fewer than the party are not confirmed', () => {
  assert.equal(bedsSleep(['1 single bed']), 1);
  assert.equal(bedsSleep(['2 single beds']), 2);
  assert.equal(bedsSleep(['1 double bed']), 2);
  assert.equal(bedsSleep(['1 semi-double bed']), 2);
  assert.equal(bedsSleep(['1 bunk bed']), 2);
  assert.equal(bedsSleep(['1 single bed', '1 sofa bed']), 2);
  assert.equal(bedsSleep(undefined), null);
  assert.equal(bedsSleep(['1 large bed']), null); // an unknown size says nothing
  assert.equal(bedsTooFew(['1 single bed'], 2), true);
  assert.equal(bedsTooFew(['1 single bed'], 1), false);
  assert.equal(bedsTooFew(['1 single bed', '1 single bed'], 4), false); // a room for two is the most needed
  assert.equal(bedsTooFew(undefined, 2), false);

  const enka = privateCheck([{ name: 'Basic Twin Room', nightly: 1130, beds: ['1 single bed'] }]);
  assert.deepEqual(roomForParty(enka, 1130, two), { room: { name: 'Basic Twin Room', nightly: 1130, beds: ['1 single bed'] }, nightly: 1130, bedsUnconfirmed: true });
  // A room whose beds do sleep two is taken over the cheaper contradicting one.
  const both = privateCheck([{ name: 'Basic Twin Room', nightly: 1130, beds: ['1 single bed'] }, { name: 'Standard Twin Room', nightly: 1300, beds: ['2 single beds'] }]);
  assert.deepEqual(roomForParty(both, 1130, two)?.room.name, 'Standard Twin Room');
  assert.equal(roomForParty(both, 1130, two)?.bedsUnconfirmed, false);
  assert.equal(roomForParty(enka, 1130, one)?.bedsUnconfirmed, false);
});
