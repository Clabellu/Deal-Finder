import asyncio
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper, Listing
from utils.logger import get_logger

logger = get_logger("subito")

# Headers realistici per evitare blocchi
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
}

_BASE_URL = "https://www.subito.it/annunci-italia/vendita/usato/"
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
_DELAY_BETWEEN_PAGES = 2.0  # secondi tra una pagina e l'altra
_MAX_PAGES = 3


class SubitoScraper(BaseScraper):
    """Scraper per Subito.it."""

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
        """Cerca inserzioni su Subito.it."""
        all_listings: list[Listing] = []

        async with aiohttp.ClientSession(
            headers=_HEADERS, timeout=_REQUEST_TIMEOUT
        ) as session:
            for page in range(1, _MAX_PAGES + 1):
                try:
                    listings = await self._fetch_page(
                        session, keyword, min_price, max_price, category_name, page
                    )
                    if not listings:
                        logger.debug(
                            "Nessun risultato a pagina %d per '%s', stop paginazione",
                            page,
                            keyword,
                        )
                        break
                    all_listings.extend(listings)
                    logger.debug(
                        "Pagina %d per '%s': %d inserzioni", page, keyword, len(listings)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Errore scraping pagina %d per '%s'", page, keyword
                    )
                    break

                if page < _MAX_PAGES:
                    await asyncio.sleep(_DELAY_BETWEEN_PAGES)

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
        """Scarica e parsa una singola pagina di risultati."""
        params = {
            "q": keyword,
            "ps": str(int(min_price)),
            "pe": str(int(max_price)),
            "o": str(page),
        }
        url = _BASE_URL + "?" + "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())

        async with session.get(url) as resp:
            if resp.status == 429:
                logger.warning("Subito: rate limited (429), attendo prima di riprovare")
                await asyncio.sleep(10)
                return []
            if resp.status != 200:
                logger.warning("Subito: HTTP %d per url %s", resp.status, url)
                return []
            html = await resp.text()

        return self._parse_listings(html, category_name)

    def _parse_listings(self, html: str, category_name: str) -> list[Listing]:
        """Parsa l'HTML di Subito.it ed estrae le inserzioni."""
        soup = BeautifulSoup(html, "html.parser")
        listings: list[Listing] = []

        # Subito usa un div con classe che contiene "items" per le card
        # Cerchiamo i link agli annunci
        items = soup.select("div.items__item, div[class*='ItemCard'], a[class*='SmallCard']")

        if not items:
            # Fallback: cerchiamo tutti i link che puntano ad annunci
            items = soup.find_all("a", href=re.compile(r"subito\.it/.+\.htm"))

        for item in items:
            listing = self._parse_single_item(item, category_name)
            if listing:
                listings.append(listing)

        return listings

    def _parse_single_item(self, item, category_name: str) -> Optional[Listing]:
        """Parsa un singolo elemento della lista risultati."""
        try:
            # Estrai URL
            if item.name == "a":
                url = item.get("href", "")
            else:
                link = item.find("a", href=True)
                url = link["href"] if link else ""

            if not url or "subito.it" not in url:
                return None

            if not url.startswith("http"):
                url = "https://www.subito.it" + url

            # Estrai ID dall'URL (es. /annuncio/12345.htm -> 12345)
            id_match = re.search(r"/(\d+)\.htm", url)
            if not id_match:
                return None
            listing_id = id_match.group(1)

            # Estrai titolo
            title_el = item.find(
                ["h2", "h3", "span"],
                class_=re.compile(r"(?i)(title|name|subject)", re.IGNORECASE),
            )
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                # Fallback: primo testo significativo
                title = item.get_text(strip=True)[:120]

            if not title:
                return None

            # Estrai prezzo
            price_el = item.find(
                ["span", "p", "div"],
                class_=re.compile(r"(?i)price", re.IGNORECASE),
            )
            price = self._extract_price(price_el.get_text() if price_el else "")
            if price is None:
                return None

            # Estrai immagine
            img_el = item.find("img", src=True)
            image_url = img_el.get("src") if img_el else None
            if image_url and image_url.startswith("//"):
                image_url = "https:" + image_url

            # Estrai localita'
            loc_el = item.find(
                ["span", "p"],
                class_=re.compile(r"(?i)(city|location|town|place)", re.IGNORECASE),
            )
            location = loc_el.get_text(strip=True) if loc_el else None

            return Listing(
                id=listing_id,
                platform="subito",
                title=title,
                description="",  # La descrizione completa richiede una visita alla pagina
                price=price,
                currency="EUR",
                url=url,
                image_url=image_url,
                location=location,
                category_matched=category_name,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        except Exception:
            logger.debug("Errore parsing singolo item Subito", exc_info=True)
            return None

    @staticmethod
    def _extract_price(text: str) -> Optional[float]:
        """Estrae un prezzo numerico da una stringa (es. '€ 350' -> 350.0)."""
        if not text:
            return None
        # Rimuovi tutto tranne cifre, virgola e punto
        cleaned = re.sub(r"[^\d.,]", "", text)
        if not cleaned:
            return None
        # Gestisci formato italiano (1.200,00 o 350)
        cleaned = cleaned.replace(".", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None
