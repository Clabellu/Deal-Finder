"""Wrapper per il motore di monitoraggio che gira in un thread separato."""

import asyncio
import statistics
import threading
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import quote_plus

import yaml
from dotenv import load_dotenv

from analyzer.price_checker import PriceChecker
from db.database import Database
from notifier.bot_commands import BotController
from notifier.telegram_bot import TelegramNotifier
from scrapers.base_scraper import BaseScraper
from scrapers.subito import SubitoScraper
from scrapers.vinted import VintedScraper
from utils.logger import get_logger

load_dotenv()

logger = get_logger("engine")

# eBay non usa scraper (rate limit API), usa PriceChecker (web scraping)
_SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "subito": SubitoScraper,
    "vinted": VintedScraper,
}


class MonitorEngine:
    """Gestisce il ciclo di monitoraggio in un thread separato."""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event = threading.Event()
        self._running = False
        self._paused = False
        self._started_at: Optional[datetime] = None
        self._cycle_count = 0
        self._deals_found = 0

        # Callbacks per aggiornare la GUI (thread-safe tramite root.after)
        self.on_status_change: Optional[Callable[[str], None]] = None
        self.on_cycle_complete: Optional[Callable[[dict], None]] = None
        self.on_deal_found: Optional[Callable[[dict], None]] = None
        self.on_listing_analyzed: Optional[Callable[[dict], None]] = None
        self.on_log: Optional[Callable[[str], None]] = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def cycle_count(self) -> int:
        return self._cycle_count

    @property
    def deals_found(self) -> int:
        return self._deals_found

    @property
    def uptime(self) -> str:
        if not self._started_at:
            return "—"
        delta = datetime.now(timezone.utc) - self._started_at
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{hours}h {minutes}m"

    def start(self) -> None:
        """Avvia il monitoraggio in un thread separato."""
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ferma il monitoraggio."""
        self._stop_event.set()
        self._running = False
        self._paused = False
        self._emit_status("stopped")

    def pause(self) -> None:
        self._paused = True
        self._emit_status("paused")

    def resume(self) -> None:
        self._paused = False
        self._emit_status("running")

    def _emit_status(self, status: str) -> None:
        if self.on_status_change:
            self.on_status_change(status)

    def _emit_log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    def _emit_analysis(
        self,
        keyword: str,
        subito_median: float = 0,
        subito_count: int = 0,
        subito_best_url: str = "",
        ebay_median: float = 0,
        ebay_count: int = 0,
        ebay_best_url: str = "",
        vinted_median: float = 0,
        vinted_count: int = 0,
        vinted_best_url: str = "",
        market_price: float = 0,
        sold_count: int = 0,
        margin_percent: float = 0,
        best_platform: str = "—",
        best_price: float = 0,
        best_url: str = "",
        status: str = "",
    ) -> None:
        if self.on_listing_analyzed:
            self.on_listing_analyzed({
                "keyword": keyword,
                "subito_median": subito_median,
                "subito_count": subito_count,
                "subito_best_url": subito_best_url,
                "ebay_median": ebay_median,
                "ebay_count": ebay_count,
                "ebay_best_url": ebay_best_url,
                "vinted_median": vinted_median,
                "vinted_count": vinted_count,
                "vinted_best_url": vinted_best_url,
                "market_price": market_price,
                "sold_count": sold_count,
                "margin_percent": margin_percent,
                "best_platform": best_platform,
                "best_price": best_price,
                "best_url": best_url,
                "status": status,
            })

    def _run_loop(self) -> None:
        """Entrypoint del thread: crea un event loop e avvia il ciclo async."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main())
        except Exception as e:
            logger.exception("Errore fatale nel motore di monitoraggio")
            self._emit_log(f"Errore fatale: {e}")
        finally:
            self._loop.close()
            self._running = False
            self._emit_status("stopped")

    async def _async_main(self) -> None:
        """Loop principale async del motore."""
        self._running = True
        self._started_at = datetime.now(timezone.utc)
        self._cycle_count = 0
        self._deals_found = 0
        self._emit_status("running")
        self._emit_log("Motore avviato")

        # Carica config
        try:
            with open("config.yaml", "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
        except FileNotFoundError:
            self._emit_log("config.yaml non trovato!")
            return

        # Inizializza componenti
        db = Database()
        await db.connect()

        active_platforms = config.get("platforms", [])
        scrapers: dict[str, BaseScraper] = {}
        for platform in active_platforms:
            scraper_cls = _SCRAPER_REGISTRY.get(platform)
            if scraper_cls:
                scrapers[platform] = scraper_cls()
                self._emit_log(f"Scraper attivato: {platform}")
        # eBay usa PriceChecker (web scraping) invece dell'API (rate limited)
        if "ebay" in active_platforms:
            self._emit_log("eBay: dati da PriceChecker (web scraping)")

        if not scrapers and "ebay" not in active_platforms:
            self._emit_log("Nessuno scraper attivo!")
            await db.close()
            return

        pricing_config = config.get("pricing", {})
        cache_hours = pricing_config.get("cache_hours", 24)
        price_checker = PriceChecker(
            sold_items_count=pricing_config.get("ebay_sold_items_count", 20),
            use_median=pricing_config.get("use_median", True),
            max_days_sold=pricing_config.get("max_days_sold", 30),
            db=db,
            cache_ttl=cache_hours * 3600,
        )

        notifier = TelegramNotifier()
        bot = BotController(db)

        try:
            await bot.start()
        except Exception:
            self._emit_log("Bot Telegram non avviato (continuo senza)")

        polling_interval = config.get("polling_interval", 300)

        try:
            while not self._stop_event.is_set():
                if self._paused:
                    await asyncio.sleep(1)
                    continue

                # Ricarica config ad ogni ciclo per riflettere le modifiche dalla GUI
                try:
                    with open("config.yaml", "r", encoding="utf-8") as f:
                        config = yaml.safe_load(f)
                except Exception:
                    pass

                try:
                    await self._run_cycle(config, db, scrapers, price_checker, notifier, bot)
                    self._cycle_count += 1
                    bot.update_stats(self._cycle_count, self._deals_found)

                    if self.on_cycle_complete:
                        stats = await db.get_stats()
                        stats["cycle_count"] = self._cycle_count
                        stats["deals_found"] = self._deals_found
                        self.on_cycle_complete(stats)
                except Exception as e:
                    logger.exception("Errore nel ciclo")
                    self._emit_log(f"Errore ciclo: {e}")

                # Pulizia periodica
                try:
                    await db.cleanup_old_records(days=30)
                except Exception:
                    pass

                self._emit_log(f"Ciclo {self._cycle_count} completato. Prossimo tra {polling_interval}s")

                for _ in range(polling_interval):
                    if self._stop_event.is_set():
                        break
                    await asyncio.sleep(1)
        finally:
            await bot.stop()
            await db.close()
            self._emit_log("Motore fermato")

    async def _run_cycle(
        self,
        config: dict,
        db: Database,
        scrapers: dict[str, BaseScraper],
        price_checker: PriceChecker,
        notifier: TelegramNotifier,
        bot: BotController,
    ) -> None:
        """Esegue un singolo ciclo: per ogni keyword cerca su tutte le piattaforme
        e confronta i prezzi con la media venduti eBay."""
        min_margin = config.get("min_margin_percent", 25)
        max_listings = config.get("max_listings_per_cycle", 50)
        categories = config.get("categories", [])
        include_image = (
            config.get("notifications", {}).get("telegram", {}).get("include_image", True)
        )

        for category in categories:
            cat_name = category["name"]
            keywords = category.get("keywords", [])
            min_price = category.get("min_price", 0)
            max_price = category.get("max_price", 99999)

            for keyword in keywords:
                if self._stop_event.is_set() or self._paused:
                    return

                self._emit_log(f"Ricerca '{keyword}' su Subito, eBay e Vinted...")

                # Ricerca parallela: Subito/Vinted via scraper, eBay via PriceChecker
                search_tasks = {
                    platform_name: asyncio.create_task(
                        scraper.search(keyword, min_price, max_price, cat_name)
                    )
                    for platform_name, scraper in scrapers.items()
                }
                # eBay: PriceChecker (web scraping, no rate limit API)
                price_task = asyncio.create_task(price_checker.check_price(keyword, ""))

                platform_listings: dict[str, list] = {}
                for platform_name, task in search_tasks.items():
                    try:
                        listings = await task
                        platform_listings[platform_name] = listings[:max_listings]
                        self._emit_log(
                            f"{platform_name}: {len(platform_listings[platform_name])} "
                            f"risultati per '{keyword}'"
                        )
                    except Exception as e:
                        logger.warning("Errore scraping %s per '%s': %s", platform_name, keyword, e)
                        platform_listings[platform_name] = []

                # Risultato PriceChecker (eBay venduti + attivi via scraping)
                try:
                    price_result = await price_task
                except Exception as e:
                    logger.warning("Errore price check '%s': %s", keyword, e)
                    price_result = None

                # Calcola mediana e miglior annuncio per Subito e Vinted
                def platform_stats(listings):
                    valid = [l for l in listings if l.price > 0]
                    if not valid:
                        return 0.0, 0, 0.0, ""
                    prices = sorted(l.price for l in valid)
                    med = statistics.median(prices)
                    best = min(valid, key=lambda l: l.price)
                    return float(med), len(valid), best.price, best.url

                sub_med, sub_cnt, sub_best_p, sub_best_url = platform_stats(
                    platform_listings.get("subito", [])
                )
                vnt_med, vnt_cnt, vnt_best_p, vnt_best_url = platform_stats(
                    platform_listings.get("vinted", [])
                )

                # eBay dati dal PriceChecker (web scraping, funziona sempre)
                if price_result:
                    eby_med = price_result.active_median
                    eby_cnt = price_result.active_count
                    market_price = price_result.median_price
                    sold_count = price_result.sold_count
                    self._emit_log(
                        f"ebay: {eby_cnt} attivi (med. {eby_med:.0f}EUR), "
                        f"{sold_count} venduti (med. {market_price:.0f}EUR) per '{keyword}'"
                    )
                else:
                    eby_med, eby_cnt = 0.0, 0
                    market_price, sold_count = 0.0, 0

                # URL di ricerca eBay (non abbiamo singoli annunci)
                eby_best_url = (
                    f"https://www.ebay.it/sch/i.html?_nkw={quote_plus(keyword)}&LH_BIN=1"
                    if eby_med > 0 else ""
                )
                eby_best_p = price_result.min_price if price_result and eby_med > 0 else 0.0

                # Trova il prezzo e la piattaforma migliore tra le 3
                candidates = [
                    (sub_best_p, "Subito", sub_best_url),
                    (eby_best_p, "eBay", eby_best_url),
                    (vnt_best_p, "Vinted", vnt_best_url),
                ]
                valid_candidates = [(p, pl, u) for p, pl, u in candidates if p > 0 and u]

                if not valid_candidates:
                    self._emit_analysis(
                        keyword=keyword,
                        subito_median=sub_med, subito_count=sub_cnt,
                        ebay_median=eby_med, ebay_count=eby_cnt,
                        vinted_median=vnt_med, vinted_count=vnt_cnt,
                        market_price=market_price, sold_count=sold_count,
                        status="no_prezzo",
                    )
                    continue

                best_price, best_platform, best_url = min(valid_candidates, key=lambda x: x[0])

                if market_price > 0:
                    margin_percent = (market_price - best_price) / best_price * 100
                    status = "deal" if margin_percent >= min_margin else "sotto_soglia"
                else:
                    margin_percent = 0.0
                    status = "no_prezzo"

                self._emit_analysis(
                    keyword=keyword,
                    subito_median=sub_med, subito_count=sub_cnt, subito_best_url=sub_best_url,
                    ebay_median=eby_med, ebay_count=eby_cnt, ebay_best_url=eby_best_url,
                    vinted_median=vnt_med, vinted_count=vnt_cnt, vinted_best_url=vnt_best_url,
                    market_price=market_price, sold_count=sold_count,
                    margin_percent=margin_percent,
                    best_platform=best_platform, best_price=best_price, best_url=best_url,
                    status=status,
                )

                if status != "deal":
                    continue

                # DEAL trovato!
                self._deals_found += 1
                self._emit_log(
                    f"DEAL! '{keyword}': {best_price:.0f}EUR su {best_platform} "
                    f"(mercato {market_price:.0f}EUR, +{margin_percent:.0f}%)"
                )

                if self.on_deal_found:
                    self.on_deal_found({
                        "product_name": keyword,
                        "asked_price": best_price,
                        "market_price": market_price,
                        "margin_percent": margin_percent,
                        "platform": best_platform,
                        "url": best_url,
                    })

                # Trova immagine dal miglior annuncio per la notifica Telegram
                best_listings = platform_listings.get(best_platform.lower(), [])
                best_listing_obj = (
                    min(best_listings, key=lambda l: l.price) if best_listings else None
                )
                image_url = best_listing_obj.image_url if best_listing_obj else None
                location = best_listing_obj.location if best_listing_obj else None

                summary = (
                    f"Subito: {sub_med:.0f}€ ({sub_cnt}) | "
                    f"eBay: {eby_med:.0f}€ ({eby_cnt}) | "
                    f"Vinted: {vnt_med:.0f}€ ({vnt_cnt})"
                )

                try:
                    sent = await notifier.send_deal(
                        product_name=keyword,
                        asked_price=best_price,
                        median_price=market_price,
                        margin=market_price - best_price,
                        margin_percent=margin_percent,
                        min_price=price_result.min_price if price_result else 0,
                        max_price=price_result.max_price if price_result else 0,
                        sold_count=sold_count,
                        key_details=summary,
                        location=location,
                        platform=best_platform,
                        url=best_url,
                        image_url=image_url,
                        include_image=include_image,
                    )
                except Exception:
                    sent = False

                if sent:
                    await db.save_notification(
                        listing_id=keyword,
                        platform=best_platform.lower(),
                        product_name=keyword,
                        asked_price=best_price,
                        market_price=market_price,
                        margin_percent=margin_percent,
                    )
