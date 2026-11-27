import { createHash } from 'node:crypto';
import type { Page } from 'playwright';
import { launchBrowser, createStealthContext } from '../google/browser';
import { dismissGoogleConsent } from '../google/consent';
import { parseGoogleMarketLevel, parseMarketDifference, type MarketLevel } from '../google/market-insight';
import { addAlert, addObservations, claimWatch, dueAlerts, listWatches, observations, recentAlerts, transaction, updateAlert, updateWatch,
  type AlertMessage, type DealWatch } from '../store';
import { buildExploreUrl, cardsWithoutPrice, isTripLength, pageTripLength, parseExplorePage, TRIP_LENGTHS, type ExploreQuote } from './explore-page';
import { cheapestCities, DEFAULT_DEAL_RULES, findDeals, type CityHistory } from './detect';
import { flightPageState, parseBookingOptions, parseFlightLabel, parseFlightLabels, type BookingOption, type FlightOption } from './flight-list';
import { bestSiteRating, buildHotelNameUrl, buildHotelSearchUrl, classifyRooms, dateInputLabel, isWrongCountryHotel, parseBelowUsual, parseHotelList, parseLabelRatings, parseRoomOffers, parseSiteOffers, attachSiteLinks, largePhotoUrl, roomPhotos, summarizeHotels,
  type HotelListQuote, type HotelSummary, type RoomCheck, type SiteOffer, type HotelPhotos } from './hotel-page';
import { dealMessage, digestMessage, scanFailingMessage, scanRecoveredMessage, type DealContext } from './message';

const DIGEST_SIZE = 10;
const MIN_CARDS = 5;
const STEADY_POLLS = 3; // 4.5 seconds without a new priced card
const PRICE_PAGES = 2; // pages of the same list in lowest-price order, merged in
const LIST_PAGES = 3; // pages of Google Hotels results read per list, some 15 hotels each
const MAX_HOTEL_LOOKUPS = 3; // hotel pages per scan, for the deepest promotions
const FAILURE_ALERT_AFTER = 3; // consecutive failed scans (retried hourly) before telling the user
const DIGEST_MAX_SCAN_AGE_MS = 12 * 3_600_000; // older scans mean the scanner is failing; skip the list
const MAX_DELIVERY_ATTEMPTS = 8;
const TAIPEI_OFFSET_MS = 8 * 60 * 60 * 1000;

/** "Loading results" stays in the page as an accessibility label, so wait for parseable cards instead. */
async function waitForCards(page: Page, ready: (text: string) => boolean): Promise<string> {
  let previous = -1;
  let steady = 0;
  for (let waited = 0; waited < 30_000; waited += 1500) {
    await page.waitForTimeout(1500);
    const text = await page.evaluate(() => document.body.innerText);
    if (GOOGLE_ERROR.test(text)) return text; // no cards will come; do not wait out the 30 seconds
    if (!ready(text)) continue;
    // Cards get their prices one by one: read once every card has one, or the count has stopped
    // growing for a few seconds (a card Google never prices), not at the first few.
    const count = parseExplorePage(text, new Date()).length;
    steady = count === previous ? steady + 1 : 0;
    previous = count;
    if (count >= MIN_CARDS && (cardsWithoutPrice(text, new Date()) === 0 || steady >= STEADY_POLLS)) break;
  }
  await page.waitForTimeout(1500);
  return page.evaluate(() => document.body.innerText);
}

/** Google's own error page in place of results ("Oops, something went wrong."). */
const GOOGLE_ERROR = /Oops, something went wrong/;
const EXPLORE_RELOADS = 2;

type ExploreTarget = Pick<DealWatch, 'origin' | 'region' | 'currency' | 'country' | 'tripLength'>;

/**
 * The region's Explore page text, loaded once more when it shows no destination prices. Google
 * sometimes answers the trip-length switch with its error page and a second load works (2026-11-21:
 * Japan two-week scans failed twice in a row on "Oops, something went wrong."; the retry read 24 cities).
 */
export async function captureExplorePage(watch: ExploreTarget, load = captureExploreOnce,
  wait = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))): Promise<string> {
  let last = '';
  const found = await retryIfEmpty(async () => {
    last = await load(watch);
    return parseExplorePage(last, new Date()).length ? last : null;
  }, 8_000, wait);
  return found ?? last;
}

/** Open the region's Explore page, switch to the watch's trip length, and return its visible text. */
async function captureExploreOnce(watch: ExploreTarget): Promise<string> {
  const url = buildExploreUrl(watch);
  const length = isTripLength(watch.tripLength) ? watch.tripLength : 'week';
  const browser = await launchBrowser();
  try {
    const context = await createStealthContext(browser);
    const page = await context.newPage();
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    await dismissGoogleConsent(page);
    await assertNotBlocked(page);
    let text = await waitForCards(page, () => true);
    const shown = pageTripLength(text);
    if (shown !== length && shown !== null) {
      // The flexible-dates picker opens from the current trip-length label.
      await page.getByText(TRIP_LENGTHS[shown].header, { exact: true }).first().click();
      await page.getByText(TRIP_LENGTHS[length].option, { exact: true }).filter({ visible: true }).first().click();
      await page.getByText('Done', { exact: true }).filter({ visible: true }).last().click();
      text = await waitForCards(page, current => pageTripLength(current) === length);
    }
    // The page's address keeps the trip length, so a reload asks again for the same list (2026-11-21:
    // reloading after Google's error page read 39 cities where a fresh browser failed again).
    for (let reload = 0; reload < EXPLORE_RELOADS && GOOGLE_ERROR.test(text); reload++) {
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 30_000 });
      text = await waitForCards(page, current => pageTripLength(current) === length);
    }
    await context.close();
    if (pageTripLength(text) !== length) throw new Error(`The Explore page did not switch to the ${length} trip length`);
    return text;
  } finally {
    await browser.close().catch(() => undefined);
  }
}

