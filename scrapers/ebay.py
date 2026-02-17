import os
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from scrapers.base_scraper import BaseScraper, Listing
from utils.logger import get_logger

logger = get_logger("ebay")

_FINDING_URL = "https://svcs.ebay.com/services/search/FindingService/v1"

# Paginazione e rate-limit
_MAX_PAGES = 3
_ITEMS_PER_PAGE = 50
_REQUEST_TIMEOUT = 15


class EbayScraper(BaseScraper):
    """Scraper per eBay tramite Finding API (findItemsByKeywords)."""

    def __init__(self):
        self._app_id = os.environ.get("EBAY_APP_ID", "")
        if not self._app_id:
            logger.warning("EBAY_APP_ID non impostato: lo scraper eBay non funzionera'")

    @property
    def platform_name(self) -> str:
        return "ebay"

    async def search(
        self,
        keyword: str,
        min_price: float,
        max_price: float,
        category_name: str,
    ) -> list[Listing]:
        if not self._app_id:
            logger.warning("EBAY_APP_ID mancante, impossibile cercare su eBay")
            return []

        all_listings: list[Listing] = []

        async with aiohttp.ClientSession() as session:
            for page in range(1, _MAX_PAGES + 1):
                items = await self._fetch_page(
                    session, keyword, min_price, max_price, page
                )
                if not items:
                    break

                for item in items:
                    listing = self._parse_item(item, category_name)
                    if listing:
                        all_listings.append(listing)

                logger.debug(
                    "eBay pagina %d per '%s': %d items", page, keyword, len(items)
                )

                # Se meno risultati del massimo, non ci sono altre pagine
                if len(items) < _ITEMS_PER_PAGE:
                    break

        logger.info(
            "eBay: trovate %d inserzioni per '%s' [%s]",
            len(all_listings), keyword, category_name,
        )
        return all_listings

    async def _fetch_page(
        self,
        session: aiohttp.ClientSession,
        keyword: str,
        min_price: float,
        max_price: float,
        page: int,
    ) -> list[dict]:
        """Chiama eBay Finding API per una singola pagina di risultati."""
        filter_idx = 0
        params: dict[str, str] = {
            "OPERATION-NAME": "findItemsByKeywords",
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": self._app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "",
            "keywords": keyword,
            "paginationInput.entriesPerPage": str(_ITEMS_PER_PAGE),
            "paginationInput.pageNumber": str(page),
            "sortOrder": "StartTimeNewest",
        }

        # Header per eBay Italia
        headers = {"X-EBAY-SOA-GLOBAL-ID": "EBAY-IT"}

        # Filtro prezzo minimo
        params[f"itemFilter({filter_idx}).name"] = "MinPrice"
        params[f"itemFilter({filter_idx}).value"] = str(int(min_price))
        params[f"itemFilter({filter_idx}).paramName"] = "Currency"
        params[f"itemFilter({filter_idx}).paramValue"] = "EUR"
        filter_idx += 1

        # Filtro prezzo massimo
        params[f"itemFilter({filter_idx}).name"] = "MaxPrice"
        params[f"itemFilter({filter_idx}).value"] = str(int(max_price))
        params[f"itemFilter({filter_idx}).paramName"] = "Currency"
        params[f"itemFilter({filter_idx}).paramValue"] = "EUR"
        filter_idx += 1

        # Solo inserzioni attive (Compralo Subito + Asta con BIN)
        params[f"itemFilter({filter_idx}).name"] = "ListingType"
        params[f"itemFilter({filter_idx}).value(0)"] = "FixedPrice"
        params[f"itemFilter({filter_idx}).value(1)"] = "AuctionWithBIN"
        filter_idx += 1

        try:
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
                data = await resp.json()
        except Exception:
            logger.exception("Errore chiamata eBay Finding API")
            return []

        return self._extract_items(data)

    @staticmethod
    def _extract_items(data: dict) -> list[dict]:
        """Estrae la lista di items dalla risposta JSON della Finding API."""
        try:
            response = data.get("findItemsByKeywordsResponse", [{}])[0]
            ack = response.get("ack", [None])[0]
            if ack != "Success":
                error_msg = ""
                errors = response.get("errorMessage", [{}])[0].get("error", [])
                if errors:
                    error_msg = errors[0].get("message", [""])[0]
                logger.warning("eBay API ack=%s: %s", ack, error_msg)
                return []

            results = response.get("searchResult", [{}])[0]
            count = int(results.get("@count", "0"))
            if count == 0:
                return []
            return results.get("item", [])
        except (KeyError, IndexError, TypeError):
            logger.debug("Errore parsing risposta eBay", exc_info=True)
            return []

    @staticmethod
    def _parse_item(item: dict, category_name: str) -> Optional[Listing]:
        """Converte un item della Finding API in un Listing."""
        try:
            # ID
            item_id = item.get("itemId", [None])[0]
            if not item_id:
                return None

            # Titolo
            title = item.get("title", [""])[0].strip()
            if not title:
                return None

            # Prezzo
            selling_status = item.get("sellingStatus", [{}])[0]
            current_price = selling_status.get("currentPrice", [{}])[0]
            price_value = current_price.get("__value__", "")
            if not price_value:
                return None
            try:
                price = float(price_value)
            except (ValueError, TypeError):
                return None

            # URL
            url = item.get("viewItemURL", [""])[0]
            if not url:
                return None

            # Immagine
            image_url = item.get("galleryURL", [None])[0]

            # Localita'
            location_str = item.get("location", [None])[0]
            country = item.get("country", [""])[0]
            if location_str and country:
                location = f"{location_str}, {country}"
            elif location_str:
                location = location_str
            else:
                location = None

            # Timestamp
            list_time = item.get("listingInfo", [{}])[0].get("startTime", [None])[0]
            timestamp = list_time if list_time else datetime.now(timezone.utc).isoformat()

            # Condizione
            condition_info = item.get("condition", [{}])[0]
            condition_name = condition_info.get("conditionDisplayName", [""])[0] if condition_info else ""

            # Descrizione sintetica con condizione e tipo inserzione
            listing_type = item.get("listingInfo", [{}])[0].get("listingType", [""])[0]
            desc_parts = []
            if condition_name:
                desc_parts.append(condition_name)
            if listing_type:
                desc_parts.append(listing_type)
            description = " | ".join(desc_parts)

            return Listing(
                id=item_id,
                platform="ebay",
                title=title,
                description=description,
                price=price,
                currency="EUR",
                url=url,
                image_url=image_url,
                location=location,
                category_matched=category_name,
                timestamp=timestamp,
            )
        except Exception:
            logger.debug("Errore parsing item eBay", exc_info=True)
            return None
