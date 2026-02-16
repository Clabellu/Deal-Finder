import asyncio
import random
import re
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from scrapers.base_scraper import BaseScraper, Listing
from utils.logger import get_logger

logger = get_logger("subito")

# API endpoint JSON di Subito (molto piu' affidabile dello scraping HTML)
_API_URL = "https://hades.subito.it/v1/search/items"

# Headers che simulano una richiesta XHR dal browser
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.subito.it/",
    "Origin": "https://www.subito.it",
    "X-Requested-With": "XMLHttpRequest",
}

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
_ITEMS_PER_PAGE = 30
_MAX_PAGES = 3
_MAX_RETRIES = 3
_BASE_DELAY = 2.0  # secondi base tra le pagine


class SubitoScraper(BaseScraper):
    """Scraper per Subito.it tramite API JSON interna."""

    @property
    def platform_name(self) -> str:
        return "subito"

    async def search(
        self,
        keyword: str,
        min_price: float,
        max_price: float,
        category_name: str,
    ) -> list[Listing]:
        """Cerca inserzioni su Subito.it via API JSON."""
        all_listings: list[Listing] = []

        async with aiohttp.ClientSession(
            headers=_HEADERS, timeout=_REQUEST_TIMEOUT
        ) as session:
            for page in range(_MAX_PAGES):
                try:
                    listings = await self._fetch_page(
                        session, keyword, min_price, max_price, category_name, page
                    )
                    if not listings:
                        logger.debug(
                            "Nessun risultato a pagina %d per '%s', stop paginazione",
                            page + 1,
                            keyword,
                        )
                        break
                    all_listings.extend(listings)
                    logger.debug(
                        "Pagina %d per '%s': %d inserzioni",
                        page + 1,
                        keyword,
                        len(listings),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Errore scraping pagina %d per '%s'", page + 1, keyword
                    )
                    break

                if page < _MAX_PAGES - 1:
                    # Delay randomizzato tra le pagine (2-4 secondi)
                    delay = _BASE_DELAY + random.uniform(0, 2.0)
                    await asyncio.sleep(delay)

        logger.info(
            "Subito: trovate %d inserzioni per '%s' [%s]",
            len(all_listings),
            keyword,
            category_name,
        )
        return all_listings

    async def _fetch_page(
        self,
        session: aiohttp.ClientSession,
        keyword: str,
        min_price: float,
        max_price: float,
        category_name: str,
        page: int,
    ) -> list[Listing]:
        """Scarica una pagina di risultati dall'API JSON con retry."""
        params = {
            "q": keyword,
            "t": "s",  # solo vendita
            "sort": "date",
            "order": "desc",
            "ps": str(int(min_price)),
            "pe": str(int(max_price)),
            "lim": str(_ITEMS_PER_PAGE),
            "start": str(page * _ITEMS_PER_PAGE),
        }

        for attempt in range(_MAX_RETRIES):
            try:
                async with session.get(_API_URL, params=params) as resp:
                    if resp.status == 429:
                        wait = (2 ** attempt) + random.uniform(0, 1)
                        logger.warning(
                            "Subito: rate limited (429), retry tra %.1fs", wait
                        )
                        await asyncio.sleep(wait)
                        continue

                    if resp.status == 403:
                        wait = (2 ** attempt) + random.uniform(0, 1)
                        logger.warning(
                            "Subito: forbidden (403), retry tra %.1fs", wait
                        )
                        await asyncio.sleep(wait)
                        continue

                    if resp.status != 200:
                        logger.warning(
                            "Subito: HTTP %d per query '%s'", resp.status, keyword
                        )
                        return []

                    data = await resp.json()
                    return self._parse_api_response(data, category_name)

            except aiohttp.ClientError as e:
                if attempt < _MAX_RETRIES - 1:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning("Subito: errore rete, retry tra %.1fs: %s", wait, e)
                    await asyncio.sleep(wait)
                else:
                    raise

        logger.warning("Subito: tentativi esauriti per query '%s'", keyword)
        return []

    def _parse_api_response(self, data: dict, category_name: str) -> list[Listing]:
        """Parsa la risposta JSON dell'API Subito."""
        listings: list[Listing] = []
        ads = data.get("ads", [])

        for ad in ads:
            listing = self._parse_ad(ad, category_name)
            if listing:
                listings.append(listing)

        return listings

    def _parse_ad(self, ad: dict, category_name: str) -> Optional[Listing]:
        """Converte un singolo annuncio dall'API JSON in un Listing."""
        try:
            # ID annuncio
            urn = ad.get("urn", "")
            # urn formato: "subito:ad:123456"
            listing_id = urn.split(":")[-1] if urn else ""
            if not listing_id:
                return None

            # Titolo
            title = ad.get("subject", "").strip()
            if not title:
                return None

            # Descrizione (body puo' essere assente nei risultati di ricerca)
            description = ad.get("body", "")

            # Prezzo
            price_data = ad.get("features", [])
            price = self._extract_price_from_features(price_data)
            if price is None:
                return None

            # URL annuncio
            urls = ad.get("urls", {})
            url = urls.get("default", "")
            if not url:
                return None

            # Immagine
            images = ad.get("images", [])
            image_url = None
            if images:
                # Prendi la versione "big" della prima immagine
                first_img = images[0] if isinstance(images[0], dict) else {}
                image_url = (
                    first_img.get("cdn_url")
                    or first_img.get("base_url", "")
                )
                if image_url and "{size}" in image_url:
                    image_url = image_url.replace("{size}", "big")

            # Localita'
            geo = ad.get("geo", {})
            city = geo.get("city", {}).get("value", "")
            region = geo.get("region", {}).get("value", "")
            location = f"{city}, {region}".strip(", ") if city or region else None

            # Timestamp pubblicazione
            dates = ad.get("dates", {})
            timestamp = dates.get("display", datetime.now(timezone.utc).isoformat())

            return Listing(
                id=listing_id,
                platform="subito",
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
            logger.debug("Errore parsing annuncio Subito", exc_info=True)
            return None

    @staticmethod
    def _extract_price_from_features(features: list) -> Optional[float]:
        """Estrae il prezzo dalla lista features dell'API."""
        for feature in features:
            uri = feature.get("uri", "")
            if "/price" in uri:
                values = feature.get("values", [])
                if values:
                    raw = values[0].get("value", "")
                    # Il valore puo' essere "450" oppure "450,00"
                    cleaned = re.sub(r"[^\d.,]", "", str(raw))
                    if not cleaned:
                        return None
                    cleaned = cleaned.replace(".", "").replace(",", ".")
                    try:
                        return float(cleaned)
                    except ValueError:
                        return None
        return None