function context(watch: DealWatch): DealContext {
  const length = isTripLength(watch.tripLength) ? watch.tripLength : 'week';
  return { originLabel: watch.originLabel, origin: watch.origin, regionLabel: watch.regionLabel, currency: watch.currency,
    tripLabel: TRIP_LENGTHS[length].label };
}

/** Google Hotels list for one city and stay; null when the page did not apply the dates or showed no hotels. */
export async function lookupHotels(watch: Pick<DealWatch, 'currency' | 'country'>, quote: ExploreQuote): Promise<HotelSummary | null> {
  const hotels = await captureHotelList(watch, quote);
  return hotels ? summarizeHotels(hotels, quote.outboundDate, quote.returnDate) : null;
}

/**
 * Every hotel card Google Hotels lists for a city and stay, or null when the
 * page did not apply the requested dates (prices would be for other nights).
 */
/**
 * Google lists hotels by its own relevance, so cheap ones can be missing from the pages read
 * (2026-11-21: Enka Nanba 1, NT$1,130 and 51% below usual, was on none of three relevance lists).
 * 'price' asks Google for lowest price first; the bars (class, rating) are applied afterwards.
 */
export type HotelOrder = 'price' | 'relevance';

/** Whether Google's sort control says the list is in lowest-price order. */
export const sortedByLowestPrice = (label: string | null) => /^Lowest price\b.*\bSelected\b/i.test(label ?? '');

/** Switch the list to "Lowest price"; the list as it was when the switch does not take. */
async function sortByLowestPrice(page: Page, shown: HotelListQuote[]): Promise<HotelListQuote[]> {
  // The control is "Sort by, Not selected" and becomes "Lowest price, Sort by, Selected" (2026-11-21).
  const sortButton = page.locator('button[aria-label*="Sort by"]').first();
  try {
    await sortButton.click({ timeout: 5_000 });
    await page.locator('label', { hasText: 'Lowest price' }).filter({ visible: true }).first().click({ timeout: 5_000 });
    const before = shown.map(hotel => hotel.name).join('|');
    // Read once the new list stops changing: read at its first change, the cheapest cards were still
    // without prices and three hotels under NT$1,000 were left out (2026-11-21).
    let last = '';
    for (let waited = 0; waited < 15_000; waited += 1500) {
      await page.waitForTimeout(1500);
      const sorted = await sortButton.getAttribute('aria-label', { timeout: 2_000 }).catch(() => null);
      const hotels = parseHotelList(await page.evaluate(() => document.body.innerText));
      const names = hotels.map(hotel => `${hotel.name}:${hotel.nightly}`).join('|');
      const settled = names === last;
      last = names;
      if (sortedByLowestPrice(sorted) && hotels.length >= MIN_CARDS && hotels.map(hotel => hotel.name).join('|') !== before && settled) {
        await page.keyboard.press('Escape').catch(() => undefined);
        return hotels;
      }
    }
  } catch { /* the sort control moved or did not open */ }
  await page.keyboard.press('Escape').catch(() => undefined);
  console.error('[deals] hotel list: could not sort by lowest price; using Google order');
  return shown;
}

/** Whether the page's check-in/check-out inputs show the requested stay; Google silently falls back to default dates. */
async function stayApplied(page: Page, stay: Pick<ExploreQuote, 'outboundDate' | 'returnDate'>): Promise<boolean> {
  const dates = await page.evaluate(() => [...document.querySelectorAll('input')]
    .filter(input => /check-?(in|out)/i.test(input.getAttribute('aria-label') ?? '')).map(input => input.value));
  return dates.some(value => value.includes(dateInputLabel(stay.outboundDate)))
    && dates.some(value => value.includes(dateInputLabel(stay.returnDate)));
}

