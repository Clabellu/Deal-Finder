import asyncio
import json
import random
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper, Listing
from utils.logger import get_logger

logger = get_logger("subito")

_SEARCH_URL = "https://www.subito.it/annunci-italia/vendita/usato/"

# Headers realistici - browser standard
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

_REQUEST_TIMEOUT = 30
_MAX_PAGES = 3
_MAX_RETRIES = 3
_BASE_DELAY = 2.0

# curl_cffi impersona il fingerprint TLS di Chrome 131
_IMPERSONATE = "chrome131"


class SubitoScraper(BaseScraper):
    """Scraper per Subito.it con curl_cffi (TLS fingerprint di Chrome)."""

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

        async with AsyncSession(
            headers=_HEADERS,
            impersonate=_IMPERSONATE,
            timeout=_REQUEST_TIMEOUT,
        ) as session:
            # Warm-up: visita homepage per ottenere cookies
            try:
                resp = await session.get("https://www.subito.it/")
                logger.debug("Warm-up homepage: HTTP %d", resp.status_code)
                await asyncio.sleep(random.uniform(1.0, 2.0))
            except Exception:
                logger.debug("Warm-up homepage fallito, continuo comunque")

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
                    logger.exception("Errore scraping pagina %d per '%s'", page, keyword)
                    break

                if page < _MAX_PAGES:
                    delay = _BASE_DELAY + random.uniform(0.5, 2.5)
                    await asyncio.sleep(delay)

        logger.info(
            "Subito: trovate %d inserzioni per '%s' [%s]",
            len(all_listings), keyword, category_name,
        )
        return all_listings

    async def _fetch_page(
        self,
        session: AsyncSession,
        keyword: str,
        min_price: float,
        max_price: float,
        category_name: str,
        page: int,
    ) -> list[Listing]:
        """Scarica una pagina HTML e ne estrae gli annunci."""
        params = f"q={quote_plus(keyword)}&ps={int(min_price)}&pe={int(max_price)}&o={page}"
        url = f"{_SEARCH_URL}?{params}"

        html = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await session.get(url)

                if resp.status_code == 429:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning("Subito: rate limited (429), retry tra %.1fs", wait)
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code == 403:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning("Subito: forbidden (403), retry tra %.1fs", wait)
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code != 200:
                    logger.warning("Subito: HTTP %d per url %s", resp.status_code, url)
                    return []

                html = resp.text

            except Exception as e:
                if attempt < _MAX_RETRIES - 1:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning("Subito: errore rete, retry tra %.1fs: %s", wait, e)
                    await asyncio.sleep(wait)
                    continue
                raise

            # Richiesta riuscita, esci dal loop
            break
        else:
            # Tutti i retry esauriti
            logger.warning("Subito: tentativi esauriti per '%s' pagina %d", keyword, page)
            return []

        if html is None:
            return []

        # Prova prima ad estrarre JSON embedded, poi fallback su HTML parsing
        listings = self._extract_from_embedded_json(html, category_name)
        if listings:
            return listings

        return self._parse_html(html, category_name)

    def _extract_from_embedded_json(self, html: str, category_name: str) -> list[Listing]:
        """Estrae annunci dal JSON embedded nella pagina (Next.js __NEXT_DATA__ o simili)."""
        listings: list[Listing] = []

        # Metodo 1: __NEXT_DATA__ (Next.js)
        match = re.search(
            r'<script\s+id="__NEXT_DATA__"\s+type="application/json"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if match:
            try:
                next_data = json.loads(match.group(1))
                ads = self._dig_for_ads(next_data)
                if ads:
                    logger.debug("Estratti %d annunci da __NEXT_DATA__", len(ads))
                    for ad in ads:
                        listing = self._parse_ad_json(ad, category_name)
                        if listing:
                            listings.append(listing)
                    return listings
            except json.JSONDecodeError:
                logger.debug("__NEXT_DATA__ trovato ma JSON non valido")

        # Metodo 2: cerca blocchi JSON con array "ads" nel <script>
        for match in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
            text = match.group(1).strip()
            # Cerca assegnazioni come window.__CONFIG__ = {...} o JSON diretto
            json_match = re.search(r'=\s*(\{.*"ads"\s*:\s*\[.*?\].*?\})', text, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                    ads = data.get("ads", [])
                    if ads:
                        logger.debug("Estratti %d annunci da script embedded", len(ads))
                        for ad in ads:
                            listing = self._parse_ad_json(ad, category_name)
                            if listing:
                                listings.append(listing)
                        return listings
                except json.JSONDecodeError:
                    continue

        # Metodo 3: JSON-LD
        for match in re.finditer(
            r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL
        ):
            try:
                ld_data = json.loads(match.group(1))
                if isinstance(ld_data, dict) and ld_data.get("@type") == "ItemList":
                    items = ld_data.get("itemListElement", [])
                    for item in items:
                        listing = self._parse_ld_item(item, category_name)
                        if listing:
                            listings.append(listing)
                    if listings:
                        logger.debug("Estratti %d annunci da JSON-LD", len(listings))
                        return listings
            except json.JSONDecodeError:
                continue

        return []

    def _dig_for_ads(self, data, depth: int = 0) -> list:
        """Cerca ricorsivamente una lista 'ads' nel JSON __NEXT_DATA__."""
        if depth > 8:
            return []
        if isinstance(data, dict):
            if "ads" in data and isinstance(data["ads"], list) and len(data["ads"]) > 0:
                return data["ads"]
            for v in data.values():
                result = self._dig_for_ads(v, depth + 1)
                if result:
                    return result
        elif isinstance(data, list):
            for item in data:
                result = self._dig_for_ads(item, depth + 1)
                if result:
                    return result
        return []

    def _parse_ad_json(self, ad: dict, category_name: str) -> Optional[Listing]:
        """Converte un annuncio dal JSON embedded in un Listing."""
        try:
            # ID
            urn = ad.get("urn", "")
            listing_id = urn.split(":")[-1] if urn else str(ad.get("id", ""))
            if not listing_id:
                return None

            # Titolo
            title = ad.get("subject", ad.get("title", "")).strip()
            if not title:
                return None

            # Descrizione
            description = ad.get("body", ad.get("description", ""))

            # Prezzo — puo' essere in "features" o direttamente in "price"
            price = None
            features = ad.get("features", [])
            if features:
                price = self._extract_price_from_features(features)
            if price is None:
                price_data = ad.get("price", {})
                if isinstance(price_data, dict):
                    price = price_data.get("value") or price_data.get("amount")
                elif isinstance(price_data, (int, float)):
                    price = float(price_data)
            if price is None:
                return None

            # URL
            urls = ad.get("urls", {})
            url = urls.get("default", ad.get("url", ""))
            if not url:
                return None
            if not url.startswith("http"):
                url = "https://www.subito.it" + url

            # Immagine
            images = ad.get("images", [])
            image_url = None
            if images:
                first_img = images[0] if isinstance(images[0], dict) else {}
                image_url = (
                    first_img.get("cdn_url")
                    or first_img.get("big")
                    or first_img.get("base_url", "")
                )
                if image_url and "{size}" in image_url:
                    image_url = image_url.replace("{size}", "big")

            # Localita'
            geo = ad.get("geo", {})
            if isinstance(geo, dict):
                city = geo.get("city", {})
                city_name = city.get("value", "") if isinstance(city, dict) else str(city)
                region = geo.get("region", {})
                region_name = region.get("value", "") if isinstance(region, dict) else str(region)
                location = f"{city_name}, {region_name}".strip(", ") or None
            else:
                location = None

            # Timestamp
            dates = ad.get("dates", {})
            timestamp = dates.get("display", datetime.now(timezone.utc).isoformat())

            return Listing(
                id=listing_id,
                platform="subito",
                title=title,
                description=description,
                price=float(price),
                currency="EUR",
                url=url,
                image_url=image_url,
                location=location,
                category_matched=category_name,
                timestamp=timestamp,
            )
        except Exception:
            logger.debug("Errore parsing annuncio JSON Subito", exc_info=True)
            return None

    def _parse_ld_item(self, item: dict, category_name: str) -> Optional[Listing]:
        """Converte un item JSON-LD in un Listing."""
        try:
            url = item.get("url", "")
            if not url:
                return None
            id_match = re.search(r"/(\d+)\.htm", url)
            if not id_match:
                return None

            name = item.get("name", "")
            if not name:
                return None

            offers = item.get("offers", {})
            price = offers.get("price")
            if price is None:
                return None

            image = item.get("image", "")

            return Listing(
                id=id_match.group(1),
                platform="subito",
                title=name,
                description="",
                price=float(price),
                currency="EUR",
                url=url if url.startswith("http") else f"https://www.subito.it{url}",
                image_url=image or None,
                location=None,
                category_matched=category_name,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        except Exception:
            logger.debug("Errore parsing JSON-LD item", exc_info=True)
            return None

    def _parse_html(self, html: str, category_name: str) -> list[Listing]:
        """Fallback: parsing diretto dell'HTML con BeautifulSoup."""
        soup = BeautifulSoup(html, "html.parser")
        listings: list[Listing] = []

        # Cerca i link agli annunci
        links = soup.find_all("a", href=re.compile(r"subito\.it/.+\.htm"))
        seen_ids: set[str] = set()

        for link in links:
            href = link.get("href", "")
            id_match = re.search(r"/(\d+)\.htm", href)
            if not id_match:
                continue
            listing_id = id_match.group(1)
            if listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)

            # Cerca titolo nel link o nei suoi figli
            title = ""
            for tag in link.find_all(["h2", "h3", "span", "p"]):
                text = tag.get_text(strip=True)
                if len(text) > 10:
                    title = text
                    break
            if not title:
                title = link.get_text(strip=True)[:120]
            if not title or len(title) < 5:
                continue

            # Cerca prezzo
            price_text = ""
            for tag in link.find_all(["span", "p"], string=re.compile(r"[\d.,]+\s*€|€\s*[\d.,]+")):
                price_text = tag.get_text()
                break
            if not price_text:
                parent = link.parent
                if parent:
                    for tag in parent.find_all(["span", "p"], string=re.compile(r"[\d.,]+\s*€|€\s*[\d.,]+")):
                        price_text = tag.get_text()
                        break
            price = self._extract_price_text(price_text)
            if price is None:
                continue

            # Immagine
            img = link.find("img", src=True)
            image_url = img.get("src") if img else None
            if image_url and image_url.startswith("//"):
                image_url = "https:" + image_url

            url = href if href.startswith("http") else f"https://www.subito.it{href}"

            listings.append(Listing(
                id=listing_id,
                platform="subito",
                title=title,
                description="",
                price=price,
                currency="EUR",
                url=url,
                image_url=image_url,
                location=None,
                category_matched=category_name,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))

        if listings:
            logger.debug("Estratti %d annunci tramite HTML parsing", len(listings))
        return listings

    @staticmethod
    def _extract_price_from_features(features: list) -> Optional[float]:
        """Estrae il prezzo dalla lista features dell'API JSON."""
        for feature in features:
            uri = feature.get("uri", "")
            if "/price" in uri:
                values = feature.get("values", [])
                if values:
                    raw = values[0].get("value", "")
                    cleaned = re.sub(r"[^\d.,]", "", str(raw))
                    if not cleaned:
                        return None
                    cleaned = cleaned.replace(".", "").replace(",", ".")
                    try:
                        return float(cleaned)
                    except ValueError:
                        return None
        return None

    @staticmethod
    def _extract_price_text(text: str) -> Optional[float]:
        """Estrae un prezzo da testo libero (es. '€ 350' -> 350.0)."""
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", text)
        if not cleaned:
            return None
        cleaned = cleaned.replace(".", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None
