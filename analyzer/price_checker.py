import asyncio
import random
import re
import statistics
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

from utils.logger import get_logger

logger = get_logger("price_checker")

_EBAY_SEARCH_URL = "https://www.ebay.it/sch/i.html"

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
_REQUEST_TIMEOUT = 20
_MAX_RETRIES = 3


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
    """Cerca il prezzo medio di mercato scraping le inserzioni vendute su eBay.it."""

    def __init__(
        self,
        sold_items_count: int = 20,
        use_median: bool = True,
        max_days_sold: int = 30,
    ):
        self._sold_items_count = sold_items_count
        self._use_median = use_median
        self._max_days_sold = max_days_sold

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
        prices = await self._scrape_sold_listings(search_query, condition)

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

    async def _scrape_sold_listings(
        self,
        query: str,
        condition: str,
    ) -> list[float]:
        """Scraping delle inserzioni vendute su eBay.it."""
        params = {
            "_nkw": query,
            "LH_Complete": "1",     # Inserzioni completate
            "LH_Sold": "1",        # Solo vendute
            "LH_BIN": "1",         # Solo Compralo Subito
            "_ipg": str(min(self._sold_items_count * 3, 240)),  # Risultati per pagina
            "_sop": "13",           # Ordina per piu' recenti
        }

        # Mappa condizione a filtro eBay
        condition_id = {
            "nuovo": "1000",
            "come_nuovo": "1500",
            "usato_buono": "3000",
            "usato_discreto": "3000",
        }.get(condition)
        if condition_id:
            params["LH_ItemCondition"] = condition_id

        url = f"{_EBAY_SEARCH_URL}?{'&'.join(f'{k}={quote_plus(str(v))}' for k, v in params.items())}"

        async with AsyncSession(
            headers=_HEADERS,
            impersonate=_IMPERSONATE,
            timeout=_REQUEST_TIMEOUT,
        ) as session:
            html = await self._fetch_with_retry(session, url)

        if not html:
            return []

        return self._extract_prices_from_html(html)

    async def _fetch_with_retry(self, session: AsyncSession, url: str) -> Optional[str]:
        """Fetch con retry ed exponential backoff."""
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await session.get(url)

                if resp.status_code == 429:
                    wait = (2 ** attempt) + random.uniform(0.5, 1.5)
                    logger.warning("eBay: rate limited (429), retry tra %.1fs", wait)
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code != 200:
                    logger.warning("eBay sold listings HTTP %d", resp.status_code)
                    return None

                return resp.text

            except Exception as e:
                if attempt < _MAX_RETRIES - 1:
                    wait = (2 ** attempt) + random.uniform(0.5, 1.5)
                    logger.warning("eBay: errore rete, retry tra %.1fs: %s", wait, e)
                    await asyncio.sleep(wait)
                    continue
                logger.exception("Errore scraping eBay sold listings")
                return None

        return None

    def _extract_prices_from_html(self, html: str) -> list[float]:
        """Estrae i prezzi di vendita dall'HTML di eBay.it."""
        soup = BeautifulSoup(html, "html.parser")
        prices: list[float] = []

        # Ogni risultato e' un <li> con classe s-item
        items = soup.select("li.s-item")

        for item in items:
            # Salta il primo item fittizio (placeholder eBay)
            title_el = item.select_one(".s-item__title")
            if title_el and "Shop on eBay" in title_el.get_text():
                continue

            price_el = item.select_one(".s-item__price")
            if not price_el:
                continue

            price_text = price_el.get_text(strip=True)
            price = self._parse_price(price_text)
            if price is not None and price > 0:
                prices.append(price)

            if len(prices) >= self._sold_items_count:
                break

        logger.debug("eBay: estratti %d prezzi venduti per questa query", len(prices))
        return prices

    @staticmethod
    def _parse_price(text: str) -> Optional[float]:
        """Converte testo prezzo eBay in float (es. 'EUR 249,00' -> 249.0)."""
        if not text:
            return None

        # Gestisci range di prezzo (es. "EUR 100,00 a EUR 200,00") — prendi il primo
        # Gestisci "Da EUR X" — prendi il prezzo
        parts = re.split(r'\s+a\s+|\s+to\s+', text, maxsplit=1)
        price_text = parts[0]

        # Rimuovi tutto tranne cifre, punti e virgole
        cleaned = re.sub(r"[^\d.,]", "", price_text)
        if not cleaned:
            return None

        # Formato europeo: 1.234,56 -> 1234.56
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")

        try:
            return float(cleaned)
        except ValueError:
            return None