export async function captureHotelList(watch: Pick<DealWatch, 'currency' | 'country'>,
  quote: Pick<ExploreQuote, 'city' | 'outboundDate' | 'returnDate'>, order: HotelOrder = 'price'): Promise<HotelListQuote[] | null> {
  const url = buildHotelSearchUrl({ city: quote.city, checkIn: quote.outboundDate, checkOut: quote.returnDate, currency: watch.currency, country: watch.country });
  const browser = await launchBrowser();
  try {
    const context = await createStealthContext(browser);
    const page = await context.newPage();
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    await dismissGoogleConsent(page);
    await assertNotBlocked(page);
    let hotels: HotelListQuote[] = [];
    for (let waited = 0; waited < 20_000 && hotels.length < MIN_CARDS; waited += 1500) {
      await page.waitForTimeout(1500);
      hotels = parseHotelList(await page.evaluate(() => document.body.innerText));
    }
    // Each hotel's "View prices for …" link opens its prices panel for the stay, from any page of results.
    const links = new Map<string, string>();
    // Google's "N% less than usual", and the rating in layouts that omit it, live in element labels.
    // Read on every page: read once at the end, only the last page's hotels kept them, and a list whose
    // cheapest hotel was "51% less than usual" said every price was usual (2026-11-20).
    const labels: string[] = [];
    const readLinks = async () => {
      for (const { label, href } of await page.evaluate(() => [...document.querySelectorAll('a[aria-label^="View prices for "]')]
        .map(anchor => ({ label: anchor.getAttribute('aria-label') ?? '', href: (anchor as HTMLAnchorElement).href })))) {
        const name = label.replace(/^View prices for /, '').replace(/\s+/g, ' ').trim();
        if (!links.has(name)) links.set(name, href);
      }
      labels.push(...await page.evaluate(() => [...document.querySelectorAll('[aria-label]')]
        .map(element => element.getAttribute('aria-label') ?? '').filter(label => /less than usual|out of 5 stars/.test(label))));
    };
    await readLinks();
    // More pages: the first holds 15 of some 60 hotels, too few to find the best value among
    // (2026-10-19, the user's aim). The page's own "Next" button (photo carousels have "Next" labels too).
    const nextPage = async (): Promise<boolean> => {
      const next = await page.evaluate(() => {
        const button = [...document.querySelectorAll('button')].find(element => (element as HTMLElement).innerText?.trim() === 'Next') as HTMLElement | undefined;
        button?.click();
        return Boolean(button);
      });
      if (!next) return false;
      const before = new Set(hotels.map(hotel => hotel.name));
      let added: HotelListQuote[] = [];
      for (let waited = 0; waited < 12_000 && !added.length; waited += 1500) {
        await page.waitForTimeout(1500);
        added = parseHotelList(await page.evaluate(() => document.body.innerText)).filter(hotel => !before.has(hotel.name));
      }
      if (!added.length) return false;
      hotels = [...hotels, ...added];
      await readLinks();
      return true;
    };
    for (let more = 1; more < LIST_PAGES; more++) if (!await nextPage()) break;
    // Then the cheapest first, merged in: Google's relevance pages can leave out cheap hotels, and its
    // price order can leave out others (2026-11-21, Osaka: price order lacked a NT$721 hotel; Tokyo:
    // relevance lacked three at NT$1,549-1,588). Two pages, some 36 of the cheapest.
    if (order === 'price' && hotels.length) {
      const shown = parseHotelList(await page.evaluate(() => document.body.innerText));
      const cheapest = await sortByLowestPrice(page, shown);
      if (cheapest !== shown) {
        await readLinks();
        const validCheapest = cheapest.filter(hotel => !isWrongCountryHotel(quote.city, { name: hotel.name, link: links.get(hotel.name) }));
        if (validCheapest.length) {
          const known = new Set(hotels.map(hotel => hotel.name));
          hotels = [...hotels, ...validCheapest.filter(hotel => !known.has(hotel.name))];
          for (let more = 1; more < PRICE_PAGES; more++) if (!await nextPage()) break;
        } else if (cheapest.length) {
          console.error(`[deals] hotel list for ${quote.city}: lowest-price sort fell back to wrong country; skipped`);
        }
      }
    }
    const below = parseBelowUsual(labels);
    const ratings = parseLabelRatings(labels);
    const beforeFilter = hotels.length;
    hotels = hotels
      .map(hotel => ({ ...hotel, rating: hotel.rating ?? ratings.get(hotel.name) ?? null, belowUsualPct: below.get(hotel.name) ?? null, link: links.get(hotel.name) }))
      .filter(hotel => !isWrongCountryHotel(quote.city, { name: hotel.name, link: hotel.link }));
    if (hotels.length < beforeFilter) {
      console.error(`[deals] hotel list for ${quote.city}: dropped ${beforeFilter - hotels.length} wrong-country hotels`);
    }
    // Prices are only meaningful for the requested stay, so confirm the date inputs took it.
    const applied = await stayApplied(page, quote);
    await context.close();
    return applied && (beforeFilter === 0 || hotels.length > 0) ? hotels : null;
  } finally {
    await browser.close().catch(() => undefined);
  }
}

/**
 * Google Flights' own verdict ("Prices are currently low/typical/high") for a
 * route and dates, judged against the route's market-wide price history.
 * Null when the page shows no verdict.
 */
export async function checkFlightMarket(origin: string, stay: Pick<ExploreQuote, 'city' | 'outboundDate' | 'returnDate'>,
  currency: string, country: string): Promise<{ level: MarketLevel; versusUsual: string | null } | null> {
  return (await searchFlights(origin, stay, currency, country)).market;
}

/**
 * Run a lookup once more after a short wait when it comes back empty. A Google page sometimes loads
 * without results and works a moment later (2026-11-12: Osaka 04/22–04/25 read nothing, then 101 flights).
 */
export async function retryIfEmpty<T>(lookup: () => Promise<T | null>, waitMs = 8_000,
  wait = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))): Promise<T | null> {
  const first = await lookup();
  if (first !== null) return first;
  await wait(waitMs);
  return lookup();
}

const FLIGHT_WAIT_MS = 45_000;
const FLIGHT_SETTLE_MS = 5_000; // results without Google's price verdict, waited for this long
const FLIGHT_RELOADS = 3;
const FLIGHT_STUCK_MS = 15_000; // no results and no error for this long: load the page again
const FLIGHT_RELOAD_SETTLE_MS = 3_000; // the error read this long after a Reload is the new page's

/**
 * Wait for a Google Flights results page: done once the results and Google's price verdict are in,
 * or the results have stood a few seconds without a verdict. Google's own "Oops, something went
 * wrong" is answered with its Reload button, and a page stuck loading is loaded again, a few times,
 * instead of waiting out the whole time and opening a new browser (2026-11-21: three runs of 90 s
 * each read nothing; Reload read 22 results in 3 s).
 */
async function waitForFlightResults(page: Page): Promise<void> {
  const start = Date.now();
  let reloads = 0;
  let loadedAt = start;
  let resultsSince: number | null = null;
  while (Date.now() - start < FLIGHT_WAIT_MS) {
    const { text, results } = await page.evaluate(() => ({ text: document.body?.innerText ?? '',
      results: document.querySelectorAll('[aria-label*="round trip total"]').length }));
    const state = flightPageState(text, results);
    if (state === 'ready') return;
    // Google's error again after its own Reload: it is limiting this computer, and more loads only
    // keep the limit on (2026-11-21: every route and date showed it for minutes on end). Its Reload
    // is pressed once; a page only stuck loading is loaded again up to FLIGHT_RELOADS times.
    if (state === 'google-error' && reloads >= 1 && Date.now() - loadedAt >= FLIGHT_RELOAD_SETTLE_MS) throw new GoogleLimitedError();
    if (results) {
      resultsSince ??= Date.now();
      if (Date.now() - resultsSince >= FLIGHT_SETTLE_MS) return;
    } else if (state === 'google-error' ? reloads === 0 : reloads < FLIGHT_RELOADS && Date.now() - loadedAt >= FLIGHT_STUCK_MS) {
      reloads++;
      loadedAt = Date.now();
      const reload = page.getByRole('button', { name: 'Reload' });
      if (state === 'google-error' && await reload.count()) await reload.first().click({ timeout: 5_000 }).catch(() => undefined);
      else await page.reload({ waitUntil: 'domcontentloaded', timeout: 30_000 }).catch(() => undefined);
      await assertNotBlocked(page);
    }
    await page.waitForTimeout(1_000);
  }
}

