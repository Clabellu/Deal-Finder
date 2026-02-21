"""Wrapper per il motore di monitoraggio che gira in un thread separato."""

import asyncio
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

import yaml
from dotenv import load_dotenv

from analyzer.llm_parser import LLMParser
from analyzer.price_checker import PriceChecker
from db.database import Database
from notifier.bot_commands import BotController
from notifier.telegram_bot import TelegramNotifier
from scrapers.base_scraper import BaseScraper
from scrapers.subito import SubitoScraper
from scrapers.ebay import EbayScraper
from utils.logger import get_logger

load_dotenv()

logger = get_logger("engine")

_SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "subito": SubitoScraper,
    "ebay": EbayScraper,
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
        product_name: str,
        asked_price: float,
        platform: str,
        ebay_query: str = "",
        market_price: float = 0,
        margin_percent: float = 0,
        sold_count: int = 0,
        status: str = "",
    ) -> None:
        if self.on_listing_analyzed:
            self.on_listing_analyzed({
                "product_name": product_name,
                "asked_price": asked_price,
                "platform": platform,
                "ebay_query": ebay_query,
                "market_price": market_price,
                "margin_percent": margin_percent,
                "sold_count": sold_count,
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

        if not scrapers:
            self._emit_log("Nessuno scraper attivo!")
            await db.close()
            return

        llm_config = config.get("llm", {})
        parser = LLMParser(
            model=llm_config.get("model", "claude-haiku-4-5-20251001"),
            max_tokens=llm_config.get("max_tokens", 500),
            temperature=llm_config.get("temperature", 0),
        )

        pricing_config = config.get("pricing", {})
        price_checker = PriceChecker(
            sold_items_count=pricing_config.get("ebay_sold_items_count", 20),
            use_median=pricing_config.get("use_median", True),
            max_days_sold=pricing_config.get("max_days_sold", 30),
            db=db,
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
                    await self._run_cycle(config, db, scrapers, parser, price_checker, notifier, bot)
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

                # Attendi con controllo periodico per stop
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
        parser: LLMParser,
        price_checker: PriceChecker,
        notifier: TelegramNotifier,
        bot: BotController,
    ) -> None:
        """Esegue un singolo ciclo di scraping + analisi."""
        min_margin = config.get("min_margin_percent", 25)
        max_listings = config.get("max_listings_per_cycle", 50)
        categories = config.get("categories", [])
        include_image = (
            config.get("notifications", {}).get("telegram", {}).get("include_image", True)
        )

        for platform_name, scraper in scrapers.items():
            for category in categories:
                cat_name = category["name"]
                keywords = category.get("keywords", [])
                min_price = category.get("min_price", 0)
                max_price = category.get("max_price", 99999)

                for keyword in keywords:
                    if self._stop_event.is_set() or self._paused:
                        return

                    try:
                        listings = await scraper.search(keyword, min_price, max_price, cat_name)
                    except Exception:
                        logger.exception("Errore scraping %s per '%s'", platform_name, keyword)
                        continue

                    listings = listings[:max_listings]
                    self._emit_log(f"{platform_name}: {len(listings)} risultati per '{keyword}'")

                    for listing in listings:
                        if self._stop_event.is_set() or self._paused:
                            return

                        if await db.is_seen(listing.id, listing.platform):
                            continue

                        await db.mark_seen(listing.id, listing.platform)

                        try:
                            parsed = await parser.parse_listing(listing)
                        except Exception:
                            self._emit_analysis(listing.title, listing.price, platform_name, status="errore_llm")
                            continue

                        if parsed is None:
                            self._emit_analysis(listing.title, listing.price, platform_name, status="skip_llm")
                            continue

                        try:
                            price_result = await price_checker.check_price(
                                parsed.ebay_search_query, parsed.condition
                            )
                        except Exception:
                            self._emit_analysis(
                                parsed.product_name, listing.price, platform_name,
                                ebay_query=parsed.ebay_search_query, status="errore_prezzo",
                            )
                            continue

                        if price_result is None:
                            self._emit_analysis(
                                parsed.product_name, listing.price, platform_name,
                                ebay_query=parsed.ebay_search_query, status="no_prezzo",
                            )
                            continue

                        reference_price = price_result.median_price
                        margin = reference_price - listing.price
                        margin_percent = (margin / listing.price * 100) if listing.price > 0 else 0

                        if margin_percent < min_margin:
                            self._emit_analysis(
                                parsed.product_name, listing.price, platform_name,
                                ebay_query=parsed.ebay_search_query,
                                market_price=reference_price, margin_percent=margin_percent,
                                sold_count=price_result.sold_count, status="sotto_soglia",
                            )
                            continue

                        # DEAL trovato!
                        self._emit_analysis(
                            parsed.product_name, listing.price, platform_name,
                            ebay_query=parsed.ebay_search_query,
                            market_price=reference_price, margin_percent=margin_percent,
                            sold_count=price_result.sold_count, status="deal",
                        )

                        try:
                            sent = await notifier.send_deal(
                                product_name=parsed.product_name,
                                asked_price=listing.price,
                                median_price=price_result.median_price,
                                margin=margin,
                                margin_percent=margin_percent,
                                min_price=price_result.min_price,
                                max_price=price_result.max_price,
                                sold_count=price_result.sold_count,
                                key_details=parsed.key_details,
                                location=listing.location,
                                platform=listing.platform,
                                url=listing.url,
                                image_url=listing.image_url,
                                include_image=include_image,
                            )
                        except Exception:
                            sent = False

                        if sent:
                            await db.mark_seen(listing.id, listing.platform, notified=True)
                            await db.save_notification(
                                listing_id=listing.id,
                                platform=listing.platform,
                                product_name=parsed.product_name,
                                asked_price=listing.price,
                                market_price=price_result.median_price,
                                margin_percent=margin_percent,
                            )
                            self._deals_found += 1
                            self._emit_log(
                                f"DEAL! {parsed.product_name}: "
                                f"{listing.price:.0f}EUR -> mercato {reference_price:.0f}EUR "
                                f"(+{margin_percent:.0f}%)"
                            )
                            if self.on_deal_found:
                                self.on_deal_found({
                                    "product_name": parsed.product_name,
                                    "asked_price": listing.price,
                                    "market_price": reference_price,
                                    "margin_percent": margin_percent,
                                    "platform": listing.platform,
                                    "url": listing.url,
                                })
