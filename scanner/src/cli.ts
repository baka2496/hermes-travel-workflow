// trip-bot command line: what the Hermes trip agent runs, and the scheduled task's tick.
//   report [weekend|week|two_weeks|all] [--limit N]   latest cheapest cities and recent promotions
//   regions                                            list scanned regions and their health
//   add-region <English name> <中文名稱>               start scanning a region (all three trip lengths)
//   remove-region <English name>                       stop scanning a region (history is kept)
//   scan-now                                           scan every active region at the next tick (within 15 minutes)
//   trip <city> <out> <ret>                            flights for chosen dates (airlines, times, verdict), saved as option T1
//   hotels-for <code> [--nearby]                       hotels for the city (or cheaper nearby areas) and dates of a report option
//   watch <code> / watch-hotel <n> / watches / unwatch <n>  price-drop alerts for a trip (and a hotel from the last list)
//   tick                                               every 15 min (scheduled task): due region scans, daily lists, alerts, then jobs
//   jobs                                               background: prefetch hotels, check watches, daily self-check
//   rooms <n>                                          room types of hotel n from the last hotel list, with photos (Booking.com)
//   travelers [N]                                      show or set how many people travel together (1-9)
// This portable build prints locally; Hermes owns account setup and delivery.
import { CITY_NAMES, cityLabel } from './deals/message';
import { TRIP_LENGTHS, buildCardSearchUrl, isTripLength, type TripLength } from './deals/explore-page';
import { buildHotelNameUrl, hotelArea, inTown, isNonSmokingRoom, isPrivateHotel, isSmokingRoom, nearbyAreas, parseGoogleRoomRow, roomKey, type HotelListQuote, type SiteOffer, type HotelPhotos } from './deals/hotel-page';
import { bookFlight, captureHotelList, checkHotelRooms, deliverDealAlerts, NoFittingFlight, retryIfEmpty, runDueDealWatches, searchFlights } from './deals/runner';
import type { FlightOption } from './deals/flight-list';
import { SHORT_TRIP_NIGHTS, chooseBookingOption, chooseFlight, chooseReturn, hotelTimes, readPrefs, type Prefs } from './prefs';
import { createWatch, findWatch, listWatches as listRegionWatches, observations, pruneObservations, recentAlerts, scheduleAllNow, setRegionActive, updateWatch, type AlertMessage } from './store';
import { bookingSearchUrl, bookingUrlFromGoogle, fetchBookingRooms, type BookingRoom } from './deals/booking-rooms';
import { fetchTripRooms, tripHotelIdFromGoogle } from './deals/trip-rooms';
import type { MarketLevel } from './google/market-insight';
import { sendToTelegram } from './telegram';
import { airlineName, bedText, flightLines, noPlanReason, phoneLines, shortDuration, withAdults, listPhoto, money, recommendedSite, roomType, siteForPrice, siteName } from './format';
import { readdirSync, readFileSync } from 'node:fs';
import { auditFlights, auditStays, type AuditPick } from './audit';
import { createHash } from 'node:crypto';
import { freshProfile, saveProfile } from './hotel-profiles';
import { spawnSync } from 'node:child_process';
import { checkOutput } from './rules';
import { Output } from './output';
import { JsonFile, dataFile, objectOf, writeAtomic, DATA_DIR } from './state';
import { bedsTooFew, bookingRoomsForParty, nightsOf, partyCost as costForParty, roomForParty, roomTypeLabel, roomsNeeded as roomsNeededFor, sortRooms, type Party } from './party-rules';
import { join } from 'node:path';
import { FLIGHT_DROP, HOTEL_DROP, checkFlight, checkHotel, findOrAddWatch as addWatch, upcoming, watchesFile, type TripWatch, type WatchStay } from './trip-watches';

/** What the command shows: printed, or with --check kept (lines, albums, buttons) for the rule check. */
const out = new Output(process.argv.includes('--check'));
const say = (...parts: unknown[]) => out.say(...parts);
const beginSegment = (photos: string[] = [], own = false) => out.album(photos, own);
const FLIGHT_LIST_BUTTON = '✈️ 機票列表';
const NEARBY_BUTTON = '住遠一點';
const BUTTONS_PER_ROW = 3;

/**
 * How many people travel together, kept in settings.json beside this file. Fares are per person
 * (Google's prices are for one adult); hotel prices are per room for two, so a party needs
 * ceil(people / 2) rooms.
 */
type Settings = { travelers?: unknown; prefs?: unknown; [key: string]: unknown };
const settingsFile = new JsonFile<Settings>(dataFile('settings.json'), () => ({}), objectOf<Settings>('設定'));
const MAX_TRAVELERS = 9;
function readTravelers(): number {
  const value = Number(settingsFile.read().travelers);
  return Number.isInteger(value) && value >= 1 && value <= MAX_TRAVELERS ? value : 1;
}
// Read when the command starts, inside its error handling, so an unreadable settings file is reported.
let travelers = 1;
let roomsNeeded = 1;
const party = (): Party => ({ travelers, nonSmoking: readUserPrefs().nonSmoking });
/** Flight plus stay for the whole party, and each person's share. */
const partyCost = (fare: number, nightly: number, nights: number) => costForParty(fare, nightly, nights, travelers);