/**
 * Google Flights for a route and dates: its verdict on the price ("Prices are currently low")
 * and the flight options with airline, times and stops, from one results page.
 */
export async function searchFlights(origin: string, stay: Pick<ExploreQuote, 'city' | 'outboundDate' | 'returnDate'>,
  currency: string, country: string): Promise<{ market: { level: MarketLevel; versusUsual: string | null } | null; flights: FlightOption[] }> {
  const url = new URL('https://www.google.com/travel/flights');
  url.searchParams.set('q', `flights from ${origin} to ${stay.city} on ${stay.outboundDate} to ${stay.returnDate}`);
  url.searchParams.set('hl', 'en');
  url.searchParams.set('curr', currency);
  url.searchParams.set('gl', country);
  const browser = await launchBrowser();
  try {
    const context = await createStealthContext(browser);
    const page = await context.newPage();
    await page.goto(url.toString(), { waitUntil: 'domcontentloaded', timeout: 30_000 });
    await dismissGoogleConsent(page);
    await assertNotBlocked(page);
    await waitForFlightResults(page);
    const text = await page.evaluate(() => document.body.innerText);
    const labels = await page.evaluate(() => [...document.querySelectorAll('[aria-label]')]
      .map(element => element.getAttribute('aria-label') ?? '').filter(label => /round trip total/.test(label)));
    await context.close();
    const level = parseGoogleMarketLevel(text);
    return { market: level ? { level, versusUsual: parseMarketDifference(text) } : null, flights: parseFlightLabels(labels) };
  } finally {
    await browser.close().catch(() => undefined);
  }
}

/** A round trip picked on Google Flights down to its booking page: what the user opens to pay. */
export interface FlightBooking {
  url: string;
  outbound: FlightOption;
  inbound: FlightOption;
  /** Every way to buy it on the booking page (one ticket from the airline, separate tickets…), cheapest first. */
  options: BookingOption[];
  /**
   * For separate tickets, each ticket's own booking page on the airline's site (Google's "Purchase
   * separate tickets" dialog), so the user has a link per ticket; null when Google gave none.
   */
  legLinks?: { outbound: string | null; inbound: string | null };
}

/**
 * Google listed flights for a leg but none fits the user's times. Thrown rather than returned as null,
 * since null means the page was not read: the user was told "no flight at a suitable time" when Google
 * had simply not answered (2026-11-20).
 */
export class NoFittingFlight extends Error {
  constructor(readonly leg: 'outbound' | 'inbound') {
    super(`no ${leg} flight fits the preferred times`);
    this.name = 'NoFittingFlight';
  }
}

/**
 * Pick an outbound and a return on Google Flights (clicking them, as a person would) and return
 * the booking page they lead to, so the link opens that exact itinerary with its booking options
 * instead of a search the user has to work through. Null when the page or a choice is missing;
 * NoFittingFlight when flights were read and none fits.
 */
export async function bookFlight(origin: string, stay: Pick<ExploreQuote, 'city' | 'outboundDate' | 'returnDate'>, currency: string, country: string,
  choose: { outbound: (flights: FlightOption[]) => FlightOption | null; inbound: (flights: FlightOption[], outboundAirline: string) => FlightOption | null }):
  Promise<FlightBooking | null> {
  const url = new URL('https://www.google.com/travel/flights');
  url.searchParams.set('q', `flights from ${origin} to ${stay.city} on ${stay.outboundDate} to ${stay.returnDate}`);
  url.searchParams.set('hl', 'en');
  url.searchParams.set('curr', currency);
  url.searchParams.set('gl', country);
  const browser = await launchBrowser();
  try {
    const context = await createStealthContext(browser);
    const page = await context.newPage();
    await page.goto(url.toString(), { waitUntil: 'domcontentloaded', timeout: 30_000 });
    await dismissGoogleConsent(page);
    await assertNotBlocked(page);
    // Each result is a link whose label describes it; the labels change once a leg is chosen.
    const readRows = (exclude: readonly string[]) => page.waitForFunction(skip => {
      const labels = [...document.querySelectorAll('[role=link][aria-label]')].map(element => element.getAttribute('aria-label') ?? '')
        .filter(label => /round trip total/.test(label));
      return labels.length && labels.some(label => !skip.includes(label)) ? labels : null;
    }, exclude, { timeout: 35_000 }).then(handle => handle.jsonValue() as Promise<string[]>, () => [] as string[]);
    const clickRow = (label: string) => page.evaluate(wanted => {
      const row = [...document.querySelectorAll('[role=link][aria-label]')].find(element => element.getAttribute('aria-label') === wanted);
      (row as HTMLElement | undefined)?.click();
      return Boolean(row);
    }, label);
    const pick = (labels: readonly string[], option: FlightOption | null) => labels.find(label => {
      const parsed = parseFlightLabel(label);
      return parsed && option && parsed.airline === option.airline && parsed.departs === option.departs && parsed.price === option.price;
    });

    // A limit met here, after the flight list was read, leaves only the booking page unread.
    if (await waitForFlightResults(page).then(() => false, error => { if (error instanceof GoogleLimitedError) return true; throw error; })) return null;
    const outboundLabels = await readRows([]);
    const outboundFlights = parseFlightLabels(outboundLabels);
    const outbound = choose.outbound(outboundFlights);
    // Flights were read and none suits the user's times: say that, not "try again" (2026-11-20).
    if (outboundFlights.length && !outbound) throw new NoFittingFlight('outbound');
    const outboundLabel = pick(outboundLabels, outbound);
    if (!outbound || !outboundLabel || !(await clickRow(outboundLabel))) return null;
    const returnLabels = await readRows(outboundLabels);
    const returnFlights = parseFlightLabels(returnLabels.filter(label => !outboundLabels.includes(label)));
    const inbound = choose.inbound(returnFlights, outbound.airline);
    if (returnFlights.length && !inbound) throw new NoFittingFlight('inbound');
    const inboundLabel = pick(returnLabels, inbound);
    if (!inbound || !inboundLabel || !(await clickRow(inboundLabel))) return null;
    const booked = await page.waitForURL(/\/travel\/flights\/booking/, { timeout: 30_000 }).then(() => true, () => false);
    if (!booked) return null;
    await page.waitForFunction(() => /Booking options/.test(document.body.innerText), undefined, { timeout: 20_000 }).catch(() => undefined);
    const text = await page.evaluate(() => document.body.innerText);
    // Read every option: a page offering one ticket and separate tickets showed a fee line first (2026-10-19).
    const options = parseBookingOptions(text);
    const bookingUrl = page.url();
    const legLinks = options.some(option => option.separateTickets) ? await separateTicketLinks(page) : undefined;
    const result: FlightBooking = { url: bookingUrl, outbound, inbound, legLinks,
      options: options.length ? options : [{ seller: inbound.airline, separateTickets: false, price: inbound.price }] };
    await context.close();
    return result;
  } finally {
    await browser.close().catch(() => undefined);
  }
}

