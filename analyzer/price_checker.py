import asyncio
import os
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
_REQUEST_TIMEOUT = 15
_MAX_RETRIES = 3
_ITEMS_PER_PAGE = 100
_DEFAULT_CACHE_TTL = 86400  # 24 ore (configurabile via config.yaml)
_API_DELAY = 2.0        # secondi tra chiamate API
_BACKOFF_BASE = 5       # secondi di backoff iniziale su rate limit
_BACKOFF_MAX = 120      # secondi massimi di backoff
_RATE_LIMIT_RETRIES = 3 # tentativi su rate limit prima di arrendersi

_EBAY_SOLD_URL = "https://www.ebay.it/sch/i.html"


class _RateLimitError(Exception):
    """Raised when eBay returns a rate limit error (HTTP 500, errorId 10001)."""


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
    """Cerca il prezzo medio di mercato tramite eBay Finding API (findCompletedItems)."""

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
        if not self._app_id:
            logger.warning("EBAY_APP_ID non impostato: il price checker non funzionera'")

        self._db = db  # Database instance per cache persistente
        # Cache in-memory: chiave = (query_normalizzata, condizione) -> (timestamp, list[float])
        self._cache: dict[tuple[str, str], tuple[float, list[float]]] = {}
        self._last_api_call: float = 0
        # Flag globale: se True, skippa l'API e va diretto allo scraping
        self._api_rate_limited = False
        self._rate_limit_until: float = 0

    async def check_price(
        self,
        search_query: str,
        condition: str = "",
    ) -> Optional[PriceResult]:
        """Cerca prezzi di vendita su eBay per un dato prodotto.

        Strategia multi-livello:
        1. Venduti completati (API findCompletedItems -> scraping sold)
        2. Inserzioni attive come fallback (API findItemsByKeywords -> scraping attive)

        Args:
            search_query: Query di ricerca ottimizzata (dal LLM parser).
            condition: Condizione del prodotto per filtrare i risultati.

        Returns:
            PriceResult con statistiche di prezzo, o None se la ricerca fallisce.
        """

        clean_query = self._clean_query(search_query)

        # --- Fase 1: cerco venduti completati ---
        attempts = [(clean_query, condition)]
        if condition:
            attempts.append((clean_query, ""))
        short_query = self._shorten_query(clean_query)
        if short_query != clean_query:
            attempts.append((short_query, ""))

        prices: list[float] = []
        used_query = clean_query
        from_active = False
        for query, cond in attempts:
            result = await self._get_prices_cached(query, cond)
            if result is None:
                break
            if result:
                prices = result
                used_query = query
                break
            logger.debug("Nessun venduto per '%s' (condizione='%s'), provo fallback", query, cond)

        # --- Fase 2: fallback a inserzioni attive ---
        if not prices:
            logger.info(
                "Nessun venduto su eBay per '%s', provo inserzioni attive", search_query
            )
            active_attempts = [(clean_query, condition)]
            if condition:
                active_attempts.append((clean_query, ""))
            if short_query != clean_query:
                active_attempts.append((short_query, ""))

            for query, cond in active_attempts:
                result = await self._get_active_prices(query, cond)
                if result is None:
                    break
                if result:
                    prices = result
                    used_query = query
                    from_active = True
                    logger.info(
                        "Trovati %d prezzi da inserzioni attive per '%s'",
                        len(prices), query,
                    )
                    break

        if not prices:
            logger.warning("Nessun prezzo trovato (venduti + attivi) su eBay per: %s", search_query)
            return None

        # Filtra outlier (prezzi sotto il 10% della mediana)
        raw_median = statistics.median(prices)
        threshold = raw_median * 0.10
        filtered = [p for p in prices if p >= threshold]

        if not filtered:
            filtered = prices

        median_price = statistics.median(filtered)
        mean_price = statistics.mean(filtered)

        # Prezzi da inserzioni attive sono meno affidabili: servono piu' campioni
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

        # Rimuovi parole che sembrano varianti (storage, colori, etc.)
        variant_patterns = re.compile(
            r"^\d+\s*(?:GB|TB|MB|G)$|^\d+$|^(?:nero|bianco|grigio|blu|rosso|verde|viola|"
            r"oro|argento|titanio|black|white|grey|gray|blue|red|green|gold|silver|purple|"
            r"titanium|natural|pink|rosa|midnight|starlight|space)$",
            re.IGNORECASE,
        )
        core_words = [w for w in words if not variant_patterns.match(w)]

        if core_words and len(core_words) < len(words):
            return " ".join(core_words)

        # Fallback: prime 4 parole (gestisce "iPhone 17 Pro Max")
        return " ".join(words[:4]) if len(words) > 4 else query

    async def _get_prices_cached(self, query: str, condition: str) -> Optional[list[float]]:
        """Restituisce prezzi dalla cache se validi, altrimenti chiama l'API.

        Ordine: cache in-memory -> cache DB -> API eBay -> scraping fallback.

        Returns:
            list[float] con prezzi (puo' essere vuota se nessun venduto),
            oppure None se tutti i metodi falliscono.
        """
        cache_key = (query.lower().strip(), condition)
        now = time.monotonic()

        # 1. Cache in-memory
        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, prices = cached
            if now - ts < self._cache_ttl:
                logger.debug("Cache in-memory hit per '%s' (%d prezzi)", query, len(prices))
                return prices

        # 2. Cache DB persistente
        if self._db is not None:
            try:
                db_prices = await self._db.get_cached_prices(query, condition, self._cache_ttl)
                if db_prices is not None:
                    logger.debug("Cache DB hit per '%s' (%d prezzi)", query, len(db_prices))
                    self._cache[cache_key] = (time.monotonic(), db_prices)
                    return db_prices
            except Exception:
                logger.debug("Errore lettura cache DB per '%s'", query, exc_info=True)

        # 3. Se l'API e' in rate limit globale, salta direttamente allo scraping
        if self._api_rate_limited and time.monotonic() < self._rate_limit_until:
            logger.debug("API eBay in rate limit globale, uso scraping per '%s'", query)
            return await self._scrape_and_cache(query, condition, cache_key)

        # Reset del flag se il tempo e' scaduto
        if self._api_rate_limited and time.monotonic() >= self._rate_limit_until:
            self._api_rate_limited = False
            logger.info("Rate limit eBay globale scaduto, riprovo API")

        # 4. Chiamata API eBay (solo se abbiamo l'APP_ID)
        if self._app_id:
            for attempt in range(_RATE_LIMIT_RETRIES):
                elapsed = time.monotonic() - self._last_api_call
                if elapsed < _API_DELAY:
                    await asyncio.sleep(_API_DELAY - elapsed)

                try:
                    prices = await self._fetch_completed_items(query, condition)
                except _RateLimitError:
                    backoff = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                    logger.warning(
                        "Rate limit eBay API, backoff %ds (tentativo %d/%d)",
                        backoff, attempt + 1, _RATE_LIMIT_RETRIES,
                    )
                    self._last_api_call = time.monotonic()
                    await asyncio.sleep(backoff)
                    continue

                self._last_api_call = time.monotonic()
                self._cache[cache_key] = (time.monotonic(), prices)

                # Salva nel DB per persistenza tra riavvii
                if self._db is not None:
                    try:
                        await self._db.save_cached_prices(query, condition, prices)
                    except Exception:
                        logger.debug("Errore scrittura cache DB per '%s'", query, exc_info=True)

                return prices

            # API rate-limited: attiva flag globale (evita di bruciare tentativi)
            # e passa allo scraping per questa query e le prossime 10 minuti
            self._api_rate_limited = True
            self._rate_limit_until = time.monotonic() + 600  # 10 minuti
            logger.warning(
                "Rate limit eBay API persistente, passo a scraping per i prossimi 10 min"
            )

        # 5. Fallback: scraping eBay.it
        return await self._scrape_and_cache(query, condition, cache_key)

    async def _scrape_and_cache(
        self, query: str, condition: str, cache_key: tuple[str, str]
    ) -> Optional[list[float]]:
        """Scraping fallback + salvataggio in cache."""
        prices = await self._scrape_ebay_sold(query, condition)
        if prices:
            self._cache[cache_key] = (time.monotonic(), prices)
            if self._db is not None:
                try:
                    await self._db.save_cached_prices(query, condition, prices)
                except Exception:
                    logger.debug("Errore scrittura cache DB (scrape) per '%s'", query, exc_info=True)
            return prices

        logger.warning("Nessun prezzo trovato (API + scraping) per '%s'", query)
        return []

    # ------------------------------------------------------------------ #
    #  Inserzioni attive: fallback quando non ci sono venduti             #
    # ------------------------------------------------------------------ #

    async def _get_active_prices(self, query: str, condition: str) -> Optional[list[float]]:
        """Cerca prezzi da inserzioni attive su eBay (non vendute).

        Usa findItemsByKeywords API, con fallback a scraping attivi.

        Returns:
            list[float] con prezzi (puo' essere vuota), None se errore.
        """
        cache_key = (f"active:{query.lower().strip()}", condition)
        now = time.monotonic()

        # Cache in-memory
        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, prices = cached
            if now - ts < self._cache_ttl:
                return prices

        # Cache DB
        if self._db is not None:
            try:
                db_prices = await self._db.get_cached_prices(
                    f"active:{query}", condition, self._cache_ttl
                )
                if db_prices is not None:
                    self._cache[cache_key] = (time.monotonic(), db_prices)
                    return db_prices
            except Exception:
                pass

        # API (se non rate-limited)
        prices: list[float] = []
        if self._app_id and not (self._api_rate_limited and time.monotonic() < self._rate_limit_until):
            elapsed = time.monotonic() - self._last_api_call
            if elapsed < _API_DELAY:
                await asyncio.sleep(_API_DELAY - elapsed)
            try:
                prices = await self._fetch_active_items(query, condition)
            except _RateLimitError:
                logger.debug("Rate limit su findItemsByKeywords per '%s'", query)
            except Exception:
                logger.debug("Errore findItemsByKeywords per '%s'", query, exc_info=True)
            self._last_api_call = time.monotonic()

        # Scraping fallback inserzioni attive
        if not prices:
            prices = await self._scrape_ebay_active(query, condition)

        # Salva in cache
        self._cache[cache_key] = (time.monotonic(), prices)
        if self._db is not None and prices:
            try:
                await self._db.save_cached_prices(f"active:{query}", condition, prices)
            except Exception:
                pass

        return prices

    async def _fetch_active_items(self, query: str, condition: str) -> list[float]:
        """Chiama eBay Finding API findItemsByKeywords per inserzioni attive."""
        filter_idx = 0
        params: dict[str, str] = {
            "OPERATION-NAME": "findItemsByKeywords",
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": self._app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "",
            "keywords": query,
            "paginationInput.entriesPerPage": str(min(60, self._sold_items_count * 3)),
            "paginationInput.pageNumber": "1",
            "sortOrder": "BestMatch",
        }
        headers = {"X-EBAY-SOA-GLOBAL-ID": "EBAY-IT"}

        # Solo Compralo Subito (no aste, prezzi piu' stabili)
        params[f"itemFilter({filter_idx}).name"] = "ListingType"
        params[f"itemFilter({filter_idx}).value(0)"] = "FixedPrice"
        params[f"itemFilter({filter_idx}).value(1)"] = "AuctionWithBIN"
        filter_idx += 1

        condition_id = {
            "nuovo": "1000",
            "come_nuovo": "1500",
            "usato_buono": "3000",
            "usato_discreto": "3000",
        }.get(condition)
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
                    timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        if resp.status == 500 and "RateLimiter" in body:
                            raise _RateLimitError(body[:300])
                        return []
                    data = await resp.json(content_type=None)
        except _RateLimitError:
            raise
        except Exception:
            logger.debug("Errore findItemsByKeywords per '%s'", query, exc_info=True)
            return []

        # La struttura e' uguale ma con chiave diversa
        try:
            response = data.get("findItemsByKeywordsResponse", [{}])[0]
            ack = response.get("ack", [None])[0]
            if ack != "Success":
                return []
            results = response.get("searchResult", [{}])[0]
            count = int(results.get("@count", "0"))
            if count == 0:
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

        logger.debug("eBay API findItemsByKeywords '%s': %d prezzi attivi", query, len(prices))
        return prices

    async def _scrape_ebay_active(self, query: str, condition: str) -> list[float]:
        """Scraping eBay.it per inserzioni attive (non vendute)."""
        params: dict[str, str] = {
            "_nkw": query,
            "LH_BIN": "1",        # Solo Compralo Subito
            "_sop": "12",          # Sort: best match
            "rt": "nc",
            "_ipg": "60",
        }

        condition_map = {
            "nuovo": "1000",
            "come_nuovo": "1500",
            "usato_buono": "3000",
            "usato_discreto": "3000",
        }
        cond_id = condition_map.get(condition)
        if cond_id:
            params["LH_ItemCondition"] = cond_id

        try:
            async with AsyncSession(impersonate="chrome") as session:
                resp = await session.get(
                    _EBAY_SOLD_URL,
                    params=params,
                    timeout=_REQUEST_TIMEOUT,
                )
                if resp.status_code != 200:
                    logger.warning("eBay scrape attive HTTP %d per '%s'", resp.status_code, query)
                    return []
                html = resp.text
        except Exception:
            logger.warning("Errore scraping eBay.it attive per '%s'", query, exc_info=True)
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

    async def _fetch_completed_items(
        self,
        query: str,
        condition: str,
    ) -> list[float]:
        """Chiama eBay Finding API findCompletedItems per ottenere prezzi venduti."""
        filter_idx = 0
        params: dict[str, str] = {
            "OPERATION-NAME": "findCompletedItems",
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": self._app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "",
            "keywords": query,
            "paginationInput.entriesPerPage": str(min(_ITEMS_PER_PAGE, self._sold_items_count * 3)),
            "paginationInput.pageNumber": "1",
            "sortOrder": "EndTimeSoonest",
        }

        headers = {"X-EBAY-SOA-GLOBAL-ID": "EBAY-IT"}

        # Solo venduti (non quelli completati senza vendita)
        params[f"itemFilter({filter_idx}).name"] = "SoldItemsOnly"
        params[f"itemFilter({filter_idx}).value"] = "true"
        filter_idx += 1

        # Mappa condizione a filtro eBay
        condition_id = {
            "nuovo": "1000",
            "come_nuovo": "1500",
            "usato_buono": "3000",
            "usato_discreto": "3000",
        }.get(condition)
        if condition_id:
            params[f"itemFilter({filter_idx}).name"] = "Condition"
            params[f"itemFilter({filter_idx}).value"] = condition_id
            filter_idx += 1

        prices: list[float] = []
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    _FINDING_URL,
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("eBay Finding API HTTP %d: %s", resp.status, body[:300])
                        if resp.status == 500 and "RateLimiter" in body:
                            raise _RateLimitError(body[:300])
                        return []
                    data = await resp.json(content_type=None)
        except _RateLimitError:
            raise
        except Exception:
            logger.exception("Errore chiamata eBay Finding API (findCompletedItems)")
            return []

        prices = self._extract_prices(data)
        logger.debug("eBay API findCompletedItems '%s': %d prezzi estratti", query, len(prices))
        return prices

    @staticmethod
    def _extract_prices(data: dict) -> list[float]:
        """Estrae i prezzi di vendita dalla risposta JSON di findCompletedItems."""
        try:
            response = data.get("findCompletedItemsResponse", [{}])[0]
            ack = response.get("ack", [None])[0]
            if ack != "Success":
                error_msg = ""
                errors = response.get("errorMessage", [{}])[0].get("error", [])
                if errors:
                    error_msg = errors[0].get("message", [""])[0]
                logger.warning("eBay API findCompletedItems ack=%s: %s", ack, error_msg)
                return []

            results = response.get("searchResult", [{}])[0]
            count = int(results.get("@count", "0"))
            if count == 0:
                return []

            items = results.get("item", [])
        except (KeyError, IndexError, TypeError):
            logger.debug("Errore parsing risposta findCompletedItems", exc_info=True)
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
    #  Scraping fallback: eBay.it pagina venduti                          #
    # ------------------------------------------------------------------ #

    async def _scrape_ebay_sold(self, query: str, condition: str) -> list[float]:
        """Fallback: scrape eBay.it sold listings quando l'API e' rate-limited."""
        params: dict[str, str] = {
            "_nkw": query,
            "LH_Complete": "1",
            "LH_Sold": "1",
            "_sop": "13",   # Sort: end date newest first
            "rt": "nc",
            "_ipg": "60",   # risultati per pagina
        }

        # Filtro condizione
        condition_map = {
            "nuovo": "1000",
            "come_nuovo": "1500",
            "usato_buono": "3000",
            "usato_discreto": "3000",
        }
        cond_id = condition_map.get(condition)
        if cond_id:
            params["LH_ItemCondition"] = cond_id

        try:
            async with AsyncSession(impersonate="chrome") as session:
                resp = await session.get(
                    _EBAY_SOLD_URL,
                    params=params,
                    timeout=_REQUEST_TIMEOUT,
                )
                if resp.status_code != 200:
                    logger.warning("eBay scrape HTTP %d per '%s'", resp.status_code, query)
                    return []
                html = resp.text
        except Exception:
            logger.warning("Errore scraping eBay.it per '%s'", query, exc_info=True)
            return []

        prices = self._parse_ebay_html(html)
        if prices:
            logger.info(
                "eBay scrape venduti per '%s': %d prezzi estratti (mediana %.2f)",
                query, len(prices), statistics.median(prices),
            )
        else:
            logger.info("eBay scrape venduti per '%s': nessun prezzo trovato", query)

        return prices

    @staticmethod
    def _parse_ebay_html(html: str) -> list[float]:
        """Estrae i prezzi dalla pagina HTML di eBay.it (venduti o attivi).

        Prova piu' selettori CSS per gestire variazioni del layout eBay.
        """
        soup = BeautifulSoup(html, "html.parser")
        prices: list[float] = []

        # Selettore primario: layout standard eBay
        items = soup.select("li.s-item")

        # Selettore alternativo se il primario non trova nulla
        if not items:
            items = soup.select("div.s-item__wrapper")
        if not items:
            items = soup.select("[data-viewport]")

        if not items:
            # Debug: logga un frammento dell'HTML per capire cosa ritorna eBay
            snippet = html[:500] if len(html) > 500 else html
            logger.debug(
                "eBay scrape: nessun item trovato nell'HTML (lunghezza=%d). Snippet: %s",
                len(html), snippet,
            )
            return []

        # Selettori prezzo multipli (eBay cambia spesso le classi)
        price_selectors = [
            ".s-item__price",
            ".s-item__detail--price",
            "[class*='s-item__price']",
        ]

        for item in items:
            price_el = None
            for sel in price_selectors:
                price_el = item.select_one(sel)
                if price_el:
                    break
            if not price_el:
                continue
            price_text = price_el.get_text(strip=True)
            price = _parse_ebay_it_price(price_text)
            if price and price > 0:
                prices.append(price)

        return prices


def _parse_ebay_it_price(text: str) -> Optional[float]:
    """Converte un prezzo eBay.it ('EUR 1.234,56' o '650,00 EUR') in float.

    Gestisce il formato italiano: punto come separatore migliaia,
    virgola come separatore decimale.
    """
    # Gestisce range di prezzo ("EUR 100,00 a EUR 200,00") -> prende il primo
    if " a " in text.lower():
        text = text.lower().split(" a ")[0]

    # Rimuove tutto tranne cifre, punti e virgole
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None

    # Formato italiano: 1.234,56 -> 1234.56
    # Se c'e' sia punto che virgola e la virgola e' dopo il punto -> formato IT
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        # Solo virgola -> separatore decimale
        cleaned = cleaned.replace(",", ".")
    # Solo punto -> gia' formato standard (o migliaia senza decimali)

    try:
        return float(cleaned)
    except ValueError:
        return None