const LENGTHS: TripLength[] = ['weekend', 'week', 'two_weeks'];
const taipei = (date: Date | null) => date
  ? new Intl.DateTimeFormat('zh-TW', { timeZone: 'Asia/Taipei', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
  : '尚未掃描';
const md = (iso: string) => iso.slice(5).replace('-', '/');
/** "10/31(六)–11/02(一)": the weekdays tell how many days off a trip takes (the user asked, 2026-10-19). */
const WEEKDAYS = '日一二三四五六';
const weekday = (iso: string) => WEEKDAYS[new Date(`${iso}T00:00:00Z`).getUTCDay()]!;
const dateRange = (from: string, to: string) => `${md(from)}(${weekday(from)})–${md(to)}(${weekday(to)})`;
const PLACE = /^[A-Za-z][A-Za-z .'-]{0,48}$/;

const LENGTH_ICON: Record<TripLength, string> = { weekend: '🗓', week: '🧳', two_weeks: '🏝' };
const HISTORY_DAYS = 30;
const MIN_HISTORY_SCANS = 2;

/**
 * Each city's usual price for a watch: the median of the cheapest fare of
 * every earlier scan in the last 30 days (the latest scan excluded).
 */
async function usualPrices(watch: { id: string; lastScanAt: Date | null }) {
  const usual = new Map<string, { median: number; scans: number }>();
  if (!watch.lastScanAt) return usual;
  const rows = observations(watch.id, { before: watch.lastScanAt, since: new Date(watch.lastScanAt.getTime() - HISTORY_DAYS * 86_400_000) });
  const perCity = new Map<string, Map<number, number>>();
  for (const row of rows) {
    const scans = perCity.get(row.city) ?? new Map<number, number>();
    scans.set(row.scannedAt.getTime(), Math.min(scans.get(row.scannedAt.getTime()) ?? Infinity, row.price));
    perCity.set(row.city, scans);
  }
  for (const [city, scans] of perCity) {
    if (scans.size < MIN_HISTORY_SCANS) continue;
    const prices = [...scans.values()].sort((a, b) => a - b);
    const middle = Math.floor(prices.length / 2);
    usual.set(city, { median: prices.length % 2 ? prices[middle]! : (prices[middle - 1]! + prices[middle]!) / 2, scans: scans.size });
  }
  return usual;
}

/**
 * "比平常便宜 NT$800" style tag against the city's usual price, in money
 * rather than percent. Within 5% counts as the usual price. Empty while the
 * history is too short.
 */
function versusUsual(price: number, usual: { median: number } | undefined, currency: string): string {
  if (!usual) return '';
  const difference = Math.round(price - usual.median);
  if (Math.abs(difference) < usual.median * 0.05) return '・≈ 平常價';
  return difference < 0 ? `・比平常便宜 ${money(-difference, currency)}` : `・比平常貴 ${money(difference, currency)}`;
}

const MARKET_TEXT: Record<MarketLevel, string> = {
  low: '✅ 偏低（比這條航線平常便宜）', typical: '➖ 一般（跟平常差不多）', high: '⚠️ 偏高（比平常貴）',
};

/**
 * Active watches in a fixed order. Each gets a letter (A, B, C …) and its
 * cities are numbered by price, so an option code such as B2 means "second
 * cheapest city of the second watch" in both `report` and `hotels-for`,
 * whatever --limit the report used. Codes are stable until the next scan.
 */
async function activeWatches() {
  return listRegionWatches({ active: true })
    .sort((a, b) => a.region.localeCompare(b.region) || LENGTHS.indexOf(a.tripLength as TripLength) - LENGTHS.indexOf(b.tripLength as TripLength))
    .map((watch, index) => ({ watch, letter: String.fromCharCode(65 + index) }));
}

/**
 * Telegram-ready Markdown: one line per city with an option code; the city
 * name links to the dated Google Flights search where the fare is booked.
 */
async function report(filter: string, limit: number) {
  const lengths = filter === 'all' ? LENGTHS : isTripLength(filter) ? [filter] : null;
  if (!lengths) throw new Error('行程長度請用 weekend、week、two_weeks 或 all');
  const lettered = (await activeWatches()).filter(({ watch }) => lengths.includes(watch.tripLength as TripLength));
  if (!lettered.length) { say('目前沒有啟用中的掃描地區。'); return; }
  const regionsShown = new Set<string>();
  for (const { watch, letter } of lettered) {
    if (!regionsShown.has(watch.region)) {
      regionsShown.add(watch.region);
      say(`${regionsShown.size > 1 ? '\n' : ''}✈️ *${watch.originLabel} → ${watch.regionLabel} 來回機票最低價*`);
    }
    const length = watch.tripLength as TripLength;
    // Phone-width lines: two short lines per option instead of one long wrapping one.
    say(`\n${LENGTH_ICON[length] ?? '•'} *${TRIP_LENGTHS[length]?.label ?? length}*`);
    if (watch.lastError) say(`⚠️ 上次掃描失敗`);
    if (!watch.lastScanAt) continue;
    const rows = observations(watch.id, { scannedAt: watch.lastScanAt }).slice(0, limit);
    const usual = await usualPrices(watch);
    const codes = rows.map((row, index) => `${letter}${index + 1} ${cityLabel(row.city)}`);
    for (let start = 0; start < codes.length; start += BUTTONS_PER_ROW) out.keyboard.push(codes.slice(start, start + BUTTONS_PER_ROW));
    rows.forEach((row, index) => {
      const url = buildCardSearchUrl(watch.origin, row, row.currency);
      const versus = versusUsual(row.price, usual.get(row.city), row.currency);
      say(`*${letter}${index + 1}* [${cityLabel(row.city)}](${url}) ${money(row.price, row.currency)}${row.stops === 0 ? '' : ` 轉${row.stops}`}`);
      for (const line of phoneLines([dateRange(row.outboundDate, row.returnDate), versus.replace(/^・/, '')])) say(line);
    });
  }
  const since = new Date(Date.now() - 7 * 86_400_000);
  const deals = recentAlerts({ kind: 'deal', since, tripLengths: lengths, limit: 10 });
  if (deals.length) {
    say(`\n🔥 *最近的促銷*`);
    for (const deal of deals) {
      const message = deal.message as { title?: string; url?: string; body?: string };
      // Alerts saved before 2026-10-19 carry the older, wider title "日本促銷（週末）：帶廣 NT$16,359".
      const title = (message.title ?? '促銷').replace(/^.+促銷（(.+)）：(.+)$/, '$2・$1促銷');
      say(`• ${message.url ? `[${title}](${message.url})` : title}`);
      // Its dates, from the alert's second line ("10/30–11/02・直飛"): a promotion is for certain days.
      const when = /(\d\d)\/(\d\d)–(\d\d)\/(\d\d)/.exec(message.body ?? '');
      if (when) {
        // The alert names no year: the next such date from today.
        const iso = (month: string, day: string) => {
          const year = new Date().getUTCFullYear();
          const thisYear = `${year}-${month}-${day}`;
          return Date.parse(`${thisYear}T00:00:00Z`) >= Date.now() - 86_400_000 ? thisYear : `${year + 1}-${month}-${day}`;
        };
        say(dateRange(iso(when[1]!, when[2]!), iso(when[3]!, when[4]!)));
      }
    }
  }
  out.keyboard.push([FLIGHT_LIST_BUTTON]);
  say('\n🏨 點按鈕看住宿');
  say(travelers > 1
    ? `每人來回價（${travelers} 人同行）\n行李另計・直飛不另標示`
    : '1 人來回價\n行李另計・直飛不另標示');
}

const MAX_HOTELS = 8;
const ROOM_CHECK_CANDIDATES = 15; // the cheapest well-rated hotels whose prices panel is opened to confirm a private room
const NEARBY_CANDIDATES = 4; // per nearby area
const NEARBY_PER_AREA = 2;
const GOOD_RATING = 3.5;

type Stay = { city: string; outboundDate: string; returnDate: string };
type Pick = { hotel: HotelListQuote & { ratingSource?: string | null }; nightly: number; note: string; sites: SiteOffer[]; photos?: HotelPhotos; area?: string };
/** The nightly price a pick is shown at: the linked site's, never under its own (printPicks). */
const shownNightly = (pick: { nightly: number; sites: readonly SiteOffer[] }) => Math.max(recommendedSite(pick.sites)?.nightly ?? 0, pick.nightly);

/**
 * Hotels in one place for a stay whose rooms were checked: the cheapest
 * private-class candidates are opened one by one, dorm-only ones are dropped,
 * confirmed private rooms come first priced at that room's rate.
 */
/**
 * Results kept for a few hours in cache/, so a hotel list the background job already fetched
 * ("deals prefetch") opens in seconds instead of a minute or two of page loads.
 */
const CACHE_DIR = join(DATA_DIR, 'cache');
const CACHE_HOURS = 6;

async function cached<T>(kind: string, key: readonly unknown[], compute: () => Promise<T | null>, fresh = false): Promise<T | null> {
  const file = join(CACHE_DIR, `${kind}-${createHash('sha1').update(JSON.stringify(key)).digest('hex').slice(0, 16)}.json`);
  if (!fresh) {
    try {
      const saved = JSON.parse(readFileSync(file, 'utf8')) as { savedAt: string; value: T };
      if (Date.now() - Date.parse(saved.savedAt) < CACHE_HOURS * 3_600_000) return saved.value;
    } catch { /* no usable cache entry */ }
  }
  const value = await compute();
  if (value !== null) {
    writeAtomic(file, JSON.stringify({ savedAt: new Date().toISOString(), key, value }));
  }
  return value;
}

const cachedStays = (watch: { currency: string; country: string }, stay: Stay, candidatesWanted: number, limit: number, fresh = false) =>
  cached('stays', [stay, watch.currency, watch.country, candidatesWanted, limit, readUserPrefs().nonSmoking], () => verifiedStays(watch, stay, candidatesWanted, limit), fresh);

// A flight page without a verdict or flights is a failed load, not worth keeping.
const cachedFlights = (origin: string, stay: Stay, currency: string, country: string, fresh = false) =>
  cached('flights', [origin, stay, currency, country], () => retryIfEmpty(async () => {
    const found = await searchFlights(origin, stay, currency, country);
    return found.market || found.flights.length ? found : null;
  }), fresh);

async function verifiedStays(watch: { currency: string; country: string }, stay: Stay, candidatesWanted: number, limit: number) {
  const hotels = await captureHotelList(watch, stay);
  if (!hotels) return null;
  const prefs = readUserPrefs();
  const { nonSmoking } = prefs;
  // The checks go to the cheapest hotels the user's bars allow (class, rating), the value candidates;
  // others only when too few pass.
  const rooms = hotels.filter(isPrivateHotel);
  const eligible = rooms.filter(hotel => (hotel.stars ?? 0) >= prefs.minStars && hotel.rating !== null && hotel.rating >= prefs.minRating)
    .sort((a, b) => a.nightly - b.nightly);
  const rest = rooms.filter(hotel => !eligible.includes(hotel)).sort((a, b) => a.nightly - b.nightly);
  const candidates = [...eligible, ...rest].slice(0, candidatesWanted);
  const { rooms: checks, ratings, sites, photos, addresses } = await checkHotelRooms(watch, stay, candidates.map(hotel => hotel.name),
    new Map(candidates.flatMap(hotel => hotel.link ? [[hotel.name, hotel.link] as const] : [])));
  const rated = candidates.map(hotel => {
    const found = ratings.get(hotel.name);
    return { ...hotel, rating: hotel.rating ?? found?.score ?? null,
      ratingSource: hotel.rating !== null ? 'Google' : found?.source ?? null };
  });
  const shared = rated.filter(hotel => checks.get(hotel.name)?.status === 'shared');
  const verified = rated.flatMap(hotel => {
    const check = checks.get(hotel.name);
    // Rank and quote by the recommended site's price (oddly cheap offers such as dorm beds are skipped
    // there), else Google's "from" price; a hotel known to sell dorm beds too is priced no lower than
    // its cheapest private room.
    const quoted = recommendedSite(sites.get(hotel.name) ?? [])?.nightly ?? hotel.nightly;
    if (check?.status !== 'private') return [];
    // Out of town (Okinawa City for Naha, Kawaguchi for Tokyo) is too far to stay; the user found four such of six.
    const address = addresses.get(hotel.name);
    // Out of town is too far; an unread address is kept and marked (the user wants the best value, and
    // dropping such hotels could drop it), so an out-of-town hotel no longer passes as in town unmarked.
    if (address && !inTown(stay.city, hotelArea(address).postcode)) return [];
    // The room for the party (no smoking, no single or shared bathroom for two): src/party-rules.ts.
    const chosen = roomForParty(check, quoted, { travelers, nonSmoking });
    return chosen ? [{ hotel, ...chosen }] : [];
  });
  const areaOf = (name: string) => { const address = addresses.get(name); return address ? hotelArea(address).label : '位置未確認'; };
  const unconfirmed = rated.filter(hotel => checks.get(hotel.name)?.status === 'unknown');
  const picks: Pick[] = [
    ...verified.map(({ hotel, room, nightly, bedsUnconfirmed }) => ({ hotel, nightly, note: `${bedsUnconfirmed ? '❔' : '✅'} ${roomType(room.name)}${bedsUnconfirmed ? BEDS_UNCONFIRMED : ''}`, sites: sites.get(hotel.name) ?? [], photos: photos.get(hotel.name), area: areaOf(hotel.name) })),
    ...unconfirmed.filter(hotel => { const address = addresses.get(hotel.name); return !address || inTown(stay.city, hotelArea(address).postcode); })
      .map(hotel => ({ hotel, nightly: hotel.nightly, note: '❔ 房型未確認', sites: sites.get(hotel.name) ?? [], photos: photos.get(hotel.name), area: areaOf(hotel.name) })),
  // Cheapest first, confirmed or not: everything listed has passed the bars (own bathroom, no bunk, rating,
  // non-smoking, in town), so the order is the value order (the user's aim, 2026-10-19).
  ].sort((a, b) => shownNightly(a) - shownNightly(b)).slice(0, limit);
  return { picks, shared };
}

/** Marks a listed room whose stated beds sleep fewer than the party (src/party-rules.ts bedsTooFew). */
const BEDS_UNCONFIRMED = '（床數未確認）';

/** Two lines per hotel: name, class, rating and room; then the money. */
function printPicks(picks: readonly Pick[], stay: Stay, nights: number, flightPrice: number, currency: string, country: string, start = 1) {
  // The user's format (2026-10-18): name, photos, rating and room type, the nightly price at the one
  // recommended site with its link, then the totals. No cancellation terms, labels or room price gaps.
  // Cheapest first as shown, whatever order a cached list was saved in.
  [...picks].sort((a, b) => shownNightly(a) - shownNightly(b)).forEach(({ hotel, nightly: listNightly, note, sites, photos, area }, index) => {
    // One price: what the linked site charges, but never under the pick's own price, which may be
    // for a bigger room than the site's cheapest offer (a room for two rather than a single); the
    // link then goes to a site asking about that price.
    const nightly = Math.max(recommendedSite(sites)?.nightly ?? 0, listNightly);
    const site = siteForPrice(sites, nightly);
    const cost = partyCost(flightPrice, nightly, nights);
    const url = buildHotelNameUrl({ name: hotel.name, city: stay.city, checkIn: stay.outboundDate, checkOut: stay.returnDate, currency, country });
    // Google states "N% less than usual" for the list price; show it as money per night.
    const saving = hotel.belowUsualPct ? Math.round(hotel.nightly / (1 - hotel.belowUsualPct / 100) - hotel.nightly) : 0;
    const album = photos ? listPhoto(photos, note.replace(/^[✅❔] /, '').replace(BEDS_UNCONFIRMED, '')) : [];
    beginSegment(album, true);
    listHotel(start + index, hotel.name, sites, nights, currency, hotel.nightly, photos, url, nightly);
    // Flush-left short lines: a caption under a photo is narrow on a phone and indents read as ragged.
    say(`${start + index}. [${hotel.name}](${url})`);
    // "私人房" only means the site named no type; it says nothing, so leave it out.
    const type = note.replace(/^[✅❔] (?!房型未確認)/, '');
    for (const line of phoneLines([hotel.rating !== null ? `⭐${hotel.rating.toFixed(1)}` : null, type === '私人房' ? null : type,
      hotel.publicBath ? '♨️大浴場' : null, area ? `📍${area}` : null])) say(line);
    // Say so when no bathroom photo could be found anywhere, rather than leave one photo unexplained (2026-10-19).
    if (album.length === 1) say('🚿 找不到浴室照片');
    // Every hotel gets a link to book or compare: Google's own page when it named no site for it.
    const booking = site ? `\n在 [${siteName(site.site)}](${site.url ?? url}) 訂` : `\n在 [Google](${url}) 看價格`;
    say(`每晚含稅 ${money(nightly, currency)}${booking}${saving > 0 ? `\n🏷 比平常省 ${money(saving, currency)}／晚` : ''}`);
    if (travelers === 1) say(`${nights}晚 ${money(cost.stay, currency)}・含機票 ${money(cost.total, currency)}`);
    else {
      // Party totals: rooms for everyone plus every person's fare, then each person's share.
      say(`${nights}晚${roomsNeeded > 1 ? ` ${roomsNeeded}間` : ''} ${money(cost.stay, currency)}`);
      say(`${travelers}人含機票 ${money(cost.total, currency)}\n每人 ${money(cost.each, currency)}`);
    }
  });
  beginSegment(); // what follows the hotels is plain text again
}

/**
 * Hotels for the city and dates of a flight option from the last report
 * (code like B1). With --nearby, cheaper areas a short train ride away instead.
 */
/**
 * A trip the user can ask hotels for: an option of the region report ("B4") or the trip they
 * searched by date ("T1", from last-trip.json).
 */
type TripOption = {
  watch: { origin: string; currency: string; country: string; id: string | null; lastScanAt: Date | null };
  row: { city: string; outboundDate: string; returnDate: string; price: number; stops: number; currency: string };
  label: string;
};
type SavedTrip = TripOption['row'] & { origin: string; country: string };
const lastTripFile = new JsonFile<SavedTrip | null>(dataFile('last-trip.json'), () => null, objectOf<SavedTrip>('行程'));
const DATED_TRIP_CODE = 'T1';

async function resolveOption(code: string | undefined): Promise<TripOption> {
  const wanted = (code ?? '').trim().toUpperCase();
  if (wanted === DATED_TRIP_CODE) {
    const trip = lastTripFile.read();
    if (!trip) throw new Error('還沒有指定日期的行程，請先說日期和城市查一次機票。');
    return { watch: { origin: trip.origin, currency: trip.currency, country: trip.country, id: null, lastScanAt: null },
      row: trip, label: `${nightsOf(trip)}晚` };
  }
  const match = /^([A-Z])(\d{1,2})$/.exec(wanted);
  if (!match) throw new Error('用法：hotels-for <選項代碼>，例如 hotels-for B1 或 T1');
  const entry = (await activeWatches()).find(({ letter }) => letter === match[1]);
  if (!entry?.watch.lastScanAt) throw new Error(`找不到選項 ${code}，請先重新查一次機票清單。`);
  const { watch } = entry;
  const rows = observations(watch.id, { scannedAt: entry.watch.lastScanAt });
  const row = rows[Number(match[2]) - 1];
  if (!row) throw new Error(`找不到選項 ${code}，請先重新查一次機票清單。`);
  return { watch, row, label: (TRIP_LENGTHS[watch.tripLength as TripLength]?.label ?? watch.tripLength).replace('約', '') };
}

/** Hotels the user's preferences allow (class and rating); unrated hotels pass the rating test. */
const fitsPrefs = (pick: Pick, prefs: Prefs) =>
  (pick.hotel.stars ?? 0) >= prefs.minStars && (pick.hotel.rating === null || pick.hotel.rating >= prefs.minRating);

async function hotelsFor(code: string | undefined, nearby = false) {
  const option = await resolveOption(code);
  const watchButton = `🔔 ${code!.toUpperCase()} 降價通知我`;
  out.keyboard = !nearby && nearbyAreas(option.row.city).length
    ? [[`${code!.toUpperCase()} ${NEARBY_BUTTON}`], [watchButton], [FLIGHT_LIST_BUTTON]] : [[watchButton], [FLIGHT_LIST_BUTTON]];
  await listHotels(option, nearby);
}

/** The flight plan and hotel list of a trip, printed; what is saved for the buttons is up to the caller. */
async function listHotels({ watch, row }: TripOption, nearby = false) {
  const stay: Stay = { city: row.city, outboundDate: row.outboundDate, returnDate: row.returnDate };
  const nights = nightsOf(row);
  lastStayFor = { ...stay, currency: row.currency, country: watch.country, origin: watch.origin };

  if (nearby) {
    const areas = nearbyAreas(row.city);
    const perFare = await planFare(watch, row, stay, readUserPrefs());
    say(`🏨 *${cityLabel(row.city)} 周邊住宿*\n${dateRange(row.outboundDate, row.returnDate)}・${nights} 晚\n✈️ 建議的機票每人 ${money(perFare, row.currency)}`);
    if (!areas.length) {
      say(`\n${cityLabel(row.city)} 沒有設定周邊住宿區域，市區的選擇已經是主要選項。`);
      return;
    }
    // Staying farther out is for paying less: only hotels cheaper than the cheapest in town (2026-10-19:
    // Kamata at NT$4,517 a night beside a town list from NT$3,379).
    const prefs = readUserPrefs();
    const town = await cachedStays(watch, stay, ROOM_CHECK_CANDIDATES, MAX_HOTELS).catch(error => { if (isBlocked(error)) throw error; return null; });
    const townPicks = (town?.picks ?? []).filter(pick => fitsPrefs(pick, prefs));
    const townCheapest = townPicks.length ? Math.min(...townPicks.map(shownNightly)) : null;
    if (townCheapest !== null) say(`市區最便宜每晚 ${money(townCheapest, row.currency)}`);
    let number = 1;
    for (const area of areas) {
      const areaStay: Stay = { ...stay, city: area.area };
      const result = await cachedStays(watch, areaStay, NEARBY_CANDIDATES, NEARBY_PER_AREA);
      say(`\n📍 *${area.label}*\n${area.ride.replace('到', '🚃 到')}`);
      const cheaper = (result?.picks ?? []).filter(pick => townCheapest === null || shownNightly(pick) < townCheapest);
      if (!result?.picks.length) { say('這次沒查到確認過的私人房。'); continue; }
      if (!cheaper.length) { say('沒有比市區便宜的'); continue; }
      printPicks(await ownBathrooms(cheaper, areaStay, row.currency), areaStay, nights, perFare, row.currency, watch.country, number);
      number += cheaper.length;
    }
    return;
  }

  say(`🏨 *${cityLabel(row.city)} 住宿*\n${dateRange(row.outboundDate, row.returnDate)}・${nights} 晚`);
  const prefs = readUserPrefs();
  // The flights are worked out beside the hotel checks, so they add no wait.
  const [result, perFare] = await Promise.all([
    cachedStays(watch, stay, ROOM_CHECK_CANDIDATES, MAX_HOTELS),
    printFlightPlan(watch, row, stay, prefs),
  ]);
  const allLink = buildHotelNameUrl({ city: row.city, checkIn: row.outboundDate, checkOut: row.returnDate, currency: row.currency, country: watch.country });
  if (!result) {
    say('\n這次沒查到這段日期的住宿價格，\n可以稍後再試。\n（Google Hotels 可能沒套用日期\n或暫時擋住了）');
    return;
  }
  if (!result.picks.length) {
    say(`\n這次沒找到確認過的私人房。[在 Google Hotels 看全部住宿](${allLink})`);
    return;
  }
  // The user's preferences decide what is shown; when nothing fits, show what there is rather than nothing.
  const fitting = result.picks.filter(pick => fitsPrefs(pick, prefs));
  const shown = fitting.length ? fitting : result.picks;
  // Counted over the hotels that are listed: Booking's room check can still leave one out, and the
  // count once named five where four were shown (2026-11-21, BAKURO's bunk twin).
  const listed = await ownBathrooms(shown, stay, row.currency);
  const cheaper = listed.filter(pick => pick.hotel.belowUsualPct);
  say(cheaper.length ? `\n🏷 住宿：${cheaper.length} 間比平常便宜` : '\n🏷 住宿：都是一般價');
  if (!fitting.length) say(`沒有評分 ${prefs.minRating} 以上的，先列全部`);
  say('');
  printPicks(listed, stay, nights, perFare, row.currency, watch.country);
  // Only the link: the user found the explanations under the list noise (2026-10-18). A public bath
  // keeps one short line, since its photos read as a shared bathroom.
  say(`\n[看全部住宿](${allLink})`);
  if (shown.some(pick => pick.hotel.publicBath)) say('♨️ 大浴場＝公共澡堂，房內有衛浴');
}

/** The user's preferences (data/settings.json "prefs"), over the defaults. */
function readUserPrefs(): Prefs {
  return readPrefs(settingsFile.read().prefs);
}

const cachedBooking = (origin: string, stay: Stay, currency: string, country: string, prefs: Prefs) =>
  cached('booking', [origin, stay, currency, country, prefs, 'per-city times, short trips'], () => bookFlight(origin, stay, currency, country, {
    outbound: flights => chooseFlight(flights, prefs, stay.city, nightsOf(stay)),
    inbound: (flights, airline) => chooseReturn(flights, airline, prefs, stay.city, nightsOf(stay)) }));

/**
 * A hotel with a public bath files the bath and its shared washrooms under Google's "Bathroom"
 * (APA Asakusa, 2026-10-19), and the user wants the room's own bathroom: it comes from the hotel's
 * Booking page (cached; one page per such hotel), else the hotel gets no bathroom photo.
 */
/**
 * A hotel's Booking rooms: from its profile when the background jobs already fetched it (photos and
 * room types do not change with the dates), else fetched now and kept. Google's Booking offer when it
 * shows one, else Booking searched by the hotel's name (kept only when the page names that hotel).
 */
async function bookingRoomsFor(pick: Pick, stay: Stay, currency: string): Promise<BookingRoom[]> {
  const profile = freshProfile(pick.hotel.name);
  if (profile) return profile.rooms;
  const booking = pick.sites.find(site => /^booking/i.test(site.site) && site.url);
  const fromGoogle = booking?.url ? bookingUrlFromGoogle(booking.url) : null;
  const bookingUrl = fromGoogle ?? bookingSearchUrl(pick.hotel.name, stay, Math.min(travelers, 2), currency);
  const found = await fetchBookingRooms(bookingUrl, fromGoogle ? undefined : pick.hotel.name).catch(() => null);
  saveProfile({ name: pick.hotel.name, hotelUrl: found?.hotelUrl ?? null, rooms: found?.rooms ?? [], fetchedAt: new Date().toISOString() });
  return found?.rooms ?? [];
}

// Hotels profiled per background run: few, so Booking does not start challenging the visits.
const PROFILES_PER_RUN = 4;

/**
 * Profile the hotels of the lists already prepared (the prefetched stays) that have no fresh profile
 * yet, a few per run, so their photos and room checks are ready when a list is shown (the user's
 * request, 2026-10-20). Returns how many were fetched.
 */
async function profileHotels(limit = PROFILES_PER_RUN): Promise<number> {
  let fetched = 0;
  let files: string[] = [];
  try { files = readdirSync(CACHE_DIR).filter(file => file.startsWith('stays-')); } catch { return 0; }
  for (const file of files) {
    let saved: { key: [Stay, string, ...unknown[]]; value: { picks: Pick[] } | null };
    try { saved = JSON.parse(readFileSync(join(CACHE_DIR, file), 'utf8')); } catch { continue; }
    const [stay, currency] = saved.key;
    for (const pick of saved.value?.picks ?? []) {
      if (fetched >= limit) return fetched;
      if (freshProfile(pick.hotel.name)) continue;
      await bookingRoomsFor(pick, stay, currency);
      fetched++;
      await pause();
    }
  }
  return fetched;
}

async function ownBathrooms(picks: readonly Pick[], stay: Stay, currency: string): Promise<Pick[]> {
  const { nonSmoking } = readUserPrefs();
  const result: Pick[] = [];
  for (let pick of picks) {
    const rooms = await bookingRoomsFor(pick, stay, currency);
    // Google's room names hide bunk beds and shared bathrooms (Minn Kamata's "Quadruple Room" is bunks,
    // DOYANEN BAKURO's twin a bunk with a shared bathroom): a hotel whose rooms for the party on Booking
    // are all smoking, bunks or shared-bathroom is poor value for two and is left out.
    const { forParty, keep } = bookingRoomsForParty(rooms, { travelers, nonSmoking });
    if (!keep) continue;
    // Google's panel names no room for some hotels (APA's own rates): Booking's cheapest room that
    // suits the party says what it is.
    const cheapest = [...forParty].sort((a, b) => (a.stayTotal ?? Infinity) - (b.stayTotal ?? Infinity))[0];
    if (pick.note.startsWith('❔') && cheapest) pick = { ...pick, note: `✅ ${roomType(cheapest.name)}` };
    // The room's own bathroom from Booking where Google's "Bathroom" photos cannot be used (a public
    // bath files its baths there) or are missing (2026-10-19: an APA went out with no bathroom photo).
    const listed = pick.note.replace(/^[✅❔] /, '').replace(BEDS_UNCONFIRMED, '');
    const ownBathroom = [...forParty.filter(room => roomType(room.name) === listed), ...forParty].map(room => room.bathroomPhoto).find(Boolean);
    const photos = pick.photos ?? { rooms: [], bedroom: [], bathroom: [] };
    if (pick.hotel.publicBath) pick = { ...pick, photos: { ...photos, bathroom: ownBathroom ? [ownBathroom] : [] } };
    else if (!photos.bathroom.length && ownBathroom) pick = { ...pick, photos: { ...photos, bathroom: [ownBathroom] } };
    // No room photo at all from Google: Booking's view of the room.
    const roomView = forParty.flatMap(room => room.photos).find(url => url !== ownBathroom);
    if (pick.photos && !pick.photos.rooms.length && !pick.photos.bedroom.length && roomView) pick = { ...pick, photos: { ...pick.photos, bedroom: [roomView] } };
    result.push(pick);
  }
  return result;
}

/**
 * The flights to book for an option (the user's request, 2026-10-19: a lazier flow): the pair that
 * fits their preferences and the hotel's check-in and check-out, cheapest with bag fees counted,
 * each linked where it is booked. The user picks the hotel from the list that follows (they found
 * one recommended hotel too poor, 2026-10-19). Returns each person's fare, bag estimate included.
 */
/**
 * Each person's fare for the flights printFlightPlan suggests, bag estimate included, without printing
 * them: the nearby list totals with it too (2026-10-19: it used the cheapest fare of the day instead).
 */
async function planFare(watch: TripOption['watch'], row: TripOption['row'], stay: Stay, prefs: Prefs): Promise<number> {
  const booking = await cachedBooking(watch.origin, stay, row.currency, watch.country, prefs).catch(error => { if (isBlocked(error)) throw error; return null; });
  const buy = booking ? chooseBookingOption(booking.options, prefs.oneTicketPremium) : null;
  if (!booking || !buy) return row.price;
  return buy.chosen.price + (prefs.checkedBag && (booking.outbound.lowCost || booking.inbound.lowCost) ? prefs.bagFeeEstimate : 0);
}

async function printFlightPlan(watch: TripOption['watch'], row: TripOption['row'], stay: Stay, prefs: Prefs): Promise<number> {
  const currency = row.currency;
  let noFit = false;
  const [verdictPage, booking] = await Promise.all([
    cachedFlights(watch.origin, stay, currency, watch.country).catch(() => null),
    cachedBooking(watch.origin, stay, currency, watch.country, prefs).catch(error => {
      if (isBlocked(error)) throw error;
      if (error instanceof NoFittingFlight) noFit = true;
      else console.error('[deals] flight booking failed:', String(error).slice(0, 300));
      return null;
    }),
  ]);
  const market = verdictPage?.market ?? null;
  const amount = market?.versusUsual?.replace(/^(\S+) cheaper$/, '比平常便宜 $1').replace(/^(\S+) more expensive$/, '比平常貴 $1');
  if (market) say(`📊 機票 Google ${MARKET_TEXT[market.level].replace(/（.*）/, '')}${amount ? `\n${amount}` : ''}`);
  const buy = booking ? chooseBookingOption(booking.options, prefs.oneTicketPremium) : null;
  if (!booking || !buy) {
    say(`✈️ [${cityLabel(row.city)} 來回每人 ${money(row.price, currency)}](${buildCardSearchUrl(watch.origin, stay, currency)})\n${noPlanReason(noFit)}`);
    return row.price;
  }
  // Times are local (Japan is an hour ahead of Taiwan), so the time in the air goes with them
  // (14:25→18:30 read as four hours to the user, 2026-10-19), on the next line: a four-character
  // airline name (泰國獅航) leaves no room on this one.
  const legs = (flight: FlightOption) => `${airlineName(flight.airline)} ${flight.departs}→${flight.arrives}`;
  // Separate tickets are two bookings: each leg links its airline's own page (2026-10-19, the user
  // got one link for two tickets); otherwise Google's booking page for the whole trip.
  const legLink = (url: string | null | undefined) => url ? withAdults(url, travelers) : booking.url;
  const separate = buy.chosen.separateTickets;
  // When the party reaches the hotel and must leave it, against its usual check-in and check-out:
  // early means dropping the bags first, a later return means checking out on time and leaving later.
  const times = hotelTimes(booking.outbound, booking.inbound, prefs, row.city);
  say(`✈️ [去 ${legs(booking.outbound)}](${separate ? legLink(booking.legLinks?.outbound) : booking.url})`);
  say(`${times.late ? '⚠️ ' : ''}飛 ${shortDuration(booking.outbound.duration)}・約 ${times.arrive} 到飯店`);
  if (times.beforeCheckIn) say(`${prefs.hotelCheckIn} 才能入住・可先寄放行李`);
  say(separate ? `[回 ${legs(booking.inbound)}](${legLink(booking.legLinks?.inbound)})` : `回 ${legs(booking.inbound)}`);
  if (times.afterCheckOut) {
    say(`飛 ${shortDuration(booking.inbound.duration)}・${prefs.hotelCheckOut} 退房`);
    say(`${times.leave} 出發去機場・可寄放行李`);
  } else {
    say(`${times.tooEarly ? '⚠️ ' : ''}飛 ${shortDuration(booking.inbound.duration)}・${times.leave} 就要離開飯店`);
  }
  const bagged = prefs.checkedBag && (booking.outbound.lowCost || booking.inbound.lowCost);
  say(`來回每人 ${money(buy.chosen.price, currency)}${bagged ? `\n＋托運行李約 ${money(prefs.bagFeeEstimate, currency)}（估算）` : ''}`);
  // A short trip may buy more time there: say what it costs over the cheapest fare for those dates.
  const forTime = buy.chosen.price - row.price;
  if (nightsOf(stay) <= SHORT_TRIP_NIGHTS && forTime > 0) say(`早去晚回・比最便宜多 ${money(forTime, currency)}`);
  // How much the chosen fare saves over the other way to buy it; negative when the other is cheaper.
  // 2026-11-22: an absolute difference printed 「分開訂可省 NT$38,423」 for separate tickets that cost NT$38,423 more.
  const saving = buy.other ? buy.other.price - buy.chosen.price : 0;
  if (separate) say(`分開訂兩張票${saving > 0 ? `・省 ${money(saving, currency)}` : ''}`);
  // One ticket is one booking: say the one link covers both legs (only the outbound line is linked).
  else for (const line of phoneLines(['去回同一張票・去程連結一次訂', buy.other?.separateTickets && saving < 0 ? `分開訂可省 ${money(-saving, currency)}` : null])) say(line);
  return buy.chosen.price + (bagged ? prefs.bagFeeEstimate : 0);
}

function regions() {
  for (const watch of listRegionWatches()) {
    const label = TRIP_LENGTHS[watch.tripLength as TripLength]?.label ?? watch.tripLength;
    say(`${watch.active ? '啟用' : '停用'}｜${watch.originLabel} → ${watch.regionLabel}（${watch.region}）｜${label}｜每 ${watch.intervalHours} 小時｜每日排行：${watch.sendDigest ? '開' : '關'}｜最後掃描：${taipei(watch.lastScanAt)}${watch.failureCount ? `｜連續失敗 ${watch.failureCount} 次` : ''}`);
  }
}

function addRegion(region: string | undefined, regionLabel: string | undefined) {
  if (!region || !PLACE.test(region) || !regionLabel || regionLabel.length > 20) throw new Error('用法：add-region <英文名稱> <中文名稱>，例如 add-region "South Korea" 韓國');
  for (const tripLength of LENGTHS) {
    const existing = findWatch('Taipei', region, tripLength);
    if (existing) updateWatch(existing.id, { active: true, regionLabel, nextScanAt: new Date() });
    else createWatch({ origin: 'Taipei', originLabel: '台北', region, regionLabel, tripLength, sendDigest: tripLength === 'week', digestHour: 14 });
  }
  say(`已開始掃描 台北 → ${regionLabel}（${region}）的週末、一週、兩週行程。15 分鐘內會進行第一次掃描。`);
}

function removeRegion(region: string | undefined) {
  if (!region) throw new Error('用法：remove-region <英文名稱>');
  const count = setRegionActive(region, false);
  say(count ? `已停止掃描 ${region}（共 ${count} 個行程長度），歷史價格保留。` : `找不到地區 ${region}。`);
}

function scanNow() {
  say(`已排入 ${scheduleAllNow()} 個掃描，15 分鐘內會執行。`);
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** "大阪" or "Osaka" → "Osaka": Google's English name, from the Chinese names the bot shows. */
function cityName(input: string): string | null {
  const trimmed = input.trim();
  const english = Object.entries(CITY_NAMES).find(([, label]) => label === trimmed)?.[0];
  return english ?? (PLACE.test(trimmed) ? trimmed : null);
}

/**
 * Flights for dates the user chose (Google Flights: verdict, airlines, times), saved as option
 * T1 so its hotels can be asked for like a report option.
 */
/** A city and dates the user named, checked, with the scanner's origin, currency and market. */
async function datedStay(command: string, cityArg: string | undefined, outboundDate: string | undefined, returnDate: string | undefined) {
  const city = cityName(cityArg ?? '');
  if (!city || !outboundDate || !returnDate || !ISO_DATE.test(outboundDate) || !ISO_DATE.test(returnDate) || returnDate <= outboundDate) {
    throw new Error(`用法：${command} <城市> <出發日 YYYY-MM-DD> <回程日 YYYY-MM-DD>，例如 ${command} 大阪 2026-12-20 2026-12-25`);
  }
  if (Date.parse(`${outboundDate}T00:00:00Z`) < Date.now() - 86_400_000) throw new Error('出發日已經過了。');
  const watch = (await activeWatches())[0]?.watch;
  return { watch, origin: watch?.origin ?? 'Taipei', currency: watch?.currency ?? 'TWD', country: watch?.country ?? 'TW',
    stay: { city, outboundDate, returnDate } satisfies Stay };
}

const sayNoFlights = () => say('\n這次沒讀到航班，可以稍後再試。\n（Google 可能暫時擋住，\n或這條航線沒有航班）');

async function tripSearch(cityArg: string | undefined, outboundDate: string | undefined, returnDate: string | undefined) {
  const { watch, origin, currency, country, stay } = await datedStay('trip', cityArg, outboundDate, returnDate);
  const { city } = stay;
  const found = await cachedFlights(origin, stay, currency, country);
  const nights = nightsOf(stay);
  say(`✈️ *${watch?.originLabel ?? '台北'} → ${cityLabel(city)}*\n${dateRange(stay.outboundDate, stay.returnDate)}・${nights} 晚・來回`);
  if (!found?.flights.length) {
    sayNoFlights();
    out.keyboard = [[FLIGHT_LIST_BUTTON]];
    return;
  }
  // The same plan as for a scanned option (2026-10-19: this list had no pair, no booking links, no
  // return times, and led with a flight landing after the last train): the pair that fits, each leg
  // linked, with when the party reaches and leaves the hotel.
  const cheapest = found.flights[0]!;
  await printFlightPlan({ origin, currency, country, id: null, lastScanAt: null },
    { ...stay, price: cheapest.price, stops: cheapest.stops, currency }, stay, readUserPrefs());
  lastTripFile.write({ origin, currency, country, ...stay, price: cheapest.price, stops: cheapest.stops });
  say(`\n[在 Google Flights 看全部航班](${buildCardSearchUrl(origin, stay, currency)})`);
  say(travelers > 1 ? `每人來回價（${travelers} 人同行）・行李另計` : '1 人來回價・行李另計');
  out.keyboard = [[`🏨 ${DATED_TRIP_CODE} 這趟的住宿`], [`🔔 ${DATED_TRIP_CODE} 降價通知我`], [FLIGHT_LIST_BUTTON]];
}

/**
 * Flights and hotels for a city and dates, for another agent's Kanban card. 2026-11-21: the commander sent
 * Trip Agent a card for Osaka, and since `trip` and `hotels-for` replace the user's Telegram list, it
 * answered from airline home pages instead ("Peach about NT$8,000" where Google had NT$13,310). This
 * reads the same prices and saves nothing: no T1, no hotel list, no buttons, and the party size is for
 * this lookup only.
 */
// 2026-11-21: with no booking page read there is no bag line, and a card told the user NT$32,392 「在 4 萬
// 預算內」 with no word that two checked bags (about NT$3,600) were not in it. But a full-service pair
// (China Airlines both ways) has no bag line either, its fare holding the bags: three cases, not two.
const PLAN_TOTAL_NOTE = '「含機票」總價＝住宿＋每人機票\n有「＋托運行李」那行：廉航行李費已加進去，不要再加\n有去回航班、沒有那行：一般航空，票價已含托運行李\n寫「這次沒讀到訂票頁」或「沒有符合時段的航班」：\n總價沒含托運行李，回報要寫「未含托運行李」';
// 2026-11-21: plan read no flights for Fukuoka in March (Google blocked it once; a rerun had Tigerair at
// NT$12,281), and the card said 「遠期尚未開賣」「預計 2026 年 3 月查價」 with two hotels nobody looked up.
const PLAN_NO_FLIGHTS_NOTE = '這次讀不到＝被擋或暫時沒讀到，不是還沒開賣\n（Google 可訂約 11 個月內的航班）\n住宿也還沒查：機票、住宿都寫「未確認」\n不要自己挑飯店或猜原因，過幾分鐘再跑一次 plan';
// 2026-11-21: three runs of a card's plan met Google's limit, some 90 s each, and Trip then spent minutes
// reading this program to find out why. Said plainly, with when to try again.
const planLimitedNote = (minutes: number) => `Google 暫時限流：每個查詢都回「Oops, something went wrong」\n不是被封鎖、不是還沒開賣，也不是程式壞掉，不用查原因\n這張卡不要重跑：約 ${minutes} 分鐘後 Google 才恢復（背景查詢已讓開）\n現在回報就寫：機票、住宿都「未確認」`;

async function planForCard(cityArg: string | undefined, outboundDate: string | undefined, returnDate: string | undefined, people: string | undefined) {
  const { watch, origin, currency, country, stay } = await datedStay('plan', cityArg, outboundDate, returnDate);
  if (people !== undefined) {
    const count = Number(people);
    if (!Number.isInteger(count) || count < 1 || count > MAX_TRAVELERS) throw new Error(`人數請用 1 到 ${MAX_TRAVELERS}`);
    travelers = count;
    roomsNeeded = roomsNeededFor(count);
  }
  out.keyboard = [];
  const heading = `✈️ *${watch?.originLabel ?? '台北'} → ${cityLabel(stay.city)}*\n${dateRange(stay.outboundDate, stay.returnDate)}・${nightsOf(stay)} 晚`;
  const found = await cachedFlights(origin, stay, currency, country).catch(error => {
    if (!isLimited(error)) throw error;
    noteBlocked(error);
    return 'limited' as const;
  });
  if (found === 'limited') {
    say(`${heading}\n\n${planLimitedNote(LIMIT_COOLDOWN_MINUTES)}`);
    return;
  }
  if (!found?.flights.length) {
    say(heading);
    sayNoFlights();
    say(PLAN_NO_FLIGHTS_NOTE);
    return;
  }
  const cheapest = found.flights[0]!;
  await listHotels({ watch: { origin, currency, country, id: null, lastScanAt: null },
    row: { ...stay, price: cheapest.price, stops: cheapest.stops, currency }, label: `${nightsOf(stay)}晚` });
  say(`\n[在 Google Flights 看全部航班](${buildCardSearchUrl(origin, stay, currency)})`);
  say(`${travelers} 人・${roomsNeeded} 間房・查詢時間 ${new Date().toISOString().slice(0, 16).replace('T', ' ')} UTC`);
  // 2026-11-21: a card read "＋托運行李約" above "2人含機票 NT$35,825" as not yet counted and added the
  // bags again (NT$39,425). The total already holds them.
  say(PLAN_TOTAL_NOTE);
}

/**
 * The last hotel list, so "rooms <n>" knows which hotel and stay the user means. Each hotel keeps
 * its Booking.com page (from Google's price link) when Booking sold it.
 */
type SavedHotels = { savedAt?: string; stay: { nights: number; currency: string } | null;
  trip: (Stay & { currency: string; country: string; origin: string }) | null; hotels: ListedHotel[] };
const lastHotelsFile = new JsonFile<SavedHotels | null>(dataFile('last-hotels.json'), () => null, value => {
  const saved = objectOf<SavedHotels>('住宿清單')(value);
  return typeof saved === 'string' || Array.isArray(saved.hotels) ? saved : '缺少 hotels 清單';
});
type ListedHotel = { n: number; name: string; bookingUrl: string | null; tripHotelId?: string | null; nightly?: number;
  /** Google's room photos and its page: the room types of a hotel neither Booking nor Trip.com sells. */
  googlePhotos?: HotelPhotos; googleUrl?: string;
  /** Its bookingUrl is a Booking search by name, whose page must name the hotel. */
  bookingByName?: boolean;
  /** The nightly price the list showed (a site's price for the room); `nightly` is Google's list price, which the watch compares. */
  shown?: number };
const listedHotels: ListedHotel[] = [];
let listedStay: { nights: number; currency: string } | null = null;
/** The stay of the last hotel list, so a hotel from it can be watched for price drops. */
let lastStayFor: (Stay & { currency: string; country: string; origin: string }) | null = null;

function listHotel(n: number, name: string, sites: readonly SiteOffer[], nights: number, currency: string, nightly?: number, googlePhotos?: HotelPhotos, googleUrl?: string, shown?: number) {
  const booking = sites.find(site => /^booking/i.test(site.site) && site.url);
  const trip = sites.find(site => /^trip\.com/i.test(site.site) && site.url);
  // Booking searched by name when Google shows no Booking offer: its room photos and checks for every hotel.
  const fromGoogle = booking?.url ? bookingUrlFromGoogle(booking.url) : null;
  const byName = !fromGoogle && lastStayFor ? bookingSearchUrl(name, lastStayFor, Math.min(travelers, 2), currency) : null;
  listedHotels.push({ n, name, bookingUrl: fromGoogle ?? byName, bookingByName: Boolean(byName),
    tripHotelId: trip?.url ? tripHotelIdFromGoogle(trip.url) : null, nightly, shown, googlePhotos, googleUrl });
  listedStay = { nights, currency };
}

const ROOM_PHOTO_BUTTON = (n: number) => `📷 第${n}間房型`;

/** Remember the list and offer a room-photo button for each hotel with room types to show (Booking, Trip.com or Google). */
function saveListedHotels() {
  if (!listedHotels.length) return;
  lastHotelsFile.write({ savedAt: new Date().toISOString(), stay: listedStay, trip: lastStayFor, hotels: listedHotels });
  // Every hotel with anything to show gets its button (2026-10-19: one with only Google's bedroom photos had none).
  const showable = (hotel: ListedHotel) => hotel.bookingUrl || hotel.tripHotelId || hotel.googlePhotos?.rooms.length
    || hotel.googlePhotos?.bedroom.length || hotel.googlePhotos?.bathroom.length;
  const buttons = listedHotels.filter(showable).map(hotel => ROOM_PHOTO_BUTTON(hotel.n));
  const rows: string[][] = [];
  for (let start = 0; start < buttons.length; start += BUTTONS_PER_ROW) rows.push(buttons.slice(start, start + BUTTONS_PER_ROW));
  out.keyboard = [...rows, ...out.keyboard];
}

/** One message per room type of a hotel from the last list: its photos, size, beds, bathroom, guests and price. */
async function roomsFor(which: string | undefined) {
  const n = Number(/\d+/.exec(which ?? '')?.[0]);
  const saved = lastHotelsFile.read();
  if (!saved) throw new Error('還沒有住宿清單，請先查一次住宿。');
  const hotel = saved.hotels.find(entry => entry.n === n);
  if (!hotel) throw new Error(`上一份住宿清單沒有第 ${which} 間。`);
  out.keyboard = [[`🔔 第${n}間降價通知`], [FLIGHT_LIST_BUTTON]];
  const currency = saved.stay?.currency ?? 'TWD';
  const nights = saved.stay?.nights ?? 1;
  // Booking first (its photos say which room they show); Trip.com when Booking does not sell the hotel,
  // shows no rooms, or has no room photos at all. One hotel page per site, only on request.
  let source = 'Booking';
  let result = hotel.bookingUrl ? await fetchBookingRooms(hotel.bookingUrl, hotel.bookingByName ? hotel.name : undefined) : null;
  const usable = (found: typeof result) => Boolean(found?.rooms.some(room => room.photos.length));
  const state = readJobsState();
  const tripPaused = state.tripPausedUntil !== null && Date.parse(state.tripPausedUntil) > Date.now();
  if (!usable(result) && hotel.tripHotelId && saved.trip && !tripPaused) {
    const fromTrip = await fetchTripRooms(hotel.tripHotelId, saved.trip, currency).catch(error => {
      // Trip.com asks for a sign-in after a few automated visits: leave it alone for a while.
      if (error instanceof Error && error.name === 'TripSignInWallError') {
        writeJobsState({ ...readJobsState(), tripPausedUntil: new Date(Date.now() + TRIP_PAUSE_HOURS * 3_600_000).toISOString() });
      }
      return null;
    });
    if (fromTrip?.rooms.length && (usable(fromTrip) || !result?.rooms.length)) { result = fromTrip; source = 'Trip.com'; }
  }
  // A hotel neither site sells (the user found no button for two of six, 2026-10-19): the room
  // types Google's price panel showed with their photos, and the hotel's own bathroom photo.
  if (!result?.rooms.length && hotel.googlePhotos?.rooms.length) {
    const bathroom = hotel.googlePhotos.bathroom[0];
    // Each site lists the same rooms in its own words: one entry per room type, at most four.
    const byType = new Map<string, BookingRoom>();
    for (const { name, url } of hotel.googlePhotos.rooms) {
      const row = parseGoogleRoomRow(name);
      if (!row.name || byType.has(roomKey(row.name))) continue;
      byType.set(roomKey(row.name), { name: row.name, guests: row.guests, stayTotal: row.stayTotal ?? (row.nightly ? row.nightly * nights : null),
        freeCancelUntil: null, sizeM2: null, beds: row.beds, bathroom: null, photos: [url, ...(bathroom ? [bathroom] : [])], bunkInPhotos: false, resale: false });
    }
    result = { hotelUrl: hotel.googleUrl ?? '', rooms: [...byType.values()].slice(0, 4) };
    source = 'Google';
  }
  // Nothing names its room types: show the hotel's room and bathroom photos Google has, and say so.
  if (!result?.rooms.length && (hotel.googlePhotos?.bedroom.length || hotel.googlePhotos?.bathroom.length)) {
    beginSegment([...hotel.googlePhotos.bedroom, ...hotel.googlePhotos.bathroom].slice(0, 4), true);
    say(`📷 *${n}. ${hotel.name}*\n房間和浴室照片（Google）\n訂房網站沒有列出房型`);
    beginSegment();
    say(`[在 Google 看房型和價格](${hotel.googleUrl ?? ''})`);
    return;
  }
  if (!result?.rooms.length) {
    const link = hotel.bookingUrl ? `\n[直接在 Booking 看這間](${hotel.bookingUrl})` : '';
    say(`${n}. ${hotel.name}：Booking 和 Trip.com 這次都沒有顯示房型（可能暫時擋住或已客滿）。${link}`);
    return;
  }
  // Beds in shared dorms are no room for the user; say the hotel sells them, list only private rooms.
  // So are smoking rooms when the user minds the smell, and rooms too small for the party: src/party-rules.ts.
  const { shown: privateRooms, dormBeds, smoking } = sortRooms(result.rooms, party());
  say(`📷 *${n}. ${hotel.name}*\n各房型照片和資料（${source}）${dormBeds.length ? '\n⚠️ 這間也賣宿舍床位，已排除' : ''}`);
  if (!privateRooms.length) { say(smoking.length ? '只有吸菸房，沒有禁菸房。' : '只有宿舍床位，沒有私人房。'); return; }
  let previousPhotos = '';
  for (const room of privateRooms) {
    const samePhotos = room.photos.length > 0 && room.photos.join() === previousPhotos;
    beginSegment(samePhotos ? [] : room.photos, true);
    previousPhotos = room.photos.join();
    const facts = [room.sizeM2 ? `${room.sizeM2} m²` : null, ...room.beds.map(bedText),
      room.bathroom === 'private' ? '私人衛浴' : room.bathroom === 'shared' ? '共用衛浴' : null,
      isSmokingRoom(room.name) ? '🚬 吸菸房' : isNonSmokingRoom(room.name) ? '🚭 禁菸' : null].filter(Boolean);
    // Who it is for, bunks, then its name or beds: src/party-rules.ts.
    const type = roomTypeLabel(room);
    say(`🛏 *${type}*\n${room.name}`);
    // Short lines: a room with four kinds of beds did not fit a phone on one line (2026-10-19).
    for (const line of phoneLines(facts)) say(line);
    // The hotel's own photos outrank its bed list: say so when they disagree.
    if (room.bunkInPhotos && !room.beds.some(bed => /bunk/i.test(bed))) say('⚠️ 照片是上下鋪\n（一張床在上層，要爬梯子）');
    // The beds named sleep fewer than the party ("Basic Twin Room", "1 single bed"): say so, do not guess.
    if (room.guests === null && bedsTooFew(room.beds, travelers)) say(`⚠️ 床數和房型對不上\n${travelers} 人住得下嗎：未確認`);
    if (room.guests !== null) say(`可住 ${room.guests} 人${room.guests < travelers ? `　⚠️ 你們 ${travelers} 人住不下` : ''}`);
    if (room.stayTotal !== null) say(`${nights} 晚 ${money(room.stayTotal, currency)}（每晚 ${money(Math.round(room.stayTotal / nights), currency)}）`);
    if (samePhotos) say('（照片和上一個房型相同）');
    else if (room.resale) say('（第三方轉售方案，\nBooking 沒有這個方案的房間照）');
    else if (!room.photos.length) say(`（${source} 沒有這個房型的房間照）`);
  }
  beginSegment();
  say(`[在 ${source} 看這間、訂房](${result.hotelUrl})\n${privateRooms.some(room => room.stayTotal) ? `價格是 ${source} 含稅總價` : '房價看清單上的每晚價'}`);
}

// Watched trips: the saved list and the drop rules are in src/trip-watches.ts.
const tripWatches = watchesFile();
const readWatches = () => tripWatches.read();
const writeWatches = (watches: TripWatch[]) => tripWatches.write(watches);
const watchTitle = (watch: TripWatch) => `${cityLabel(watch.city)} ${dateRange(watch.outboundDate, watch.returnDate)}`;

function findOrAddWatch(stay: WatchStay): { watches: TripWatch[]; watch: TripWatch; added: boolean } {
  const watches = readWatches();
  return { watches, ...addWatch(watches, stay) };
}

/** Watch an option's flight ("B4", "T1"): the alert baseline is today's cheapest fare for its dates. */
async function watchTrip(code: string | undefined) {
  const { watch: source, row } = await resolveOption(code);
  const { watches, watch, added } = findOrAddWatch({ origin: source.origin, currency: row.currency, country: source.country,
    city: row.city, outboundDate: row.outboundDate, returnDate: row.returnDate });
  const found = await cachedFlights(watch.origin, watch, watch.currency, watch.country).catch(() => null);
  watch.flight = found?.flights[0]?.price ?? row.price;
  writeWatches(watches);
  say(`🔔 ${added ? '開始盯' : '已經在盯'}\n${watchTitle(watch)}`);
  // The cheapest fare of those days, any flight: not the suggested pair, whose price differs (2026-10-19).
  say(`這幾天最便宜的機票\n每人 ${money(watch.flight, watch.currency)}（不限班次）\n便宜 ${money(FLIGHT_DROP, watch.currency)} 以上就通知你`);
  say('想一起盯飯店：\n點房型照片下的 🔔 按鈕');
  out.keyboard = [[FLIGHT_LIST_BUTTON]];
}

/** Watch hotel n of the last hotel list for that list's stay. */
function watchHotel(which: string | undefined) {
  const n = Number(/\d+/.exec(which ?? '')?.[0]);
  const saved = lastHotelsFile.read();
  if (!saved) throw new Error('還沒有住宿清單，請先查一次住宿。');
  const hotel = saved.hotels.find(entry => entry.n === n);
  if (!hotel || !saved.trip || !hotel.nightly) throw new Error(`上一份住宿清單沒有第 ${which} 間。`);
  const { watches, watch } = findOrAddWatch(saved.trip);
  watch.hotel = { name: hotel.name, nightly: hotel.nightly, shown: hotel.shown };
  writeWatches(watches);
  say(`🔔 開始盯\n${watchTitle(watch)}\n${hotel.name}\n每晚現在 ${money(hotel.shown ?? hotel.nightly, watch.currency)}\n便宜 ${money(HOTEL_DROP, watch.currency)} 以上就通知你`);
  out.keyboard = [[FLIGHT_LIST_BUTTON]];
}

function listWatches() {
  const watches = readWatches();
  if (!watches.length) { say('目前沒有在盯的行程。'); return; }
  for (const watch of watches) {
    say(`${watch.id}. ${watchTitle(watch)}`);
    if (watch.flight !== null) say(`最便宜機票每人 ${money(watch.flight, watch.currency)}`);
    if (watch.hotel) say(`${watch.hotel.name}\n每晚 ${money(watch.hotel.shown ?? watch.hotel.nightly, watch.currency)}`);
  }
}

function unwatch(which: string | undefined) {
  const id = Number(/\d+/.exec(which ?? '')?.[0]);
  const watches = readWatches();
  const kept = watches.filter(watch => watch.id !== id);
  if (kept.length === watches.length) throw new Error(`沒有第 ${which} 個在盯的行程。`);
  writeWatches(kept);
  say(`已停止盯第 ${id} 個行程。`);
}

/** Check every watched trip; returns the alert messages to send. Trips that have started are dropped. */
async function checkWatches(): Promise<string[]> {
  const watches = upcoming(readWatches(), new Date().toISOString().slice(0, 10));
  const alerts: string[] = [];
  for (const watch of watches) {
    const found = await searchFlights(watch.origin, watch, watch.currency, watch.country).catch(error => { if (isBlocked(error)) throw error; return null; });
    const fare = found?.flights[0];
    if (fare) {
      const { alert, bar } = checkFlight(watch.flight, fare.price);
      if (alert) {
        alerts.push(`🔔 機票降價：${watchTitle(watch)}\n${money(alert.from, watch.currency)} → ${money(alert.to, watch.currency)}`
          + `（便宜 ${money(alert.from - alert.to, watch.currency)}）\n${flightLines([fare], watch.currency, 1)[0]!.replace(/^1\. /, '')}`
          + `\n[看航班、訂票](${buildCardSearchUrl(watch.origin, watch, watch.currency)})`);
      }
      watch.flight = bar;
    }
    if (watch.hotel) {
      const watched = watch.hotel;
      const hotels = await captureHotelList(watch, watch).catch(error => { if (isBlocked(error)) throw error; return null; });
      const now = hotels?.find(hotel => hotel.name === watched.name)?.nightly;
      if (now !== undefined) {
        const { alert, hotel } = checkHotel(watched, now);
        if (alert) {
          const url = buildHotelNameUrl({ name: watched.name, city: watch.city, checkIn: watch.outboundDate, checkOut: watch.returnDate, currency: watch.currency, country: watch.country });
          alerts.push(`🔔 飯店降價：${watchTitle(watch)}\n[${watched.name}](${url})\n每晚約 ${money(alert.from, watch.currency)} → ${money(alert.to, watch.currency)}`
            + `\n便宜 ${money(alert.drop, watch.currency)}`);
        }
        watch.hotel = hotel;
      }
    }
    watch.lastCheckedAt = new Date().toISOString();
    writeWatches(watches); // keep progress if a later trip meets a block
    await pause();
  }
  writeWatches(watches);
  return alerts;
}

/**
 * Background work, run every few hours by the "Trip Bot Jobs" scheduled task:
 *  - after each new region scan, fetch the hotels and flights of the two cheapest options of
 *    every trip length into the cache, so tapping an option answers in seconds;
 *  - check watched trips for price drops;
 *  - once a day, a self-check of the hotel pipeline that reports to Telegram when results look
 *    broken (the user should not be the one to find a Google change).
 */
// Kept low so background work never gets the home network blocked by Google (the region scanner and
// the user's own lookups use the same address): two prefetches per trip length per scan (their
// hotels then get photos ready, which the user asked for on 2026-10-20), a pause between page-heavy
// steps, watches every six hours, and a three-hour pause after any block.
const PREFETCH_PER_LENGTH = 2;
const PAUSE_BETWEEN_MS = 60_000;
const WATCH_CHECK_HOURS = 6;
const BLOCK_COOLDOWN_HOURS = 3;
// Google's "Oops, something went wrong" on every search (GoogleLimitedError): a shorter pause, but one,
// since the background's own requests kept the limit on (2026-11-21).
const LIMIT_COOLDOWN_MINUTES = 20;
// The user's own lookups go first: after one, the background leaves Google alone for this long
// (2026-11-21: a card's plan met the limit right after the 14:16 background run).
const USER_FIRST_MINUTES = 10;
type JobsState = { prefetched: Record<string, string>; lastSelfCheck: string | null; lastWatchCheck: string | null; blockedUntil: string | null;
  tripPausedUntil: string | null; userLookupAt: string | null };
const TRIP_PAUSE_HOURS = 6;

const isLimited = (error: unknown) => error instanceof Error && error.name === 'GoogleLimitedError';
const isBlocked = (error: unknown) => error instanceof Error && (error.name === 'GoogleBlockedError' || isLimited(error));
const cooldownMs = (error: unknown) => isLimited(error) ? LIMIT_COOLDOWN_MINUTES * 60_000 : BLOCK_COOLDOWN_HOURS * 3_600_000;
const pause = () => new Promise(resolve => setTimeout(resolve, PAUSE_BETWEEN_MS));

const emptyJobsState = (): JobsState => ({ prefetched: {}, lastSelfCheck: null, lastWatchCheck: null, blockedUntil: null, tripPausedUntil: null, userLookupAt: null });
const jobsStateFile = new JsonFile<JobsState>(dataFile('jobs-state.json'), emptyJobsState, value => {
  const saved = objectOf<Partial<JobsState>>('背景工作狀態')(value);
  return typeof saved === 'string' ? saved : { ...emptyJobsState(), ...saved };
});
const readJobsState = () => jobsStateFile.read();
const writeJobsState = (state: JobsState) => jobsStateFile.write(state);

/** The pause after a block: the longer of the one running and this one's. */
const blockedUntilAfter = (state: JobsState, error: unknown) =>
  new Date(Math.max(state.blockedUntil ? Date.parse(state.blockedUntil) : 0, Date.now() + cooldownMs(error))).toISOString();

/** Any command that meets a Google block pauses the background jobs; returns whether it was new. */
function noteBlocked(error: unknown): boolean {
  const state = readJobsState();
  const wasBlocked = state.blockedUntil !== null && Date.parse(state.blockedUntil) > Date.now();
  state.blockedUntil = blockedUntilAfter(state, error);
  writeJobsState(state);
  return !wasBlocked;
}

/** A lookup the user (or a card) is waiting for: the background steps aside for a while. */
const noteUserLookup = () => writeJobsState({ ...readJobsState(), userLookupAt: new Date().toISOString() });

/**
 * Saves what a background run changed onto the newest state. 2026-11-22: runJobs read the state at
 * its start and wrote all of it back at its end, so the pause a card's plan noted meanwhile (and its
 * user lookup) vanished, and the next run went straight back to Google.
 */
function saveJobsRun(run: JobsState) {
  const fresh = readJobsState();
  const later = (a: string | null, b: string | null) => (a && b ? (a > b ? a : b) : a ?? b);
  writeJobsState({ ...fresh, prefetched: { ...fresh.prefetched, ...run.prefetched }, lastWatchCheck: run.lastWatchCheck,
    lastSelfCheck: run.lastSelfCheck, blockedUntil: later(fresh.blockedUntil, run.blockedUntil) });
}

/** Why the background should leave Google alone now, or null. */
function backgroundPause(state: { blockedUntil: string | null; userLookupAt: string | null }, now = Date.now()): string | null {
  if (state.blockedUntil && Date.parse(state.blockedUntil) > now) return `paused until ${state.blockedUntil} after a Google block`;
  if (state.userLookupAt && now - Date.parse(state.userLookupAt) < USER_FIRST_MINUTES * 60_000) return `waiting: a user lookup ran at ${state.userLookupAt}`;
  return null;
}

async function runJobs() {
  const state = readJobsState();
  const paused = backgroundPause(state);
  if (paused) {
    say(`jobs: ${paused}`);
    return;
  }
  const problems: string[] = [];
  let alerts: string[] = [];
  // Checked again before each Google step: a lookup someone waits for (or a block) that starts while
  // this runs gets Google to itself. 2026-11-22: a card's plan met the limit while this kept prefetching.
  const stepAside = () => {
    const reason = backgroundPause(readJobsState());
    if (reason) say(`jobs: stopped, ${reason}`);
    return reason !== null;
  };
  try {
    watches: for (const { watch, letter } of await activeWatches()) {
      if (!watch.lastScanAt || state.prefetched[watch.id] === watch.lastScanAt.toISOString()) continue;
      for (let index = 1; index <= PREFETCH_PER_LENGTH; index++) {
        if (stepAside()) break watches;
        try {
          const { row } = await resolveOption(`${letter}${index}`);
          const stay: Stay = { city: row.city, outboundDate: row.outboundDate, returnDate: row.returnDate };
          // Not forced fresh: a stay still cached from the last six hours costs no requests.
          await cachedFlights(watch.origin, stay, row.currency, watch.country);
          await cachedStays(watch, stay, ROOM_CHECK_CANDIDATES, MAX_HOTELS);
          await pause();
        } catch (error) {
          if (isBlocked(error)) throw error;
          problems.push(`預先查 ${letter}${index} 失敗：${error instanceof Error ? error.message.slice(0, 80) : String(error)}`);
        }
      }
      state.prefetched[watch.id] = watch.lastScanAt.toISOString();
    }
    // Photos and room checks for the hotels of those lists, a few each run, ready before they are shown.
    const profiled = stepAside() ? 0 : await profileHotels();
    if (profiled) say(`jobs: profiled ${profiled} hotels`);
    if ((!state.lastWatchCheck || Date.now() - Date.parse(state.lastWatchCheck) >= WATCH_CHECK_HOURS * 3_600_000) && !stepAside()) {
      alerts = await checkWatches();
      state.lastWatchCheck = new Date().toISOString();
    }
    const today = new Date().toISOString().slice(0, 10);
    if (state.lastSelfCheck !== today && !stepAside()) {
      problems.push(...await selfCheck());
      state.lastSelfCheck = today;
    }
  } catch (error) {
    if (!isBlocked(error)) throw error;
    state.blockedUntil = blockedUntilAfter(state, error);
    // A limit lifts within its pause and needs no word to the user; a block is worth one.
    if (!isLimited(error)) problems.push(`Google 暫時擋住自動查詢，背景工作暫停 ${BLOCK_COOLDOWN_HOURS} 小時（你主動查詢照常，但可能也會失敗）`);
  }
  saveJobsRun(state);
  for (const alert of alerts) await sendToTelegram([{ text: alert, photos: [] }], [[FLIGHT_LIST_BUTTON]]);
  if (problems.length) await sendToTelegram([{ text: `⚠️ trip bot 自我檢查發現問題\n${problems.map(problem => `• ${problem}`).join('\n')}`, photos: [] }], []);
  say(`jobs: ${alerts.length} alerts, ${problems.length} problems`);
}

/**
 * Today's hotel pipeline on the cheapest one-week option, judged on what the user sees: enough
 * hotels, photos, booking links and flights. Returns the problems found (empty when healthy).
 */
async function selfCheck(): Promise<string[]> {
  const entry = (await activeWatches()).find(({ watch }) => watch.tripLength === 'week') ?? (await activeWatches())[0];
  if (!entry?.watch.lastScanAt) return ['沒有可用的掃描結果'];
  const { watch, letter } = entry;
  const { row } = await resolveOption(`${letter}1`);
  const stay: Stay = { city: row.city, outboundDate: row.outboundDate, returnDate: row.returnDate };
  const problems: string[] = [];
  const result = await cachedStays(watch, stay, ROOM_CHECK_CANDIDATES, MAX_HOTELS).catch(error => { if (isBlocked(error)) throw error; return null; });
  const flights = await cachedFlights(watch.origin, stay, row.currency, watch.country).catch(error => { if (isBlocked(error)) throw error; return null; });
  const picks = result?.picks ?? [];
  if (picks.length < 3) problems.push(`住宿只找到 ${picks.length} 間（${cityLabel(row.city)}）`);
  const withPhotos = picks.filter(pick => pick.photos && (pick.photos.rooms.length || pick.photos.bedroom.length)).length;
  if (picks.length && withPhotos < picks.length / 2) problems.push(`只有 ${withPhotos}/${picks.length} 間有照片`);
  const withSites = picks.filter(pick => recommendedSite(pick.sites)).length;
  if (picks.length && withSites < picks.length / 2) problems.push(`只有 ${withSites}/${picks.length} 間有訂房網站`);
  if (!flights?.flights.length) problems.push('讀不到航班清單');
  else if (!flights.market) problems.push('讀不到 Google 的機票價格判斷');
  // The data check over every cached list: report what would mislead (wrong room for the party,
  // implausible or inconsistent prices, missing photos), not notes such as a hotel without a site link.
  const NOTES = /沒有任何訂房網站價格|共用衛浴/;
  problems.push(...auditCache().filter(problem => !NOTES.test(problem)).slice(0, 8));
  // Messages that went out breaking one of the user's rules since the last check.
  problems.push(...recentRuleProblems(Date.now() - 86_400_000).slice(0, 8));
  // And today's data through every command, as the user would tap through it.
  const checked = spawnSync(process.execPath, [...process.execArgv, process.argv[1]!, 'check'], { encoding: 'utf8', timeout: 1_800_000 });
  problems.push(...(checked.stdout ?? '').split('\n').filter(line => line.startsWith('✗')).map(line => line.slice(2)).slice(0, 8));
  return problems;
}

const LOGS_DIR = new URL('../logs/', import.meta.url);
const RULES_LOG = new URL('../logs/rules.log', import.meta.url);

/** Rule problems logged since a time, as "command：problem", each once. */
function recentRuleProblems(since: number): string[] {
  let log = '';
  try { log = readFileSync(RULES_LOG, 'utf8'); } catch { return []; }
  const found = log.split('\n').map(line => line.split('\t')).filter(([at]) => at && Date.parse(at) >= since)
    .map(([, ran, problem]) => `${ran}：${problem}`);
  return [...new Set(found)];
}

/**
 * Render every command the user taps through, as Telegram would get it, and check each against the
 * user's rules (src/rules.ts): the flight list, each trip length's first option with its hotels, the
 * room photos of the first listed hotels, and a dated trip. Run after every change, before saying it
 * is done. Uses the caches, so it opens few pages.
 */
async function checkAll() {
  const entries = [...new Set((await activeWatches()).filter(({ watch }) => watch.lastScanAt).map(({ letter }) => letter))];
  const runs: string[][] = [['report', 'all', '--limit', '5'], ...entries.map(letter => ['hotels-for', `${letter}1`])];
  if (entries.length) runs.push(['hotels-for', `${entries[0]}1`], ['rooms', 'each']);
  // The nearby list too (2026-10-19: it went unchecked and a hotel lost its button): the first option
  // whose city has nearby areas, then the room photos of each hotel it lists.
  const options = entries.flatMap(letter => [1, 2, 3, 4, 5].map(number => `${letter}${number}`));
  for (const code of options) {
    const option = await resolveOption(code).catch(() => null);
    if (!option || !nearbyAreas(option.row.city).length) continue;
    runs.push(['hotels-for', code, '--nearby'], ['rooms', 'each']);
    break;
  }
  // A dated trip as the user would ask for one: Osaka, five nights, two months out.
  const day = (offset: number) => new Date(Date.now() + offset * 86_400_000).toISOString().slice(0, 10);
  runs.push(['trip', '大阪', day(60), day(65)], ['watches'], ['prefs']);
  let failures = 0;
  for (let at = 0; at < runs.length; at++) {
    const run = runs[at]!;
    // "rooms each": the room photos of every hotel the list just before it saved, however many.
    if (run[0] === 'rooms' && run[1] === 'each') {
      let listed: ListedHotel[] = [];
      listed = lastHotelsFile.read()?.hotels ?? [];
      runs.splice(at + 1, 0, ...listed.map(hotel => ['rooms', String(hotel.n)]));
      continue;
    }
    const result = spawnSync(process.execPath, [...process.execArgv, process.argv[1]!, ...run, '--check'], { encoding: 'utf8', timeout: 600_000 });
    const verdict = (result.stdout ?? '').trim().split('\n').filter(line => /^[✓✗]/.test(line));
    const error = (result.stderr ?? '').split('\n').filter(line => line && !/ExperimentalWarning|--trace-warnings|^\[deals\]/.test(line)).join(' ').slice(0, 200);
    if (verdict.some(line => line.startsWith('✗')) || (!verdict.length && error)) failures++;
    console.log(`${run.join(' ')}\n${verdict.length ? verdict.join('\n') : error ? `✗ ${error}` : '（沒有輸出）'}`);
  }
  console.log(failures ? `\n${failures} 個指令有問題` : '\n全部符合規則');
  if (failures) process.exitCode = 1;
}

/** A queued scan alert (promotion, daily list, scan health) as one Telegram message. */
const alertText = (message: AlertMessage) =>
  `*${message.title}*\n\n${message.body}${message.url ? `\n\n[看航班、訂票](${message.url})` : ''}`;
const sendAlert = (message: AlertMessage) => sendToTelegram([{ text: alertText(message), photos: [] }], []);
const OBSERVATION_DAYS = 60; // the usual price looks back 30 days

/**
 * What the "Trip Bot" scheduled task runs every 15 minutes: the region scans that are due, the
 * daily lists and alerts they produce, then the background jobs. During a Google block only
 * already-queued alerts go out.
 */
async function tick() {
  const paused = backgroundPause(readJobsState());
  if (paused) {
    await deliverDealAlerts(sendAlert);
    say(`tick: ${paused}`);
    return;
  }
  try {
    await runDueDealWatches(sendAlert);
  } catch (error) {
    if (!isBlocked(error)) throw error;
    if (noteBlocked(error) && !isLimited(error)) await sendAlert({ title: '⚠️ Google 暫時擋住自動查詢', body: `掃描和背景查詢暫停 ${BLOCK_COOLDOWN_HOURS} 小時，之後自動恢復。`, url: '' });
    return;
  }
  pruneObservations(new Date(Date.now() - OBSERVATION_DAYS * 86_400_000));
  await runJobs();
}

/**
 * Every result cached in the last six hours, checked against the mistakes the user once found
 * (src/audit.ts). Returns one line per problem, prefixed with the stay it belongs to.
 */
function auditCache(): string[] {
  const problems: string[] = [];
  let files: string[] = [];
  try { files = readdirSync(CACHE_DIR); } catch { return problems; }
  // Only hotel lists and flight pages: the cache also keeps booking pages and Booking room lists.
  for (const file of files.filter(name => /^(stays|flights)-/.test(name))) {
    let saved: { savedAt: string; key: unknown[]; value: unknown };
    try { saved = JSON.parse(readFileSync(join(CACHE_DIR, file), 'utf8')); } catch { continue; }
    if (Date.now() - Date.parse(saved.savedAt) > CACHE_HOURS * 3_600_000) continue;
    const stay = (file.startsWith('stays-') ? saved.key[0] : saved.key[1]) as Stay | undefined;
    const label = stay ? `${cityLabel(stay.city)} ${md(stay.outboundDate)}–${md(stay.returnDate)}` : file;
    const found = file.startsWith('stays-')
      ? auditStays((saved.value as { picks: AuditPick[] } | null)?.picks ?? [], travelers)
      : auditFlights(saved.value as Parameters<typeof auditFlights>[0]);
    problems.push(...found.map(problem => `${label}｜${problem}`));
  }
  return problems;
}

function audit() {
  const problems = auditCache();
  say(problems.length ? problems.join('\n') : '快取裡的清單都沒有發現問題。');
}

const PREF_NAMES: Record<keyof Prefs, string> = {
  departAfter: '最早出發', departBefore: '最晚出發', checkedBag: '托運行李', bagFeeEstimate: '廉航行李費估算（每人來回）',
  minStars: '最低星級', minRating: '最低評分', maxPerPerson: '每人預算上限', oneTicketPremium: '一張票最多可以貴多少',
  nonSmoking: '只要禁菸房', hotelCheckIn: '飯店入住時間', hotelCheckOut: '飯店退房時間',
};

/** Show the preferences, or set one: `prefs minRating 4.2`, `prefs checkedBag false`, `prefs maxPerPerson none`. */
function setPrefs(key: string | undefined, value: string | undefined) {
  const settings = settingsFile.read();
  if (key) {
    if (!(key in PREF_NAMES)) throw new Error(`可以設定：${Object.keys(PREF_NAMES).join('、')}`);
    const parsed = value === 'none' ? null : value === 'true' ? true : value === 'false' ? false : /^\d+(\.\d+)?$/.test(value ?? '') ? Number(value) : value;
    const next = readPrefs({ ...(settings.prefs as object | undefined), [key]: parsed });
    if (JSON.stringify(next[key as keyof Prefs]) !== JSON.stringify(parsed)) throw new Error(`${PREF_NAMES[key as keyof Prefs]} 的值不合理：${value}`);
    settings.prefs = { ...(settings.prefs as object | undefined), [key]: next[key as keyof Prefs] }; // only what the user set: defaults stay changeable
    settingsFile.write(settings);
  }
  const prefs = readPrefs(settings.prefs);
  say(['你的偏好', `出發時間 ${prefs.departAfter}–${prefs.departBefore}`, `托運行李：${prefs.checkedBag ? `要（廉航加 ${money(prefs.bagFeeEstimate, 'TWD')}）` : '不用'}`,
    `飯店 ${prefs.minStars} 星以上、評分 ${prefs.minRating} 以上`, `每人預算：${prefs.maxPerPerson === null ? '不限' : money(prefs.maxPerPerson, 'TWD')}`,
    prefs.oneTicketPremium > 0 ? `一張票貴 ${money(prefs.oneTicketPremium, 'TWD')} 內也選一張票` : '分開訂比較便宜就分開訂', `飯店 ${prefs.hotelCheckIn} 入住・${prefs.hotelCheckOut} 退房`, prefs.nonSmoking ? '只要禁菸房' : '吸菸房也可以'].join('\n'));
}

function setTravelers(value: string | undefined) {
  if (value === undefined) { say(`目前設定 ${travelers} 人同行（${roomsNeeded} 間房）。`); return; }
  const people = Number(value);
  if (!Number.isInteger(people) || people < 1 || people > MAX_TRAVELERS) throw new Error(`人數請用 1 到 ${MAX_TRAVELERS}`);
  // Keep the other settings (preferences) in the same file.
  const settings = settingsFile.read();
  settingsFile.write({ ...settings, travelers: people });
  say(`已設定 ${people} 人同行：機票 × ${people}，住宿每間 2 人、共 ${Math.ceil(people / 2)} 間。之後查住宿都會算整團總價和每人平均。`);
}

const [command = 'report', ...args] = process.argv.slice(2);
// The commands someone is waiting on that read Google: the background steps aside after each.
const USER_LOOKUPS = new Set(['trip', 'plan', 'hotels-for', 'rooms', 'watch', 'check']);
const limitIndex = args.indexOf('--limit');
const limit = limitIndex >= 0 ? Math.min(40, Math.max(1, Number(args[limitIndex + 1]) || 10)) : 10;
// The arguments without flags: `prefs --telegram` read the flag as a preference to set (2026-10-19).
const words = args.filter(arg => !arg.startsWith('--'));
if (args.includes('--telegram')) {
  console.error('Direct Telegram delivery is disabled. Use Hermes delivery instead.');
  process.exit(2);
}
const checkOnly = args.includes('--check'); // render as for Telegram, check the user's rules, send nothing
try {
  travelers = readTravelers();
  roomsNeeded = roomsNeededFor(travelers);
  if (USER_LOOKUPS.has(command) && !checkOnly) noteUserLookup();
  if (command === 'report') await report(args.find(arg => !arg.startsWith('--') && arg !== args[limitIndex + 1]) ?? 'all', limit);
  else if (command === 'regions') regions();
  else if (command === 'add-region') addRegion(args[0], args[1]);
  else if (command === 'remove-region') removeRegion(args[0]);
  else if (command === 'scan-now') scanNow();
  else if (command === 'travelers') setTravelers(words[0]);
  else if (command === 'prefs') setPrefs(words[0], words[1]);
  else if (command === 'hotels-for') {
    await hotelsFor(args[0], args.includes('--nearby')); // --all is the default now
    saveListedHotels();
  } else if (command === 'rooms') await roomsFor(words[0]);
  else if (command === 'trip') await tripSearch(args[0], args[1], args[2]);
  else if (command === 'plan') await planForCard(words[0], words[1], words[2], words[3]);
  else if (command === 'watch') await watchTrip(args[0]);
  else if (command === 'watch-hotel') watchHotel(words[0]);
  else if (command === 'watches') listWatches();
  else if (command === 'unwatch') unwatch(words[0]);
  else if (command === 'jobs') await runJobs();
  else if (command === 'tick') await tick();
  else if (command === 'audit') audit();
  else if (command === 'check') await checkAll();
  else if (command === 'profile') say(`profiled ${await profileHotels(Number(words[0]) || PROFILES_PER_RUN)} hotels`);
  else throw new Error('指令：report、trip、plan、hotels-for、rooms、watch、watch-hotel、watches、unwatch、travelers、regions、add-region、remove-region、scan-now、jobs');
  if (checkOnly && out.said) {
    const prefs = readUserPrefs();
    const problems = checkOutput(out.segments, out.keyboard, { travelers, nonSmoking: prefs.nonSmoking, checkIn: prefs.hotelCheckIn, checkOut: prefs.hotelCheckOut });
    console.log(problems.length ? problems.map(problem => `✗ ${problem}`).join('\n') : '✓');
    if (problems.length) process.exitCode = 1;
  }

} catch (error) {
  if (isBlocked(error)) {
    // The user's own lookup met the block too: pause the background jobs so they do not prolong it.
    noteBlocked(error);
    console.error(isLimited(error)
      ? `Google 暫時限流（每個查詢都回 Oops），約 ${LIMIT_COOLDOWN_MINUTES} 分鐘後再試。背景的預先查詢已暫停 ${LIMIT_COOLDOWN_MINUTES} 分鐘。`
      : `Google 暫時擋住了自動查詢，通常幾小時內會恢復，請稍後再試。背景的預先查詢已暫停 ${BLOCK_COOLDOWN_HOURS} 小時。`);
  } else console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
