// Trips the user asked to watch ("11 月東京降價叫我"): the cheapest flight for the dates and,
// optionally, a hotel from a hotel list. "deals jobs" checks them every few hours and sends an alert
// when either drops by a real amount of money. The saved list and the drop rules, out of cli.ts
// (2026-11-20 refactor); cli.ts looks the prices up and words the alerts.
import { JsonFile, dataFile, listOf } from './state';

export const FLIGHT_DROP = 300; // whole round trip, list currency
export const HOTEL_DROP = 200; // per night

export type WatchedHotel = {
  name: string;
  /** Google's list price, which later checks compare with. */
  nightly: number;
  /** The price the hotel list showed (a site's price for the room), which the alert quotes. */
  shown?: number;
};
export type TripWatch = {
  id: number; origin: string; currency: string; country: string; city: string; outboundDate: string; returnDate: string;
  flight: number | null; hotel: WatchedHotel | null; createdAt: string; lastCheckedAt: string | null;
};
export type WatchStay = { origin: string; currency: string; country: string; city: string; outboundDate: string; returnDate: string };

export const watchesFile = (path = dataFile('trip-watches.json')) => new JsonFile<TripWatch[]>(path, () => [], listOf<TripWatch>('盯價行程'));

/** The watch for these dates, or a new one added to `watches` (numbered after the highest). */
export function findOrAddWatch(watches: TripWatch[], stay: WatchStay, now = new Date()): { watch: TripWatch; added: boolean } {
  const same = watches.find(watch => watch.city === stay.city && watch.outboundDate === stay.outboundDate && watch.returnDate === stay.returnDate);
  if (same) return { watch: same, added: false };
  const watch: TripWatch = { id: Math.max(0, ...watches.map(entry => entry.id)) + 1, origin: stay.origin, currency: stay.currency, country: stay.country,
    city: stay.city, outboundDate: stay.outboundDate, returnDate: stay.returnDate, flight: null, hotel: null, createdAt: now.toISOString(), lastCheckedAt: null };
  watches.push(watch);
  return { watch, added: true };
}

/** Watches whose trip has not started; the others are dropped when the list is next saved. */
export const upcoming = (watches: readonly TripWatch[], today: string) => watches.filter(watch => watch.outboundDate > today);

/**
 * Today's cheapest fare against the watch. `alert` when it is FLIGHT_DROP or more under the bar.
 * The bar becomes the lowest fare seen, so the next alert means a further drop.
 */
export function checkFlight(bar: number | null, fare: number): { alert: { from: number; to: number } | null; bar: number } {
  const alert = bar !== null && fare <= bar - FLIGHT_DROP ? { from: bar, to: fare } : null;
  return { alert, bar: bar === null ? fare : Math.min(bar, fare) };
}

/**
 * Google's price for the watched hotel now, against the watch. `alert` when it is HOTEL_DROP or more
 * lower. The alert quotes the price the list showed, moved by Google's drop (2026-10-19: the watch
 * quoted Google's lower list price, NT$1,386, for a hotel listed at NT$1,801). Any fall moves both
 * prices down, so the next alert means a further drop.
 */
export function checkHotel(hotel: WatchedHotel, now: number): { alert: { from: number; to: number; drop: number } | null; hotel: WatchedHotel } {
  const drop = hotel.nightly - now;
  const before = hotel.shown ?? hotel.nightly;
  const alert = now <= hotel.nightly - HOTEL_DROP ? { from: before, to: before - drop, drop } : null;
  if (now >= hotel.nightly) return { alert, hotel };
  return { alert, hotel: { ...hotel, nightly: now, ...(hotel.shown !== undefined ? { shown: hotel.shown - drop } : {}) } };
}
