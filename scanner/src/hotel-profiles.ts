// What Booking.com shows of each hotel, kept for a month in trip-bot.db (`hotel_profiles`):
// its room types, beds, bathrooms and photos do not change from one stay to the next.
// Replaces the single growing hotel-profiles.json file with O(1) SQLite lookups + country tagging.
import { existsSync, readFileSync } from 'node:fs';
import type { BookingRoom } from './deals/booking-rooms';
import { bookingCountryCode } from './deals/hotel-page';
import { dataFile } from './state';
import { countHotelProfiles, getHotelProfile, listHotelProfiles, transaction, upsertHotelProfile, type StoredHotelProfile } from './store';

export interface HotelProfile {
  name: string;
  /** Booking's page for the hotel, or null when Booking was searched and had nothing. */
  hotelUrl: string | null;
  countryCode?: string | null;
  rooms: BookingRoom[];
  fetchedAt: string;
}

const LEGACY_PROFILES_FILE = dataFile('hotel-profiles.json');
export const PROFILE_DAYS = 30;
// A hotel Booking did not show (or a visit Booking challenged) is tried again after this long.
export const RETRY_MISSING_DAYS = 2;

/** One hotel however Google spells it ("Mr . KINJO" / "Mr.KINJO"). */
export const profileKey = (name: string) => name.toLowerCase().normalize('NFKD').replace(/[^\p{L}\p{N}]+/gu, '');

function fromStored(row: StoredHotelProfile): HotelProfile {
  let rooms: BookingRoom[] = [];
  try { rooms = JSON.parse(row.roomsJson) as BookingRoom[]; } catch { rooms = []; }
  return { name: row.name, hotelUrl: row.hotelUrl, countryCode: row.countryCode, rooms, fetchedAt: row.fetchedAt };
}

/** Import legacy hotel-profiles.json rows into trip-bot.db, tagging each with its Booking country code. */
export function importLegacyProfiles(file = LEGACY_PROFILES_FILE, options: { skipCountries?: readonly string[] } = {}): { imported: number; skipped: number } {
  if (!existsSync(file)) return { imported: 0, skipped: 0 };
  let parsed: Record<string, HotelProfile>;
  try { parsed = JSON.parse(readFileSync(file, 'utf8')) as Record<string, HotelProfile>; } catch { return { imported: 0, skipped: 0 }; }
  const skip = new Set((options.skipCountries ?? []).map(code => code.toLowerCase()));
  let imported = 0;
  let skipped = 0;
  transaction(() => {
    for (const [rawKey, profile] of Object.entries(parsed)) {
      if (!profile || typeof profile.name !== 'string' || typeof profile.fetchedAt !== 'string') continue;
      const countryCode = profile.countryCode ?? bookingCountryCode(profile.hotelUrl);
      if (countryCode && skip.has(countryCode.toLowerCase())) { skipped++; continue; }
      upsertHotelProfile({
        key: profileKey(profile.name) || rawKey,
        name: profile.name,
        hotelUrl: profile.hotelUrl ?? null,
        countryCode: countryCode ?? null,
        roomsJson: JSON.stringify(Array.isArray(profile.rooms) ? profile.rooms : []),
        fetchedAt: profile.fetchedAt,
      });
      imported++;
    }
  });
  return { imported, skipped };
}

let autoMigrated = false;
function ensureMigrated(): void {
  if (autoMigrated) return;
  autoMigrated = true;
  if (countHotelProfiles() === 0 && existsSync(LEGACY_PROFILES_FILE)) {
    importLegacyProfiles(LEGACY_PROFILES_FILE, { skipCountries: ['tw'] });
  }
}

export function readProfiles(): Record<string, HotelProfile> {
  ensureMigrated();
  const out: Record<string, HotelProfile> = {};
  for (const row of listHotelProfiles()) out[row.key] = fromStored(row);
  return out;
}

/** A hotel's profile while it is fresh: a month when Booking showed it, two days when it did not. */
export function freshProfile(name: string, now = Date.now(), profiles?: Record<string, HotelProfile>): HotelProfile | null {
  const key = profileKey(name);
  let profile: HotelProfile | null;
  if (profiles) {
    profile = profiles[key] ?? null;
  } else {
    ensureMigrated();
    const row = getHotelProfile(key);
    profile = row ? fromStored(row) : null;
  }
  if (!profile) return null;
  const days = profile.hotelUrl && profile.rooms.length ? PROFILE_DAYS : RETRY_MISSING_DAYS;
  return now - Date.parse(profile.fetchedAt) < days * 86_400_000 ? profile : null;
}

export function saveProfile(profile: HotelProfile): void {
  ensureMigrated();
  const countryCode = profile.countryCode ?? bookingCountryCode(profile.hotelUrl);
  upsertHotelProfile({
    key: profileKey(profile.name),
    name: profile.name,
    hotelUrl: profile.hotelUrl ?? null,
    countryCode: countryCode ?? null,
    roomsJson: JSON.stringify(profile.rooms ?? []),
    fetchedAt: profile.fetchedAt,
  });
}
