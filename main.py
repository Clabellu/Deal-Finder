#!/usr/bin/env python3
"""Deal Finder — entry point principale.

Monitora marketplace di vendita tra privati per trovare inserzioni sotto prezzo.
"""

import asyncio
import signal
import sys

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
from utils.logger import setup_logger, get_logger

load_dotenv()

logger = get_logger("main")

# Mappa piattaforma -> classe scraper
_SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "subito": SubitoScraper,
    "ebay": EbayScraper,
}

_shutdown = False


def _handle_signal(sig, frame):
    global _shutdown
    logger.info("Segnale %s ricevuto, arresto in corso...", sig)
    _shutdown = True


async def run_cycle(
    config: dict,
    db: Database,
    scrapers: dict[str, BaseScraper],
    parser: LLMParser,
    price_checker: PriceChecker,
    notifier: TelegramNotifier,
    bot: BotController | None = None,
) -> None:
    """Esegue un ciclo completo di scraping, analisi e notifica."""
    min_margin = config.get("min_margin_percent", 25)
    max_listings = config.get("max_listings_per_cycle", 50)
    categories = config.get("categories", [])
    include_image = (
        config.get("notifications", {}).get("telegram", {}).get("include_image", True)
    )

    total_new = 0
    total_notified = 0

    for platform_name, scraper in scrapers.items():
        for category in categories:
            cat_name = category["name"]
            keywords = category.get("keywords", [])
            min_price = category.get("min_price", 0)
            max_price = category.get("max_price", 99999)

            for keyword in keywords:
                if _shutdown or (bot and bot.is_paused):
                    return

                try:
                    listings = await scraper.search(keyword, min_price, max_price, cat_name)
                except Exception:
                    logger.exception(
                        "Errore scraping %s per '%s' [%s]",
                        platform_name,
                        keyword,
                        cat_name,
                    )
                    continue

                # Limita il numero di inserzioni per ciclo
                listings = listings[:max_listings]

                for listing in listings:
                    if _shutdown or (bot and bot.is_paused):
                        return

                    # Controlla se gia' vista
                    if await db.is_seen(listing.id, listing.platform):
                        continue

                    total_new += 1

                    # Segna come vista subito per evitare duplicati in cicli paralleli
                    await db.mark_seen(listing.id, listing.platform)

                    # Parsing LLM
                    try:
                        parsed = await parser.parse_listing(listing)
                    except Exception:
                        logger.exception(
                            "Errore LLM parsing per listing %s", listing.id
                        )
                        continue

                    if parsed is None:
                        continue

                    # Price check su eBay
                    try:
                        price_result = await price_checker.check_price(
                            parsed.ebay_search_query, parsed.condition
                        )
                    except Exception:
                        logger.exception(
                            "Errore price check per listing %s", listing.id
                        )
                        continue

                    if price_result is None:
                        continue

                    # Calcola margine
                    reference_price = price_result.median_price
                    margin = reference_price - listing.price
                    margin_percent = (margin / listing.price * 100) if listing.price > 0 else 0

                    if margin_percent < min_margin:
                        logger.debug(
                            "Margine %.0f%% sotto soglia %.0f%% per %s",
                            margin_percent,
                            min_margin,
                            parsed.product_name,
                        )
                        continue

                    if not price_result.reliable:
                        logger.info(
                            "Prezzo non affidabile (<%d venduti) per %s, notifico comunque",
                            5,
                            parsed.product_name,
                        )

                    # Invia notifica Telegram
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
                        logger.exception("Errore invio notifica Telegram")
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
                        total_notified += 1

    logger.info(
        "Ciclo completato: %d nuove inserzioni, %d notifiche inviate",
        total_new,
        total_notified,
    )


async def main() -> None:
    """Loop principale di Deal Finder."""
    # Setup logger root
    setup_logger(level="INFO")
    logger.info("Deal Finder avviato")

    # Carica configurazione
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config.yaml non trovato. Copia config.yaml.example e configuralo.")
        sys.exit(1)

    # Inizializza database
    db = Database()
    await db.connect()

    # Inizializza scrapers attivi
    active_platforms = config.get("platforms", [])
    scrapers: dict[str, BaseScraper] = {}
    for platform in active_platforms:
        scraper_cls = _SCRAPER_REGISTRY.get(platform)
        if scraper_cls:
            scrapers[platform] = scraper_cls()
            logger.info("Scraper attivato: %s", platform)
        else:
            logger.warning("Scraper non trovato per piattaforma: %s", platform)

    if not scrapers:
        logger.error("Nessuno scraper attivo. Controlla config.yaml -> platforms")
        await db.close()
        sys.exit(1)

    # Inizializza analyzer
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
    )

    # Inizializza notifier
    notifier = TelegramNotifier()

    # Inizializza controller comandi Telegram
    bot = BotController(db)

    polling_interval = config.get("polling_interval", 300)
    logger.info(
        "Configurazione: %d piattaforme, %d categorie, polling ogni %ds",
        len(scrapers),
        len(config.get("categories", [])),
        polling_interval,
    )

    # Gestione segnali di arresto
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Avvia bot comandi Telegram
    try:
        await bot.start()
    except Exception:
        logger.exception("Errore avvio bot comandi (continuo senza comandi)")

    # Loop principale
    cycle_count = 0
    deals_found = 0
    try:
        while not _shutdown:
            # Se in pausa, aspetta senza fare scraping
            if bot.is_paused:
                logger.info("Bot in pausa, in attesa di /resume...")
                for _ in range(10):
                    if _shutdown or not bot.is_paused:
                        break
                    await asyncio.sleep(1)
                continue

            try:
                await run_cycle(config, db, scrapers, parser, price_checker, notifier, bot)
                cycle_count += 1
                bot.update_stats(cycle_count, deals_found)
            except Exception:
                logger.exception("Errore nel ciclo principale")

            # Pulizia periodica del DB
            try:
                await db.cleanup_old_records(days=30)
            except Exception:
                logger.exception("Errore pulizia DB")

            if not _shutdown:
                logger.info("Prossimo ciclo tra %d secondi", polling_interval)
                # Attendi con controllo periodico per shutdown e pausa
                for _ in range(polling_interval):
                    if _shutdown:
                        break
                    await asyncio.sleep(1)
    finally:
        await bot.stop()
        await db.close()
        logger.info("Deal Finder arrestato")


if __name__ == "__main__":
    asyncio.run(main())