/**
 * The airline page behind each ticket of a separate-tickets option: its Continue opens Google's
 * "Purchase separate tickets" dialog, whose two Continue buttons (departing, then returning) each
 * open the airline's own booking page in a new tab. Overlays catch real clicks, so they are clicked
 * from the page script.
 */
async function separateTicketLinks(page: Page): Promise<{ outbound: string | null; inbound: string | null }> {
  const clickLeg = (index: number) => page.evaluate(n => {
    const button = [...document.querySelectorAll<HTMLElement>('[role=dialog] button[aria-label^="Continue to book with"]')][n];
    button?.click();
    return Boolean(button);
  }, index);
  const opened = async (index: number) => {
    const popup = page.context().waitForEvent('page', { timeout: 15_000 }).catch(() => null);
    if (!(await clickLeg(index))) return null;
    const tab = await popup;
    if (!tab) return null;
    // The first request to the airline is the link: Jetstar turns the headless browser away, so the
    // tab may end on an error page (2026-10-19).
    const airline = (url: string) => /^https?:/.test(url) && !/(^|\.)google\./.test(new URL(url).hostname);
    const request = await tab.waitForEvent('request', { predicate: request => request.isNavigationRequest() && airline(request.url()), timeout: 15_000 })
      .then(request => request.url(), () => null);
    await tab.close().catch(() => undefined);
    return request ?? (airline(tab.url()) ? tab.url() : null);
  };
  const separate = await page.evaluate(() => {
    const button = [...document.querySelectorAll<HTMLElement>('button[aria-label^="Continue to book with"]')].find(element => /booked individually/.test(element.getAttribute('aria-label')!));
    button?.click();
    return Boolean(button);
  });
  if (!separate) return { outbound: null, inbound: null };
  const shown = await page.waitForFunction(() => /Purchase separate tickets/.test(document.body.innerText), undefined, { timeout: 10_000 }).then(() => true, () => false);
  if (!shown) return { outbound: null, inbound: null };
  return { outbound: await opened(0), inbound: await opened(1) };
}

const ROOM_CHECK_PAGES = 3;

/** Google answered with its "unusual traffic" page instead of results: too many automated requests. */
export class GoogleBlockedError extends Error {
  constructor() {
    super('Google 暫時擋住了自動查詢（unusual traffic）');
    this.name = 'GoogleBlockedError';
  }
}

/**
 * Google answered with "Oops, something went wrong" even after its own Reload: not the "unusual traffic"
 * page, but it stays that way for minutes on every search (2026-11-21), so it is handled like a block
 * with a shorter pause. A GoogleBlockedError, so every place that passes a block on passes this too.
 */
export class GoogleLimitedError extends GoogleBlockedError {
  constructor() {
    super();
    this.message = 'Google 暫時限流（每個查詢都回 Oops, something went wrong）';
    this.name = 'GoogleLimitedError';
  }
}

async function assertNotBlocked(page: Page): Promise<void> {
  const blocked = /\/sorry\//.test(page.url())
    || await page.evaluate(() => /unusual traffic from your computer network|our systems have detected unusual traffic/i.test(document.body?.innerText ?? '')).catch(() => false);
  if (blocked) throw new GoogleBlockedError();
}

/** A guest rating and who gave it: Google's own, or another travel site's when Google hides it. */
export interface HotelRating { score: number; source: string }

/**
 * Open each hotel's Google Hotels prices panel for the stay and read the room
 * names it lists, so hotels that only sell dorm beds can be dropped even when
 * Google gives them a hotel class. Runs a few pages at a time in one browser.
 */
