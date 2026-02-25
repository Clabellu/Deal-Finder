"""Scraper per Vinted.it tramite API interna /api/v2/catalog/items."""

import asyncio
import random
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

from curl_cffi.requests import AsyncSession
from scrapers.base_scraper import BaseScraper, Listing
from utils.logger import get_logger

logger = get_logger("vinted")

_BASE_URL = "https://www.vinted.it"
_API_URL = f"{_BASE_URL}/api/v2/catalog/items"

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "DNT": "1",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

_REQUEST_TIMEOUT = 30
_MAX_PAGES = 3
_PER_PAGE = 20
_MAX_RETRIES = 3
_IMPERSONATE = "chrome131"


class VintedScraper(BaseScraper):
    """Scraper per Vinted.it con curl_cffi (TLS fingerprint di Chrome)."""

    @property
    def platform_name(self) -> str:
        return "vinted"

    async def search(
        self,
        keyword: str,
        min_price: float,
        max_price: float,
        category_name: str,
    ) -> list[Listing]:
        """Cerca inserzioni su Vinted.it."""
        all_listings: list[Listing] = []

        async with AsyncSession(
            headers=_HEADERS,
            impersonate=_IMPERSONATE,
            timeout=_REQUEST_TIMEOUT,
        ) as session:
            # Warm-up: visita homepage per ottenere cookie access_token_web
            if not await self._init_session(session):
                return []

            for page in range(1, _MAX_PAGES + 1):
                try:
                    listings = await self._fetch_page(
                        session, keyword, min_price, max_price, category_name, page
                    )
                    if not listings:
                        logger.debug(
                            "Nessun risultato a pagina %d per '%s', stop paginazione",
                            page, keyword,
                        )
                        break
                    all_listings.extend(listings)
                    logger.debug(
                        "Pagina %d per '%s': %d inserzioni", page, keyword, len(listings)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Errore scraping Vinted pagina %d per '%s'", page, keyword)
                    break

                if page < _MAX_PAGES:
                    delay = 1.5 + random.uniform(0.5, 2.0)
                    await asyncio.sleep(delay)

        logger.info(
            "Vinted: trovate %d inserzioni per '%s' [%s]",
            len(all_listings), keyword, category_name,
        )
        return all_listings

    async def _init_session(self, session: AsyncSession) -> bool:
        """Visita la homepage per ottenere il cookie access_token_web."""
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await session.get(_BASE_URL)
                logger.debug("Vinted homepage: HTTP %d", resp.status_code)

                # Verifica che il cookie sia stato impostato
                cookies = session.cookies
                has_token = any("access_token" in name for name in cookies.keys())
                if has_token:
                    logger.debug("Cookie access_token ottenuto")
                    return True

                # Anche senza cookie esplicito, la sessione potrebbe funzionare
                if resp.status_code == 200:
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    return True

            except Exception as e:
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning("Vinted: errore init sessione (tentativo %d): %s", attempt + 1, e)
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(wait)

        logger.error("Vinted: impossibile inizializzare la sessione")
        return False

    async def _fetch_page(
        self,
        session: AsyncSession,
        keyword: str,
        min_price: float,
        max_price: float,
        category_name: str,
        page: int,
    ) -> list[Listing]:
        """Scarica una pagina di risultati dall'API Vinted."""
        params = {
            "search_text": keyword,
            "price_from": str(int(min_price)),
            "price_to": str(int(max_price)),
            "currency": "EUR",
            "per_page": str(_PER_PAGE),
            "page": str(page),
            "order": "newest_first",
        }

        for attempt in range(_MAX_RETRIES):
            try:
                resp = await session.get(_API_URL, params=params)

                if resp.status_code == 401:
                    logger.warning("Vinted: sessione scaduta (401), reinizializzo")
                    if await self._init_session(session):
                        continue
                    return []

                if resp.status_code == 429:
                    wait = (2 ** attempt) + random.uniform(1, 3)
                    logger.warning("Vinted: rate limited (429), retry tra %.1fs", wait)
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code == 403:
                    wait = (2 ** attempt) + random.uniform(1, 3)
                    logger.warning("Vinted: forbidden (403), retry tra %.1fs", wait)
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code != 200:
                    logger.warning("Vinted: HTTP %d per '%s'", resp.status_code, keyword)
                    return []

                data = resp.json()
                break

            except Exception as e:
                if attempt < _MAX_RETRIES - 1:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning("Vinted: errore rete, retry tra %.1fs: %s", wait, e)
                    await asyncio.sleep(wait)
                    continue
                raise
        else:
            logger.warning("Vinted: tentativi esauriti per '%s' pagina %d", keyword, page)
            return []

        # Controlla risposta API
        if data.get("code", 0) != 0:
            logger.warning("Vinted API errore: code=%s", data.get("code"))
            return []

        items = data.get("items", [])
        listings: list[Listing] = []
        for item in items:
            listing = self._parse_item(item, category_name)
            if listing:
                listings.append(listing)

        return listings

    @staticmethod
    def _parse_item(item: dict, category_name: str) -> Optional[Listing]:
        """Converte un item dell'API Vinted in un Listing."""
        try:
            item_id = str(item.get("id", ""))
            if not item_id:
                return None

            title = item.get("title", "").strip()
            if not title:
                return None

            # Prezzo: stringa "25.00" o numero
            price_raw = item.get("price", "0")
            if isinstance(price_raw, str):
                price_raw = price_raw.replace(",", ".")
            try:
                price = float(price_raw)
            except (ValueError, TypeError):
                return None
            if price <= 0:
                return None

            currency = item.get("currency", "EUR")

            # URL
            item_url = item.get("url", "")
            if item_url and not item_url.startswith("http"):
                item_url = f"{_BASE_URL}{item_url}"
            if not item_url:
                item_url = f"{_BASE_URL}/items/{item_id}"

            # Immagine
            photo = item.get("photo") or {}
            image_url = photo.get("full_size_url") or photo.get("url")

            # Localita' (dall'oggetto user se disponibile)
            user = item.get("user") or {}
            city = user.get("city")
            country = user.get("country_title")
            if city and country:
                location = f"{city}, {country}"
            elif city:
                location = city
            else:
                location = None

            # Descrizione (non sempre presente nella lista, ma utile se c'e')
            description = item.get("description", "")

            # Brand
            brand = item.get("brand_title", "")
            if brand and description:
                description = f"{brand} | {description}"
            elif brand:
                description = brand

            # Timestamp
            created_at = item.get("created_at_ts", "")
            if not created_at:
                created_at = datetime.now(timezone.utc).isoformat()

            return Listing(
                id=item_id,
                platform="vinted",
                title=title,
                description=description,
                price=price,
                currency=currency,
                url=item_url,
                image_url=image_url,
                location=location,
                category_matched=category_name,
                timestamp=created_at,
            )
        except Exception:
            logger.debug("Errore parsing item Vinted", exc_info=True)
            return None
