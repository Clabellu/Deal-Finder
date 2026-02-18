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

_EBAY_SEARCH_URLS = [
    "https://www.ebay.it/sch/i.html",
    "https://www.ebay.com/sch/i.html",
]

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

        Prova prima la query originale, poi con query semplificata,
        poi senza filtro condizione.

        Args:
            search_query: Query di ricerca ottimizzata (dal LLM parser).
            condition: Condizione del prodotto per filtrare i risultati.

        Returns:
            PriceResult con statistiche di prezzo, o None se la ricerca fallisce.
        """
        # Pulisci la query da parole che non aiutano la ricerca eBay
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
            prices = await self._scrape_sold_listings(query, cond)
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

    async def _scrape_sold_listings(
        self,
        query: str,
        condition: str,
    ) -> list[float]:
        """Scraping delle inserzioni vendute su eBay (prima .it, poi .com come fallback)."""
        params = {
            "_nkw": query,
            "LH_Complete": "1",     # Inserzioni completate
            "LH_Sold": "1",        # Solo vendute
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

        qs = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())

        async with AsyncSession(
            headers=_HEADERS,
            impersonate=_IMPERSONATE,
            timeout=_REQUEST_TIMEOUT,
        ) as session:
            for base_url in _EBAY_SEARCH_URLS:
                url = f"{base_url}?{qs}"
                html = await self._fetch_with_retry(session, url)
                if not html:
                    continue
                prices = self._extract_prices_from_html(html)
                if prices:
                    return prices
                logger.debug("Nessun prezzo estratto da %s, provo prossimo dominio", base_url)

        return []

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

                html = resp.text
                logger.debug(
                    "eBay risposta: %d caratteri, status=%d",
                    len(html), resp.status_code,
                )

                # Controlla se eBay ha servito un CAPTCHA o pagina di blocco
                html_lower = html.lower()
                if "captcha" in html_lower or "robot" in html_lower:
                    logger.warning("eBay: CAPTCHA rilevato, richiesta bloccata")
                    return None
                if "signin" in html_lower and "s-item" not in html_lower:
                    logger.warning("eBay: redirect a pagina di login")
                    return None

                return html

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
        logger.debug("eBay HTML: %d li.s-item trovati", len(items))

        if not items:
            # Prova selettori alternativi nel caso eBay abbia cambiato struttura
            for alt_sel in [
                "ul.srp-results > li",
                "[data-viewport]",
                ".s-item__wrapper",
            ]:
                alt_items = soup.select(alt_sel)
                if alt_items:
                    logger.debug("eBay: selettore alternativo '%s' ha trovato %d elementi", alt_sel, len(alt_items))

            # Log per debug: mostra titolo pagina e snippet
            title = soup.title.string if soup.title else "N/A"
            logger.info("eBay: nessun risultato nel parsing HTML (titolo pagina: %s)", title)

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
