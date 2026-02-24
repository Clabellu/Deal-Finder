import asyncio
import json
import os
import random
import re
import statistics
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

from utils.logger import get_logger

load_dotenv()

logger = get_logger("price_checker")

_FINDING_URL = "https://svcs.ebay.com/services/search/FindingService/v1"
_EBAY_SOLD_URL = "https://www.ebay.it/sch/i.html"

_REQUEST_TIMEOUT = 20
_DEFAULT_CACHE_TTL = 86400  # 24 ore
_SCRAPE_DELAY = 1.5  # secondi minimi tra richieste scraping
_MAX_SCRAPE_RETRIES = 3

# Headers realistici come Subito scraper
_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_IMPERSONATE = "chrome131"

# Mappa condizione interna -> filtro eBay
_CONDITION_MAP = {
    "nuovo": "1000",
    "come_nuovo": "1500",
    "usato_buono": "3000",
    "usato_discreto": "3000",
}


@dataclass
class PriceResult:
    """Risultato della ricerca prezzo di mercato."""

    median_price: float
    mean_price: float
    min_price: float
    max_price: float
    sold_count: int
    reliable: bool  # True se sold_count >= 5


class PriceChecker:
    """Cerca il prezzo medio di mercato su eBay.it tramite scraping (come Subito).

    Approccio scraping-first: nessun rate limit API.
    Fallback opzionale su API se lo scraping non trova nulla.
    """

    def __init__(
        self,
        sold_items_count: int = 20,
        use_median: bool = True,
        max_days_sold: int = 30,
        db=None,
        cache_ttl: int = _DEFAULT_CACHE_TTL,
    ):
        self._sold_items_count = sold_items_count
        self._use_median = use_median
        self._max_days_sold = max_days_sold
        self._cache_ttl = cache_ttl
        self._app_id = os.environ.get("EBAY_APP_ID", "")

        self._db = db
        self._cache: dict[tuple[str, str], tuple[float, list[float]]] = {}
        self._last_scrape: float = 0
        # Sessione scraping condivisa (inizializzata al primo uso)
        self._session: Optional[AsyncSession] = None

    async def _ensure_session(self) -> AsyncSession:
        """Crea/riusa la sessione scraping.

        Non fa warm-up sulla homepage (causa curl error 23 su eBay.it).
        La ricerca diretta funziona senza warm-up.
        """
        if self._session is None:
            self._session = AsyncSession(
                headers=_HEADERS,
                impersonate=_IMPERSONATE,
                timeout=_REQUEST_TIMEOUT,
            )
        return self._session

    async def close(self):
        """Chiude la sessione scraping."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ #
    #  Entry point principale                                              #
    # ------------------------------------------------------------------ #

    async def check_price(
        self,
        search_query: str,
        condition: str = "",
    ) -> Optional[PriceResult]:
        """Cerca prezzi su eBay per un dato prodotto.

        Strategia scraping-first (nessun rate limit):
        1. Scraping venduti eBay.it
        2. Scraping inserzioni attive eBay.it
        3. API come ultima risorsa (opzionale)

        Args:
            search_query: Query di ricerca ottimizzata (dal LLM parser).
            condition: Condizione del prodotto per filtrare i risultati.

        Returns:
            PriceResult con statistiche di prezzo, o None se la ricerca fallisce.
        """
        clean_query = self._clean_query(search_query)
        short_query = self._shorten_query(clean_query)

        # Costruisci lista tentativi con query progressivamente piu' larghe
        attempts = [(clean_query, condition)]
        if condition:
            attempts.append((clean_query, ""))
        if short_query != clean_query:
            attempts.append((short_query, ""))

        prices: list[float] = []
        used_query = clean_query
        from_active = False

        # --- Fase 1: scraping venduti ---
        for query, cond in attempts:
            result = await self._get_prices_cached(query, cond, sold=True)
            if result:
                prices = result
                used_query = query
                break
            logger.debug("Scraping venduti: niente per '%s' (cond='%s')", query, cond)

        # --- Fase 2: scraping inserzioni attive ---
        if not prices:
            logger.info("Nessun venduto trovato per '%s', cerco inserzioni attive", search_query)
            for query, cond in attempts:
                result = await self._get_prices_cached(query, cond, sold=False)
                if result:
                    prices = result
                    used_query = query
                    from_active = True
                    logger.info(
                        "Trovati %d prezzi da inserzioni attive per '%s'",
                        len(prices), query,
                    )
                    break

        # --- Fase 3: API come ultima risorsa (opzionale) ---
        if not prices and self._app_id:
            logger.info("Scraping fallito per '%s', provo API eBay", search_query)
            for query, cond in attempts:
                result = await self._try_api(query, cond)
                if result:
                    prices = result
                    used_query = query
                    break

        if not prices:
            logger.warning("Nessun prezzo trovato su eBay per: %s", search_query)
            return None

        # Filtra outlier (prezzi sotto il 10% della mediana)
        raw_median = statistics.median(prices)
        threshold = raw_median * 0.10
        filtered = [p for p in prices if p >= threshold]
        if not filtered:
            filtered = prices

        median_price = statistics.median(filtered)
        mean_price = statistics.mean(filtered)

        # Inserzioni attive: servono piu' campioni per essere affidabili
        min_reliable = 5 if not from_active else 8

        result = PriceResult(
            median_price=round(median_price, 2),
            mean_price=round(mean_price, 2),
            min_price=round(min(filtered), 2),
            max_price=round(max(filtered), 2),
            sold_count=len(filtered),
            reliable=len(filtered) >= min_reliable,
        )

        source = "attive" if from_active else "venduti"
        logger.info(
            "eBay prezzo per '%s' (%s): mediana=%.2f, media=%.2f, campioni=%d, affidabile=%s",
            used_query,
            source,
            result.median_price,
            result.mean_price,
            result.sold_count,
            result.reliable,
        )
        return result

    # ------------------------------------------------------------------ #
    #  Query cleaning                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _clean_query(query: str) -> str:
        """Rimuove parole inutili dalla query di ricerca eBay."""
        noise_words = {
            "usato", "usata", "usati", "usate",
            "nuovo", "nuova", "nuovi", "nuove",
            "come nuovo", "come nuova",
            "ricondizionato", "ricondizionata", "refurbished",
            "ottime condizioni", "buone condizioni", "perfetto stato",
            "spedizione", "spedizione gratuita", "gratis",
            "promo", "offerta", "affare", "occasione",
            "originale", "garanzia",
        }
        result = query
        for word in sorted(noise_words, key=len, reverse=True):
            result = re.sub(rf'\b{re.escape(word)}\b', '', result, flags=re.IGNORECASE)
        return re.sub(r'\s+', ' ', result).strip()

    @staticmethod
    def _shorten_query(query: str) -> str:
        """Riduce la query rimuovendo varianti (storage, colore) ma mantenendo il modello completo.

        Es: 'iPhone 17 Pro Max 256GB nero' -> 'iPhone 17 Pro Max'
            'Samsung Galaxy S24 Ultra 512GB' -> 'Samsung Galaxy S24 Ultra'
        """
        words = query.split()
        if len(words) <= 3:
            return query

        variant_patterns = re.compile(
            r"^\d+\s*(?:GB|TB|MB|G)$|^\d+$|^(?:nero|bianco|grigio|blu|rosso|verde|viola|"
            r"oro|argento|titanio|black|white|grey|gray|blue|red|green|gold|silver|purple|"
            r"titanium|natural|pink|rosa|midnight|starlight|space)$",
            re.IGNORECASE,
        )
        core_words = [w for w in words if not variant_patterns.match(w)]

        if core_words and len(core_words) < len(words):
            return " ".join(core_words)

        return " ".join(words[:4]) if len(words) > 4 else query

    # ------------------------------------------------------------------ #
    #  Cache + scraping (metodo primario)                                  #
    # ------------------------------------------------------------------ #

    async def _get_prices_cached(
        self, query: str, condition: str, *, sold: bool
    ) -> list[float]:
        """Restituisce prezzi dalla cache o tramite scraping.

        Args:
            sold: True per venduti completati, False per inserzioni attive.
        """
        prefix = "sold" if sold else "active"
        cache_key = (f"{prefix}:{query.lower().strip()}", condition)
        now = time.monotonic()

        # 1. Cache in-memory
        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, prices = cached
            if now - ts < self._cache_ttl:
                logger.debug("Cache hit per '%s' [%s] (%d prezzi)", query, prefix, len(prices))
                return prices

        # 2. Cache DB
        if self._db is not None:
            try:
                db_prices = await self._db.get_cached_prices(
                    f"{prefix}:{query}", condition, self._cache_ttl
                )
                if db_prices is not None:
                    logger.debug("Cache DB hit per '%s' [%s] (%d prezzi)", query, prefix, len(db_prices))
                    self._cache[cache_key] = (time.monotonic(), db_prices)
                    return db_prices
            except Exception:
                logger.debug("Errore cache DB per '%s'", query, exc_info=True)

        # 3. Scraping
        if sold:
            prices = await self._scrape_ebay_sold(query, condition)
        else:
            prices = await self._scrape_ebay_active(query, condition)

        # Salva in cache (anche se vuoto, per evitare richieste ripetute)
        self._cache[cache_key] = (time.monotonic(), prices)
        if self._db is not None:
            try:
                await self._db.save_cached_prices(f"{prefix}:{query}", condition, prices)
            except Exception:
                logger.debug("Errore scrittura cache DB per '%s'", query, exc_info=True)

        return prices

    # ------------------------------------------------------------------ #
    #  Scraping eBay.it (metodo primario, nessun rate limit)               #
    # ------------------------------------------------------------------ #

    async def _scrape_with_retry(self, params: dict[str, str]) -> Optional[str]:
        """Esegue una richiesta scraping con retry e backoff (come Subito)."""
        session = await self._ensure_session()

        # Rispetta delay tra richieste
        elapsed = time.monotonic() - self._last_scrape
        if elapsed < _SCRAPE_DELAY:
            await asyncio.sleep(_SCRAPE_DELAY - elapsed + random.uniform(0.2, 0.8))

        for attempt in range(_MAX_SCRAPE_RETRIES):
            try:
                resp = await session.get(_EBAY_SOLD_URL, params=params)
                self._last_scrape = time.monotonic()

                if resp.status_code == 429:
                    wait = (2 ** attempt) + random.uniform(0.5, 1.5)
                    logger.warning("eBay scrape: rate limited (429), retry tra %.1fs", wait)
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code == 403:
                    wait = (2 ** attempt) + random.uniform(0.5, 1.5)
                    logger.warning("eBay scrape: forbidden (403), retry tra %.1fs", wait)
                    # Reset sessione per prendere nuovi cookie
                    self._session = None
                    self._warmed_up = False
                    session = await self._ensure_session()
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code != 200:
                    logger.warning("eBay scrape HTTP %d", resp.status_code)
                    return None

                return resp.text

            except Exception as e:
                if attempt < _MAX_SCRAPE_RETRIES - 1:
                    wait = (2 ** attempt) + random.uniform(0.5, 1.5)
                    logger.warning("eBay scrape: errore rete, retry tra %.1fs: %s", wait, e)
                    await asyncio.sleep(wait)
                    continue
                logger.warning("eBay scrape: tutti i tentativi falliti", exc_info=True)
                return None

        logger.warning("eBay scrape: tentativi esauriti")
        return None

    async def _scrape_ebay_sold(self, query: str, condition: str) -> list[float]:
        """Scraping eBay.it per oggetti venduti."""
        params: dict[str, str] = {
            "_nkw": query,
            "LH_Complete": "1",
            "LH_Sold": "1",
            "_sop": "13",
            "rt": "nc",
            "_ipg": "60",
        }
        cond_id = _CONDITION_MAP.get(condition)
        if cond_id:
            params["LH_ItemCondition"] = cond_id

        html = await self._scrape_with_retry(params)
        if html is None:
            return []

        prices = self._parse_ebay_html(html)
        if prices:
            logger.info(
                "eBay scrape venduti per '%s': %d prezzi (mediana %.2f)",
                query, len(prices), statistics.median(prices),
            )
        else:
            logger.info("eBay scrape venduti per '%s': nessun prezzo trovato", query)
        return prices

    async def _scrape_ebay_active(self, query: str, condition: str) -> list[float]:
        """Scraping eBay.it per inserzioni attive (Compralo Subito)."""
        params: dict[str, str] = {
            "_nkw": query,
            "LH_BIN": "1",
            "_sop": "12",
            "rt": "nc",
            "_ipg": "60",
        }
        cond_id = _CONDITION_MAP.get(condition)
        if cond_id:
            params["LH_ItemCondition"] = cond_id

        html = await self._scrape_with_retry(params)
        if html is None:
            return []

        prices = self._parse_ebay_html(html)
        if prices:
            logger.info(
                "eBay scrape attive per '%s': %d prezzi (mediana %.2f)",
                query, len(prices), statistics.median(prices),
            )
        else:
            logger.debug("eBay scrape attive per '%s': nessun prezzo", query)
        return prices

    @staticmethod
    def _parse_ebay_html(html: str) -> list[float]:
        """Estrae i prezzi dalla pagina HTML di eBay.it (venduti o attivi).

        Strategia multi-livello:
        1. Selettori CSS (metodo classico)
        2. JSON-LD structured data (Schema.org, piu' stabile)
        3. JSON embedded nei tag <script> della pagina
        4. Regex fallback diretto sull'HTML
        """
        soup = BeautifulSoup(html, "html.parser")

        # --- Metodo 1: selettori CSS ---
        prices = _parse_css_selectors(soup)
        if prices:
            logger.debug("Prezzi estratti via CSS selectors: %d", len(prices))
            return prices

        # --- Metodo 2: JSON-LD (application/ld+json) ---
        prices = _parse_json_ld(soup)
        if prices:
            logger.debug("Prezzi estratti via JSON-LD: %d", len(prices))
            return prices

        # --- Metodo 3: JSON embedded negli script ---
        prices = _parse_embedded_json(html)
        if prices:
            logger.debug("Prezzi estratti via JSON embedded: %d", len(prices))
            return prices

        # --- Metodo 4: regex fallback diretto ---
        prices = _parse_regex_fallback(html)
        if prices:
            logger.debug("Prezzi estratti via regex fallback: %d", len(prices))
            return prices

        # Diagnostica: log INFO con indicatori per capire cosa c'e' nell'HTML
        has_consent = "consent" in html.lower() or "gdpr" in html.lower()
        has_captcha = "captcha" in html.lower()
        has_items = "s-item" in html
        title_match = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
        page_title = title_match.group(1).strip()[:100] if title_match else "(nessun titolo)"
        logger.info(
            "eBay scrape: parsing fallito (len=%d, titolo='%s', "
            "s-item=%s, consent=%s, captcha=%s)",
            len(html), page_title, has_items, has_consent, has_captcha,
        )
        return []

    # ------------------------------------------------------------------ #
    #  API eBay: ultima risorsa (opzionale)                                #
    # ------------------------------------------------------------------ #

    async def _try_api(self, query: str, condition: str) -> list[float]:
        """Tenta una singola chiamata API come ultima risorsa.

        Non fa retry aggressivi: se fallisce, torna vuoto.
        """
        if not self._app_id:
            return []

        # Prova prima venduti, poi attivi
        for operation, response_key in [
            ("findCompletedItems", "findCompletedItemsResponse"),
            ("findItemsByKeywords", "findItemsByKeywordsResponse"),
        ]:
            prices = await self._api_call(query, condition, operation, response_key)
            if prices:
                logger.info("eBay API %s per '%s': %d prezzi", operation, query, len(prices))
                return prices

        return []

    async def _api_call(
        self,
        query: str,
        condition: str,
        operation: str,
        response_key: str,
    ) -> list[float]:
        """Singola chiamata alla Finding API di eBay."""
        filter_idx = 0
        params: dict[str, str] = {
            "OPERATION-NAME": operation,
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": self._app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "",
            "keywords": query,
            "paginationInput.entriesPerPage": "60",
            "paginationInput.pageNumber": "1",
            "sortOrder": "EndTimeSoonest" if "Completed" in operation else "BestMatch",
        }
        headers = {"X-EBAY-SOA-GLOBAL-ID": "EBAY-IT"}

        if "Completed" in operation:
            params[f"itemFilter({filter_idx}).name"] = "SoldItemsOnly"
            params[f"itemFilter({filter_idx}).value"] = "true"
            filter_idx += 1
        else:
            params[f"itemFilter({filter_idx}).name"] = "ListingType"
            params[f"itemFilter({filter_idx}).value(0)"] = "FixedPrice"
            params[f"itemFilter({filter_idx}).value(1)"] = "AuctionWithBIN"
            filter_idx += 1

        condition_id = _CONDITION_MAP.get(condition)
        if condition_id:
            params[f"itemFilter({filter_idx}).name"] = "Condition"
            params[f"itemFilter({filter_idx}).value"] = condition_id
            filter_idx += 1

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    _FINDING_URL,
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json(content_type=None)
        except Exception:
            logger.debug("Errore API %s per '%s'", operation, query, exc_info=True)
            return []

        try:
            response = data.get(response_key, [{}])[0]
            if response.get("ack", [None])[0] != "Success":
                return []
            results = response.get("searchResult", [{}])[0]
            if int(results.get("@count", "0")) == 0:
                return []
            items = results.get("item", [])
        except (KeyError, IndexError, TypeError):
            return []

        prices: list[float] = []
        for item in items:
            try:
                selling_status = item.get("sellingStatus", [{}])[0]
                current_price = selling_status.get("currentPrice", [{}])[0]
                price_value = current_price.get("__value__", "")
                if not price_value:
                    continue
                price = float(price_value)
                if price > 0:
                    prices.append(price)
            except (ValueError, TypeError, KeyError, IndexError):
                continue

        return prices


# ------------------------------------------------------------------ #
#  Funzioni di parsing HTML: CSS, JSON-LD, JSON embedded               #
# ------------------------------------------------------------------ #


def _parse_css_selectors(soup: BeautifulSoup) -> list[float]:
    """Estrae prezzi tramite selettori CSS.

    Supporta sia la vecchia struttura (s-item) che la nuova (eBay Marko.js).
    """
    # --- Tentativo 1: vecchi selettori s-item ---
    items = soup.select("li.s-item")
    if items:
        return _extract_prices_from_items(items)

    # --- Tentativo 2: nuova struttura eBay ---
    # eBay ora usa <ul> dentro srp-results con <li> senza classe s-item
    srp = soup.select_one("[class*='srp-results']")
    if not srp:
        srp = soup.select_one("[id*='srp-results']")
    if not srp:
        # Cerca per attributo data
        srp = soup.find(attrs={"data-view": re.compile(r"results", re.I)})

    if srp:
        # Prendi tutti i <li> diretti che contengono un link
        items = [li for li in srp.find_all("li", recursive=False) if li.find("a")]
        if not items:
            # Prova anche <li> nested
            items = [li for li in srp.find_all("li") if li.find("a", href=re.compile(r"/itm/"))]
        if items:
            return _extract_prices_from_items(items)

    # --- Tentativo 3: cerca tutti i link /itm/ e risali al parent ---
    itm_links = soup.find_all("a", href=re.compile(r"ebay\.it/itm/"))
    if len(itm_links) >= 5:
        prices: list[float] = []
        for link in itm_links:
            # Cerca prezzo nel parent del link
            parent = link.parent
            if parent is None:
                continue
            for span in parent.find_all("span"):
                text = span.get_text(strip=True)
                if re.search(r"EUR|€", text) and re.search(r"\d+,\d{2}", text):
                    price = _parse_ebay_it_price(text)
                    if price and price > 5:
                        prices.append(price)
                        break
        if prices:
            return prices

    return []


def _extract_prices_from_items(items: list) -> list[float]:
    """Estrae prezzi da una lista di elementi item (vecchio o nuovo formato)."""
    price_selectors = [
        ".s-item__price",
        ".s-item__detail--price",
        "[class*='s-item__price']",
        "[class*='price']",
        "span.BOLD",
    ]

    prices: list[float] = []
    for item in items:
        price_el = None
        for sel in price_selectors:
            price_el = item.select_one(sel)
            if price_el:
                break
        if not price_el:
            # Fallback: cerca qualsiasi span con prezzo EUR/€ e decimali
            for span in item.find_all("span"):
                text = span.get_text(strip=True)
                if re.search(r"EUR|€", text) and re.search(r"\d+,\d{2}", text):
                    price_el = span
                    break
        if not price_el:
            continue
        price_text = price_el.get_text(strip=True)
        price = _parse_ebay_it_price(price_text)
        if price and price > 5:
            prices.append(price)

    return prices


def _parse_json_ld(soup: BeautifulSoup) -> list[float]:
    """Estrae prezzi dai tag <script type='application/ld+json'> (Schema.org).

    eBay include spesso dati strutturati JSON-LD per SEO.
    Strutture supportate:
    - ItemList con itemListElement[].offers.price
    - Product con offers.price / offers[].price
    - SearchResultsPage con mainEntity.itemListElement[]
    """
    prices: list[float] = []

    for script_tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script_tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(data, dict):
            # Puo' essere una lista di oggetti JSON-LD
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        prices.extend(_extract_prices_from_ld(item))
            continue

        prices.extend(_extract_prices_from_ld(data))

    return prices


def _extract_prices_from_ld(data: dict) -> list[float]:
    """Estrae prezzi da un singolo oggetto JSON-LD."""
    prices: list[float] = []
    ld_type = data.get("@type", "")

    # ItemList: lista di prodotti nella pagina di ricerca
    if ld_type == "ItemList":
        for element in data.get("itemListElement", []):
            if isinstance(element, dict):
                price = _get_price_from_offer(element)
                if price:
                    prices.append(price)
                # Nested: element.item.offers
                item = element.get("item", {})
                if isinstance(item, dict):
                    price = _get_price_from_offer(item)
                    if price:
                        prices.append(price)

    # Product: singolo prodotto con offerte
    elif ld_type == "Product":
        price = _get_price_from_offer(data)
        if price:
            prices.append(price)

    # SearchResultsPage: pagina di risultati
    elif ld_type == "SearchResultsPage":
        main_entity = data.get("mainEntity", {})
        if isinstance(main_entity, dict):
            for element in main_entity.get("itemListElement", []):
                if isinstance(element, dict):
                    price = _get_price_from_offer(element)
                    if price:
                        prices.append(price)
                    item = element.get("item", {})
                    if isinstance(item, dict):
                        price = _get_price_from_offer(item)
                        if price:
                            prices.append(price)

    # CollectionPage o generico con mainEntity
    elif "mainEntity" in data:
        main = data["mainEntity"]
        if isinstance(main, dict):
            prices.extend(_extract_prices_from_ld(main))
        elif isinstance(main, list):
            for item in main:
                if isinstance(item, dict):
                    prices.extend(_extract_prices_from_ld(item))

    return prices


def _get_price_from_offer(data: dict) -> Optional[float]:
    """Estrae un prezzo dal campo 'offers' di un oggetto JSON-LD."""
    offers = data.get("offers", data.get("offer", {}))

    if isinstance(offers, dict):
        return _parse_ld_price(offers.get("price") or offers.get("lowPrice"))

    if isinstance(offers, list):
        # Prende il primo prezzo valido
        for offer in offers:
            if isinstance(offer, dict):
                price = _parse_ld_price(offer.get("price") or offer.get("lowPrice"))
                if price:
                    return price
                # Nested: offer.itemOffered[].offers[].price
                item_offered = offer.get("itemOffered", [])
                if isinstance(item_offered, list):
                    for sub in item_offered:
                        if isinstance(sub, dict):
                            sub_price = _get_price_from_offer(sub)
                            if sub_price:
                                return sub_price

    # Prezzo diretto sull'oggetto
    return _parse_ld_price(data.get("price"))


def _parse_ld_price(value) -> Optional[float]:
    """Converte un valore prezzo JSON-LD in float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        return _parse_ebay_it_price(value)
    return None


def _parse_embedded_json(html: str) -> list[float]:
    """Cerca dati di prezzo in JSON embedded nei tag <script> della pagina.

    eBay usa Marko.js e include dati in vari formati:
    - var X = {...};
    - window.__data = {...};
    - JSON diretto in script tag
    - Array di oggetti con prezzi
    """
    prices: list[float] = []

    for match in re.finditer(
        r'<script[^>]*>(.*?)</script>', html, re.DOTALL
    ):
        text = match.group(1).strip()
        if len(text) < 50:
            continue
        # Cerca solo script che contengono indicatori di prezzo
        if '"price"' not in text and '"prc"' not in text:
            continue

        # Strategia 1: assegnazione JS (var X = {...}; o window.X = {...};)
        for json_match in re.finditer(r'=\s*(\{.+?\})\s*;', text, re.DOTALL):
            try:
                data = json.loads(json_match.group(1))
                found = _dig_prices(data, depth=0)
                if found:
                    prices.extend(found)
                    return prices
            except (json.JSONDecodeError, RecursionError):
                continue

        # Strategia 2: JSON diretto nel tag
        if text.lstrip().startswith("{"):
            try:
                data = json.loads(text.strip().rstrip(";"))
                found = _dig_prices(data, depth=0)
                if found:
                    prices.extend(found)
                    return prices
            except (json.JSONDecodeError, RecursionError):
                pass

        # Strategia 3: cerca sottostringhe JSON con "price"
        for sub_match in re.finditer(
            r'\{[^{}]*"price"\s*:\s*["\d][^{}]*\}', text
        ):
            try:
                obj = json.loads(sub_match.group(0))
                price = _parse_ld_price(obj.get("price"))
                if price and price > 0:
                    prices.append(price)
            except (json.JSONDecodeError, TypeError):
                continue

        if prices:
            return prices

    return prices


def _dig_prices(data, depth: int) -> list[float]:
    """Cerca ricorsivamente campi prezzo in un dizionario JSON."""
    if depth > 6:
        return []
    prices: list[float] = []

    if isinstance(data, dict):
        # Campi prezzo comuni
        for key in ("price", "prc", "currentPrice", "salePrice", "binPrice"):
            val = data.get(key)
            if val is not None:
                parsed = _parse_ld_price(val)
                if parsed and parsed > 0:
                    prices.append(parsed)
        # Recurse
        for v in data.values():
            if isinstance(v, (dict, list)):
                prices.extend(_dig_prices(v, depth + 1))
    elif isinstance(data, list):
        for item in data[:100]:  # Limita per performance
            if isinstance(item, (dict, list)):
                prices.extend(_dig_prices(item, depth + 1))

    return prices


def _parse_regex_fallback(html: str) -> list[float]:
    """Fallback: cerca prezzi direttamente nell'HTML con regex.

    Usato quando i selettori CSS e JSON non trovano nulla (es. eBay
    ha cambiato le classi CSS e non usa JSON-LD).
    Cerca pattern 'EUR 350,00' / '350,00 EUR' / '€ 350,00' con decimali,
    filtrando rumore (prezzi da filtri sidebar, spedizione, ecc.).
    """
    prices: list[float] = []
    seen: set[float] = set()

    # Richiedi almeno la virgola decimale per evitare numeri da filtri/contatori
    # Es. "EUR 227,74" si', "EUR1000" no (probabile filtro sidebar)
    price_pattern = re.compile(
        r'(?:EUR\s+|€\s*)(\d{1,3}(?:\.\d{3})*,\d{2})\b|'
        r'\b(\d{1,3}(?:\.\d{3})*,\d{2})\s*(?:EUR|€)',
    )

    # Cerca solo nella sezione risultati di ricerca
    results_section = html
    for marker in ("srp-results", "srp-river", "ListViewInner"):
        idx = html.find(marker)
        if idx != -1:
            # Prendi fino alla fine della sezione (approssimativo)
            end_idx = html.find("</ul>", idx + 50000)
            if end_idx == -1:
                end_idx = min(idx + 500000, len(html))
            results_section = html[idx:end_idx]
            break

    for m in price_pattern.finditer(results_section):
        price_str = m.group(1) or m.group(2)
        if not price_str:
            continue
        price = _parse_ebay_it_price(price_str)
        if price and 5 < price < 50000 and price not in seen:
            seen.add(price)
            prices.append(price)

    return prices


def _parse_ebay_it_price(text: str) -> Optional[float]:
    """Converte un prezzo eBay.it ('EUR 1.234,56' o '650,00 EUR') in float.

    Gestisce il formato italiano: punto come separatore migliaia,
    virgola come separatore decimale.
    """
    if " a " in text.lower():
        text = text.lower().split(" a ")[0]

    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")

    try:
        return float(cleaned)
    except ValueError:
        return None
