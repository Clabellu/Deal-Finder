import os
import re
import statistics
from dataclasses import dataclass
from typing import Optional

import aiohttp
from dotenv import load_dotenv

from utils.logger import get_logger

load_dotenv()

logger = get_logger("price_checker")

_FINDING_URL = "https://svcs.ebay.com/services/search/FindingService/v1"
_REQUEST_TIMEOUT = 15
_MAX_RETRIES = 3
_ITEMS_PER_PAGE = 100


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
    ):
        self._sold_items_count = sold_items_count
        self._use_median = use_median
        self._max_days_sold = max_days_sold
        self._app_id = os.environ.get("EBAY_APP_ID", "")
        if not self._app_id:
            logger.warning("EBAY_APP_ID non impostato: il price checker non funzionera'")

    async def check_price(
        self,
        search_query: str,
        condition: str = "",
    ) -> Optional[PriceResult]:
        """Cerca prezzi di vendita su eBay per un dato prodotto.

        Usa l'operazione findCompletedItems della Finding API per ottenere
        i prezzi degli articoli venduti, senza scraping HTML.

        Args:
            search_query: Query di ricerca ottimizzata (dal LLM parser).
            condition: Condizione del prodotto per filtrare i risultati.

        Returns:
            PriceResult con statistiche di prezzo, o None se la ricerca fallisce.
        """
        if not self._app_id:
            logger.warning("EBAY_APP_ID mancante, impossibile controllare prezzi")
            return None

        clean_query = self._clean_query(search_query)

        # Strategia di fallback: query pulita con condizione -> senza condizione -> query ridotta
        attempts = [(clean_query, condition)]
        if condition:
            attempts.append((clean_query, ""))
        short_query = self._shorten_query(clean_query)
        if short_query != clean_query:
            attempts.append((short_query, ""))

        prices: list[float] = []
        used_query = clean_query
        for query, cond in attempts:
            prices = await self._fetch_completed_items(query, cond)
            if prices:
                used_query = query
                break
            logger.debug("Nessun risultato per '%s' (condizione='%s'), provo fallback", query, cond)

        if not prices:
            logger.info("Nessun venduto trovato su eBay per: %s", search_query)
            return None

        # Filtra outlier (prezzi sotto il 10% della mediana)
        raw_median = statistics.median(prices)
        threshold = raw_median * 0.10
        filtered = [p for p in prices if p >= threshold]

        if not filtered:
            filtered = prices

        median_price = statistics.median(filtered)
        mean_price = statistics.mean(filtered)

        result = PriceResult(
            median_price=round(median_price, 2),
            mean_price=round(mean_price, 2),
            min_price=round(min(filtered), 2),
            max_price=round(max(filtered), 2),
            sold_count=len(filtered),
            reliable=len(filtered) >= 5,
        )

        logger.info(
            "eBay prezzo per '%s': mediana=%.2f, media=%.2f, venduti=%d, affidabile=%s",
            used_query,
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
        """Riduce la query alle prime 3 parole (marca + modello)."""
        words = query.split()
        if len(words) <= 3:
            return query
        return " ".join(words[:3])

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
                        return []
                    data = await resp.json(content_type=None)
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
