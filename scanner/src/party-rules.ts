// Which rooms suit the party and what the trip costs them: the rules the user set over October 2026,
// taken out of cli.ts (2026-11-20 refactor) so each one is tested on its own, without opening a page.
import type { BookingRoom } from './deals/booking-rooms';
import { isDormBed, isSmokingRoom, type RoomCheck, type RoomOffer } from './deals/hotel-page';
import { roomType } from './format';

export interface Party {
  travelers: number;
  /** The user minds the smell: smoking rooms are left out. */
  nonSmoking: boolean;
}

/** Hotel prices are per room for two, so a party needs ceil(people / 2) rooms. */
export const roomsNeeded = (travelers: number) => Math.ceil(travelers / 2);

/** Flight plus stay for the whole party, and each person's share. Fares are per person. */
export function partyCost(fare: number, nightly: number, nights: number, travelers: number) {
  const stay = nightly * nights * roomsNeeded(travelers);
  const total = fare * travelers + stay;
  return { stay, total, each: Math.round(total / travelers) };
}

export const nightsOf = (stay: { outboundDate: string; returnDate: string }) =>
  Math.round((Date.parse(`${stay.returnDate}T00:00:00Z`) - Date.parse(`${stay.outboundDate}T00:00:00Z`)) / 86_400_000);

/**
 * The room a hotel from Google's list is offered with, and its nightly price, or null when none suits
 * the party. `quoted` is the price the hotel is listed at (the recommended site's, else Google's).
 * - Smoking rooms are left out when the user minds the smell (a hotel with nothing else goes).
 * - Two or more travellers need a room for two: no single room, and no shared bathroom (the user
 *   asked whether it meant bathing with strangers, 2026-10-18); the price is never under that room's.
 *   Three lists offered a single room to two people, priced as one (2026-10-18).
 * - One traveller takes the cheapest room; a hotel that also sells dorm beds, or whose cheapest room
 *   was left out, is priced no lower than the room offered.
 */
export function roomForParty(check: RoomCheck, quoted: number, party: Party): { room: RoomOffer; nightly: number } | null {
  if (check.status !== 'private') return null;
  const options = check.options.filter(option => !(party.nonSmoking && isSmokingRoom(option.name)));
  if (!options.length) return null;
  if (party.travelers >= 2) {
    const fits = options.filter(option => roomType(option.name) !== '單人房');
    const forTwo = fits.find(option => !/共用衛浴/.test(roomType(option.name)));
    return forTwo ? { room: forTwo, nightly: Math.max(forTwo.nightly, quoted) } : null;
  }
  const room = options[0]!;
  return { room, nightly: check.alsoShared || room !== check.room ? Math.max(room.nightly, quoted) : quoted };
}

export const hasBunk = (room: BookingRoom) => room.bunkInPhotos || room.beds.some(bed => /bunk/i.test(bed));

/**
 * What a Booking room is, for the room-photo list. Booking says who a room is for ("for 1 Adult"):
 * a one-guest room is a single whatever its name. Bunk beds decide over the name ("Superior Twin Room"
 * with "1 bunk bed"). A name that says no type is read from the beds: two or more beds of any size
 * ("2 small double beds") is a twin, one big bed a double.
 */
export function roomTypeLabel(room: BookingRoom): string {
  if (room.guests === 1) return '單人房';
  if (hasBunk(room)) return '上下鋪房';
  const named = roomType(room.name);
  if (named !== '私人房') return named;
  const bedCount = room.beds.reduce((sum, bed) => sum + Number(/^\d+/.exec(bed)?.[0] ?? 1), 0);
  return bedCount >= 2 ? '雙床房' : room.beds.some(bed => /\b(double|queen|king)\b/i.test(bed)) ? '雙人房' : named;
}

/**
 * A hotel's rooms sorted for the room-photo list. Dorm beds are no room for the user; smoking rooms go
 * when they mind the smell (they were shown one, 2026-10-19); rooms too small for the party go too
 * (two people are never offered a single, 2026-10-18). The rest are `shown`.
 */
export function sortRooms(rooms: readonly BookingRoom[], party: Party) {
  const dormBeds = rooms.filter(room => isDormBed(room.name));
  const smoking = party.nonSmoking ? rooms.filter(room => isSmokingRoom(room.name)) : [];
  const tooSmall = rooms.filter(room => room.guests !== null && room.guests < Math.min(party.travelers, 2));
  const shown = rooms.filter(room => !dormBeds.includes(room) && !smoking.includes(room) && !tooSmall.includes(room));
  return { shown, dormBeds, smoking, tooSmall };
}

/**
 * A listed hotel's Booking rooms checked again before the list goes out. Google's room names hide bunk
 * beds and shared bathrooms (Minn Kamata's "Quadruple Room" is bunks, DOYANEN BAKURO's twin a bunk with
 * a shared bathroom). `fits`: rooms big enough that are not dorm beds; `forParty`: those that are also
 * not smoking (when it matters), not bunks and not shared-bathroom. `keep` is false when Booking lists
 * rooms that fit but none for the party: such a hotel is poor value for two and is left out.
 */
export function bookingRoomsForParty(rooms: readonly BookingRoom[], party: Party) {
  const fits = rooms.filter(room => !isDormBed(room.name) && (room.guests === null || room.guests >= Math.min(party.travelers, 2)));
  const forParty = fits.filter(room => !(party.nonSmoking && isSmokingRoom(room.name)) && !hasBunk(room) && room.bathroom !== 'shared');
  return { fits, forParty, keep: !fits.length || forParty.length > 0 };
}