export async function checkHotelRooms(watch: Pick<DealWatch, 'currency' | 'country'>,
  stay: Pick<ExploreQuote, 'city' | 'outboundDate' | 'returnDate'>, names: readonly string[], links: ReadonlyMap<string, string> = new Map()): Promise<{ rooms: Map<string, RoomCheck>; ratings: Map<string, HotelRating>; sites: Map<string, SiteOffer[]>; photos: Map<string, HotelPhotos>; addresses: Map<string, string> }> {
  const results = new Map<string, RoomCheck>(names.map(name => [name, { status: 'unknown' }]));
  // Some list layouts print no ratings, so read each hotel's from its own search page.
  const ratings = new Map<string, HotelRating>();
  // Each booking site's price for the stay, from the same panel.
  const sites = new Map<string, SiteOffer[]>();
  // Room and bathroom photos, so the user can judge a room without opening a booking site.
  const photos = new Map<string, HotelPhotos>();
  const addresses = new Map<string, string>();
  const rooms = new Map<string, HotelPhotos['rooms']>();
  if (!names.length) return { rooms: results, ratings, sites, photos, addresses };
  const browser = await launchBrowser();
  try {
    const context = await createStealthContext(browser);
    const queue = [...names];
    const worker = async () => {
      const page = await context.newPage();
      try {
        for (let name = queue.shift(); name !== undefined; name = queue.shift()) {
          try {
            // The hotel's own link from the list opens its prices panel for the stay, whichever page of
            // results it was on (2026-10-19: hotels from pages 2–3 came back with no room types).
            const direct = links.get(name);
            if (direct) {
              await page.goto(direct, { waitUntil: 'domcontentloaded', timeout: 30_000 });
              await dismissGoogleConsent(page);
              await assertNotBlocked(page);
            } else {
              // The same list search the candidates came from: a query led by the hotel's name can open
              // on Google's default dates (2026-10-16: slash 川崎 priced for Oct 19–20 instead of Oct 29–Nov 4).
              const url = buildHotelSearchUrl({ city: stay.city, checkIn: stay.outboundDate, checkOut: stay.returnDate,
                currency: watch.currency, country: watch.country });
              await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30_000 });
              await dismissGoogleConsent(page);
              await assertNotBlocked(page);
              const findLink = (hotel: string) => {
                const wanted = `view prices for ${hotel}`.toLowerCase();
                return [...document.querySelectorAll('a')].find(a => (a.getAttribute('aria-label') ?? '').replace(/\s+/g, ' ').trim().toLowerCase() === wanted);
              };
              const waitForListing = (timeout: number) => page.waitForFunction(`(${findLink.toString()})(${JSON.stringify(name)}) !== undefined`, undefined, { timeout })
                .then(() => true, () => false);
              let listed = await waitForListing(15_000);
              if (!listed) {
                // Google reorders the list between loads, so a hotel from the first load can be missing
                // (2026-10-18: Hotel Takao Asile went out with no photos). Search for it by name instead;
                // prices are still kept only when the date inputs show the stay.
                await page.goto(buildHotelNameUrl({ name, city: stay.city, checkIn: stay.outboundDate, checkOut: stay.returnDate,
                  currency: watch.currency, country: watch.country, language: 'en' }), { waitUntil: 'domcontentloaded', timeout: 30_000 });
                listed = await waitForListing(15_000);
              }
              if (!listed) {
                console.error(`[deals] room check for ${name}: not found in the list or by name`);
                continue;
              }
              // Read the rating while the list is still shown; opening the prices panel replaces it.
              const rating = parseLabelRatings(await page.evaluate(() => [...document.querySelectorAll('[aria-label]')]
                .map(element => element.getAttribute('aria-label') ?? '').filter(label => label.includes('out of 5 stars')))).get(name);
              if (rating !== undefined) ratings.set(name, { score: rating, source: 'Google' });
              await page.evaluate(`(${findLink.toString()})(${JSON.stringify(name)}).click()`);
            }
            await page.waitForFunction(() => /All options|Featured options/.test(document.body.innerText), undefined, { timeout: 12_000 }).catch(() => undefined);
            const panel = await page.evaluate(() => {
              const text = document.body.innerText;
              return text.slice(text.lastIndexOf('Back to list of all results'));
            });
            // Room prices from a panel showing other dates would be wrong; leave the hotel unconfirmed instead.
            if (await stayApplied(page, stay)) {
              results.set(name, classifyRooms(parseRoomOffers(panel)));
              const links = await page.evaluate(() => [...document.querySelectorAll('a[href*="/travel/lodging/clk"]')]
                .map(anchor => ({ text: (anchor as HTMLElement).innerText, url: (anchor as HTMLAnchorElement).href })));
              sites.set(name, attachSiteLinks(parseSiteOffers(panel), links));
              // Each room row in the panel carries that room's own photo. Only offer rows ("… Visit site"):
              // the page also holds cards of similar hotels, whose photos once went out as this hotel's.
              rooms.set(name, roomPhotos(await page.evaluate(() => [...document.querySelectorAll('a')]
                .filter(anchor => /Visit site/.test((anchor as HTMLElement).innerText))
                .map(anchor => ({ name: (anchor as HTMLElement).innerText.split('\n')[0]?.trim() ?? '', url: anchor.querySelector('img')?.src ?? '' }))
                .filter(row => row.name && row.url))));
            } else console.error(`[deals] room check for ${name} opened on other dates; skipped`);
            if (!ratings.has(name)) {
              // Some Google layouts hide their own score; the Reviews tab still lists other sites' ratings.
              await page.evaluate(() => ([...document.querySelectorAll('[role=tab], a, button, div')]
                .find(element => element.textContent?.trim() === 'Reviews') as HTMLElement | undefined)?.click());
              await page.waitForFunction(() => /Reviews on other travel sites|\d\/5 · /.test(document.body.innerText), undefined, { timeout: 6_000 }).catch(() => undefined);
              const site = bestSiteRating(await page.evaluate(() => {
                const text = document.body.innerText;
                return text.slice(text.lastIndexOf('Back to list of all results'), text.lastIndexOf('Back to list of all results') + 1500);
              }));
              if (site) ratings.set(name, { score: site.score, source: site.site });
            }
            // The Photos tab groups photos by subject ("Bedroom (299)", "Bathroom (209)"); each group
            // section shows its first two as thumbnails.
            await page.evaluate(() => ([...document.querySelectorAll('[role=tab], a, button, div')]
              .find(element => element.textContent?.trim() === 'Photos') as HTMLElement | undefined)?.click());
            await page.waitForFunction(() => /Bedroom \(|Bathroom \(/.test(document.body.innerText), undefined, { timeout: 8_000 }).catch(() => undefined);
            // No named helpers inside evaluate: the bundler wraps them in a __name() the page does not have.
            const thumbnails = await page.evaluate(() => {
              const found: Record<'bedroom' | 'bathroom', string[]> = { bedroom: [], bathroom: [] };
              for (const section of document.querySelectorAll('section')) {
                const subject = /^(Bedroom|Bathroom) \(/.exec(section.innerText ?? '')?.[1]?.toLowerCase() as 'bedroom' | 'bathroom' | undefined;
                if (subject) found[subject].push(...[...section.querySelectorAll('img')].map(image => image.src || image.dataset.src || ''));
              }
              return found;
            });
            const large = (urls: string[]) => urls.map(url => largePhotoUrl(url)).filter((url): url is string => url !== null).slice(0, 2);
            const found = { rooms: rooms.get(name) ?? [], bedroom: large(thumbnails.bedroom), bathroom: large(thumbnails.bathroom) };
            if (found.rooms.length || found.bedroom.length || found.bathroom.length) photos.set(name, found);
            // The Overview tab gives the address: where the hotel is (2026-10-19: four of six "Okinawa"
            // hotels were an hour from Naha, and the list did not say).
            // A hotel without its address is left out of the list, so a slow tab gets a second, longer try.
            let address: string | null = null;
            for (const wait of [6_000, 12_000]) {
              await page.evaluate(() => ([...document.querySelectorAll('[role=tab], a, button, div')]
                .find(element => element.textContent?.trim() === 'Overview') as HTMLElement | undefined)?.click());
              await page.waitForFunction(() => /\d{3}-\d{4},? Japan|\bTaiwan\b|台灣|臺灣/.test(document.body.innerText), undefined, { timeout: wait }).catch(() => undefined);
              address = await page.evaluate(() => {
                const text = document.body.innerText;
                const slice = text.slice(text.lastIndexOf('Back to list of all results'));
                return /[^\n•]*\d{3}-\d{4},? Japan/.exec(slice)?.[0]?.trim()
                  ?? /[^\n•]*(?:Taiwan|台灣|臺灣)[^\n•]*/.exec(slice)?.[0]?.trim()
                  ?? null;
              });
              if (address) break;
            }
            if (address) addresses.set(name, address);
            else console.error(`[deals] no address for ${name}`);
          } catch (error) {
            // A block ends the whole check: more requests would only prolong it.
            if (error instanceof GoogleBlockedError) throw error;
            console.error(`[deals] room check failed for ${name}: ${error instanceof Error ? error.message.slice(0, 120) : 'unknown'}`);
          }
        }
      } finally {
        await page.close().catch(() => undefined);
      }
    };
    await Promise.all(Array.from({ length: Math.min(ROOM_CHECK_PAGES, names.length) }, worker));
    // Google sometimes answers a page slowly or partly, leaving a hotel with nothing at all (2026-10-18:
    // APA Hotel Ayase-Ekimae went out without photos once and complete the next run). One more pass,
    // on a single page, for the hotels that got neither a room check nor photos.
    queue.push(...names.filter(name => results.get(name)?.status === 'unknown' && !photos.has(name)));
    if (queue.length) await worker();
    await context.close();
    return { rooms: results, ratings, sites, photos, addresses };
  } finally {
    await browser.close().catch(() => undefined);
  }
}

/** Earlier scans' cheapest fare per city within the history window, and each city's last promotion. */
function cityHistory(watch: DealWatch, scannedAt: Date): Map<string, CityHistory> {
  const since = new Date(scannedAt.getTime() - DEFAULT_DEAL_RULES.historyDays * 86_400_000);
  const cheapest = new Map<string, Map<number, number>>();
  for (const row of observations(watch.id, { since, before: scannedAt, currency: watch.currency })) {
    const scans = cheapest.get(row.city) ?? new Map<number, number>();
    const at = row.scannedAt.getTime();
    scans.set(at, Math.min(scans.get(at) ?? Infinity, row.price));
    cheapest.set(row.city, scans);
  }
  const alerts = recentAlerts({ kind: 'deal', watchId: watch.id, since, limit: 500 });
  const history = new Map<string, CityHistory>();
  for (const [city, scans] of cheapest) {
    const last = alerts.find(alert => alert.city === city && alert.price !== null);
    history.set(city, {
      scanPrices: [...scans].map(([at, price]) => ({ price, scannedAt: new Date(at) })),
      lastAlert: last ? { price: last.price!, at: last.createdAt } : null,
    });
  }
  return history;
}

const eventKey = (...parts: unknown[]) => createHash('sha256').update(JSON.stringify(parts)).digest('hex');

export type HotelLookup = (quote: ExploreQuote) => Promise<HotelSummary | null>;

/**
 * Store one scan, then queue its promotion alerts. Hotel prices for the top promotions are fetched
 * before the write, so a slow page never holds the database.
 */
export async function recordDealScan(watch: DealWatch, quotes: readonly ExploreQuote[], scannedAt: Date,
  hotelsFor: HotelLookup = async () => null): Promise<{ deals: number }> {
  const deals = findDeals(quotes, cityHistory(watch, scannedAt), scannedAt);
  const hotels = new Map<string, HotelSummary | null>();
  for (const deal of deals.slice(0, MAX_HOTEL_LOOKUPS)) {
    hotels.set(deal.quote.city, await hotelsFor(deal.quote).catch(error => {
      console.error(`[deals] hotel lookup failed for ${deal.quote.city}: ${error instanceof Error ? error.message.slice(0, 200) : 'unknown'}`);
      return null;
    }));
  }
  return transaction(() => {
    addObservations(quotes.map(quote => ({ watchId: watch.id, city: quote.city, outboundDate: quote.outboundDate, returnDate: quote.returnDate,
      stops: quote.stops, duration: quote.duration ?? null, price: quote.price, currency: watch.currency, scannedAt })));
    for (const deal of deals) {
      addAlert({ watchId: watch.id, kind: 'deal', city: deal.quote.city, price: deal.quote.price,
        eventKey: `deal:${eventKey(watch.id, deal.quote.city, deal.quote.price, deal.quote.outboundDate, scannedAt.toISOString())}`,
        message: dealMessage(deal, context(watch), hotels.get(deal.quote.city) ?? null) }, scannedAt);
    }
    if (watch.failureAlerted) {
      addAlert({ watchId: watch.id, kind: 'health', eventKey: `health:${eventKey(watch.id, 'recovered', scannedAt.toISOString())}`,
        message: scanRecoveredMessage(context(watch), quotes.length) }, scannedAt);
    }
    updateWatch(watch.id, { lastScanAt: scannedAt, lastError: null, failureCount: 0, failureAlerted: false });
    return { deals: deals.length };
  });
}

/** "2026-10-18" in Taipei. */
function taipeiDateKey(date: Date): string {
  return new Date(date.getTime() + TAIPEI_OFFSET_MS).toISOString().slice(0, 10);
}

/**
 * Send each watch's daily cheapest list at its Taipei hour, built from its latest scan. A list is
 * skipped when that scan is stale, so a missing daily list doubles as a sign the scanner needs attention.
 */
export function queueDueDigests(now = new Date()): number {
  const day = taipeiDateKey(now);
  const hour = new Date(now.getTime() + TAIPEI_OFFSET_MS).getUTCHours();
  let queued = 0;
  for (const watch of listWatches({ active: true })) {
    if (!watch.sendDigest || watch.digestHour > hour || watch.lastDigestDay === day) continue;
    if (!watch.lastScanAt || watch.lastScanAt.getTime() < now.getTime() - DIGEST_MAX_SCAN_AGE_MS) continue;
    const quotes: ExploreQuote[] = observations(watch.id, { scannedAt: watch.lastScanAt }).map(row => ({ city: row.city,
      outboundDate: row.outboundDate, returnDate: row.returnDate, stops: row.stops, duration: row.duration ?? '', price: row.price }));
    if (!quotes.length) continue;
    transaction(() => {
      updateWatch(watch.id, { lastDigestDay: day });
      addAlert({ watchId: watch.id, kind: 'digest', eventKey: `digest:${eventKey(watch.id, day)}`,
        message: digestMessage(cheapestCities(quotes, DIGEST_SIZE), context(watch), day) }, now);
    });
    queued++;
  }
  return queued;
}

export async function scanDealWatch(watch: DealWatch, now = new Date(), capture = captureExplorePage) {
  if (!claimWatch(watch, new Date(now.getTime() + watch.intervalHours * 3_600_000))) return null;
  try {
    const quotes = parseExplorePage(await capture(watch), now);
    if (!quotes.length) throw new Error('The Explore page showed no destination prices');
    const result = await recordDealScan(watch, quotes, now, quote => lookupHotels(watch, quote));
    console.log(`[deals] ${watch.origin}→${watch.region} (${watch.tripLength}): ${quotes.length} cities, ${result.deals} promotions`);
    return { quotes: quotes.length, ...result };
  } catch (error) {
    const message = error instanceof Error ? error.message.slice(0, 300) : 'Scan failed';
    console.error(`[deals] ${watch.origin}→${watch.region} scan failed: ${message}`);
    // Retry sooner than a full interval when Google was slow or blocked, and tell the user once
    // when the failures keep repeating.
    const failureCount = watch.failureCount + 1;
    const alertNow = !watch.failureAlerted && failureCount >= FAILURE_ALERT_AFTER;
    transaction(() => {
      updateWatch(watch.id, { lastError: message, failureCount, ...(alertNow ? { failureAlerted: true } : {}),
        nextScanAt: new Date(now.getTime() + Math.min(watch.intervalHours, 1) * 3_600_000) });
      if (alertNow) {
        addAlert({ watchId: watch.id, kind: 'health', eventKey: `health:${eventKey(watch.id, 'failing', now.toISOString())}`,
          message: scanFailingMessage(context(watch), failureCount, message) }, now);
      }
    });
    // A block stops the whole pass: more requests now would only prolong it.
    if (error instanceof GoogleBlockedError) throw error;
    return null;
  }
}

/** Send queued alerts to the trip chat, retrying failures with backoff. */
export async function deliverDealAlerts(send: (message: AlertMessage) => Promise<void>, now = new Date()): Promise<number> {
  let sent = 0;
  for (const alert of dueAlerts(now)) {
    const attempts = alert.attempts + 1;
    try {
      await send(alert.message);
      updateAlert(alert.id, { pending: false, attempts, nextAttemptAt: now, lastError: null });
      sent++;
    } catch (error) {
      updateAlert(alert.id, { pending: attempts < MAX_DELIVERY_ATTEMPTS, attempts,
        nextAttemptAt: new Date(now.getTime() + Math.min(2 ** attempts, 60) * 60_000),
        lastError: error instanceof Error ? error.message.slice(0, 300) : 'Delivery failed' });
    }
  }
  return sent;
}

/** One pass: scan the watches that are due (a few at a time), queue the daily lists, send alerts. */
export async function runDueDealWatches(send: (message: AlertMessage) => Promise<void>, now = new Date()): Promise<void> {
  const due = listWatches({ active: true }).filter(watch => watch.nextScanAt <= now)
    .sort((a, b) => a.nextScanAt.getTime() - b.nextScanAt.getTime()).slice(0, 3);
  try {
    for (const watch of due) await scanDealWatch(watch, now);
  } finally {
    queueDueDigests(now);
    await deliverDealAlerts(send, now);
  }
}
