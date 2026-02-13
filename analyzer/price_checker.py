import os
import statistics
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus

import aiohttp

from utils.logger import get_logger

logger = get_logger("price_checker")

_EBAY_AUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_EBAY_FINDING_URL = "https://svcs.ebay.com/services/search/FindingService/v1"


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
    """Cerca il prezzo medio di mercato su eBay usando gli oggetti venduti."""

    def __init__(
        self,
        sold_items_count: int = 20,
        use_median: bool = True,
        max_days_sold: int = 30,
    ):
        self._app_id = os.environ.get("EBAY_APP_ID", "")
        self._cert_id = os.environ.get("EBAY_CERT_ID", "")
        self._sold_items_count = sold_items_count
        self._use_median = use_median
        self._max_days_sold = max_days_sold
        self._access_token: Optional[str] = None

        if not self._app_id:
            logger.warning("EBAY_APP_ID non impostato: il price checker non funzionera'")

    async def _get_access_token(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Ottiene un token OAuth 2.0 Client Credentials da eBay."""
        if self._access_token:
            return self._access_token

        if not self._app_id or not self._cert_id:
            return None

        auth = aiohttp.BasicAuth(self._app_id, self._cert_id)
        data = {
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            async with session.post(
                _EBAY_AUTH_URL, auth=auth, data=data, headers=headers
            ) as resp:
                if resp.status != 200:
                    logger.error("eBay OAuth fallito: HTTP %d", resp.status)
                    return None
                body = await resp.json()
                self._access_token = body.get("access_token")
                return self._access_token
        except Exception:
            logger.exception("Errore durante autenticazione eBay OAuth")
            return None

    async def check_price(
        self,
        search_query: str,
        condition: str = "",
    ) -> Optional[PriceResult]:
        """Cerca prezzi di vendita su eBay per un dato prodotto.

        Args:
            search_query: Query di ricerca ottimizzata (dal LLM parser).
            condition: Condizione del prodotto per filtrare i risultati.

        Returns:
            PriceResult con statistiche di prezzo, o None se la ricerca fallisce.
        """
        if not self._app_id:
            logger.warning("EBAY_APP_ID mancante, impossibile controllare il prezzo")
            return None

        async with aiohttp.ClientSession() as session:
            prices = await self._search_completed_items(session, search_query, condition)

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
            search_query,
            result.median_price,
            result.mean_price,
            result.sold_count,
            result.reliable,
        )
        return result

    async def _search_completed_items(
        self,
        session: aiohttp.ClientSession,
        query: str,
        condition: str,
    ) -> list[float]:
        """Usa eBay Finding API per cercare oggetti venduti."""
        params = {
            "OPERATION-NAME": "findCompletedItems",
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": self._app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "",
            "keywords": query,
            "itemFilter(0).name": "SoldItemsOnly",
            "itemFilter(0).value": "true",
            "itemFilter(1).name": "ListingType",
            "itemFilter(1).value": "FixedPrice",
            "sortOrder": "EndTimeSoonest",
            "paginationInput.entriesPerPage": str(self._sold_items_count),
        }

        # Mappa condizione dell'inserzione a filtro eBay
        condition_map = {
            "nuovo": "New",
            "come_nuovo": "1500",       # Seller refurbished
            "usato_buono": "3000",      # Used
            "usato_discreto": "3000",
        }
        ebay_condition = condition_map.get(condition)
        if ebay_condition:
            params["itemFilter(2).name"] = "Condition"
            params["itemFilter(2).value"] = ebay_condition

        try:
            async with session.get(
                _EBAY_FINDING_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    logger.warning("eBay Finding API HTTP %d", resp.status)
                    return []
                data = await resp.json()
        except Exception:
            logger.exception("Errore chiamata eBay Finding API")
            return []

        return self._extract_prices(data)

    @staticmethod
    def _extract_prices(data: dict) -> list[float]:
        """Estrae i prezzi dalla risposta JSON di eBay Finding API."""
        prices: list[float] = []
        try:
            response = data.get("findCompletedItemsResponse", [{}])[0]
            results = response.get("searchResult", [{}])[0]
            items = results.get("item", [])

            for item in items:
                selling_status = item.get("sellingStatus", [{}])[0]
                current_price = selling_status.get("currentPrice", [{}])[0]
                value = current_price.get("__value__", "")
                if value:
                    try:
                        prices.append(float(value))
                    except (ValueError, TypeError):
                        continue
        except (KeyError, IndexError, TypeError):
            logger.debug("Errore nell'estrazione prezzi dalla risposta eBay", exc_info=True)

        return prices
