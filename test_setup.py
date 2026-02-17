"""Script di test per verificare la configurazione di Deal Finder.

Testa: Telegram, Anthropic/Claude, Scraper Subito, Database.
Non richiede le API key di eBay.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

PASS = "[OK]"
FAIL = "[ERRORE]"


async def test_telegram():
    """Verifica connessione al bot Telegram e invio messaggio."""
    print("\n--- Test Telegram ---")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print(f"{FAIL} TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID mancanti nel .env")
        return False

    import aiohttp

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "Deal Finder - Test connessione riuscito!",
        "parse_mode": "HTML",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    print(f"{PASS} Messaggio di test inviato su Telegram!")
                    print("     Controlla il tuo bot su Telegram, dovresti vedere il messaggio.")
                    return True
                body = await resp.text()
                print(f"{FAIL} Telegram API errore HTTP {resp.status}: {body[:200]}")
                return False
    except Exception as e:
        print(f"{FAIL} Errore connessione Telegram: {e}")
        return False


async def test_anthropic():
    """Verifica connessione API Anthropic con una richiesta minimale."""
    print("\n--- Test Anthropic/Claude ---")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key:
        print(f"{FAIL} ANTHROPIC_API_KEY mancante nel .env")
        return False

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            messages=[{"role": "user", "content": "Rispondi solo: OK"}],
        )
        text = response.content[0].text.strip()
        print(f"{PASS} Claude ha risposto: \"{text}\"")
        print(f"     Modello usato: {response.model}")
        tokens_in = response.usage.input_tokens
        tokens_out = response.usage.output_tokens
        print(f"     Token usati: {tokens_in} input + {tokens_out} output")
        return True
    except anthropic.AuthenticationError:
        print(f"{FAIL} API key non valida. Controlla ANTHROPIC_API_KEY nel .env")
        return False
    except anthropic.APIError as e:
        print(f"{FAIL} Errore API Anthropic: {e}")
        return False
    except Exception as e:
        print(f"{FAIL} Errore inatteso: {e}")
        return False


async def test_database():
    """Verifica che il database SQLite funzioni."""
    print("\n--- Test Database ---")
    try:
        from db.database import Database

        db = Database()
        await db.connect()
        # Test write/read
        await db.mark_seen("test_123", "test_platform")
        is_seen = await db.is_seen("test_123", "test_platform")
        await db.close()

        if is_seen:
            print(f"{PASS} Database SQLite funzionante (write + read OK)")
            return True
        else:
            print(f"{FAIL} Database: scrittura OK ma lettura fallita")
            return False
    except Exception as e:
        print(f"{FAIL} Errore database: {e}")
        return False


async def test_subito_scraper():
    """Verifica che lo scraper di Subito.it riesca a fare una ricerca."""
    print("\n--- Test Scraper Subito.it ---")
    try:
        from scrapers.subito import SubitoScraper

        scraper = SubitoScraper()
        listings = await scraper.search(
            keyword="iphone 15",
            min_price=400,
            max_price=800,
            category_name="Smartphone",
        )
        print(f"{PASS} Scraper Subito (API JSON): trovati {len(listings)} annunci")
        for i, l in enumerate(listings[:3]):
            loc = l.location or "N/A"
            print(f"     {i+1}. \"{l.title}\" - {l.price}EUR ({loc})")
        if not listings:
            print("     (nessun risultato, ma la connessione funziona)")
        return True
    except Exception as e:
        print(f"{FAIL} Errore scraper Subito: {e}")
        return False


async def test_ebay():
    """Verifica che lo scraper eBay riesca a cercare tramite Finding API."""
    print("\n--- Test eBay Scraper ---")
    app_id = os.environ.get("EBAY_APP_ID", "")
    if not app_id:
        print("[SKIP] EBAY_APP_ID non configurato")
        return None

    try:
        from scrapers.ebay import EbayScraper

        scraper = EbayScraper()
        listings = await scraper.search(
            keyword="iphone 15",
            min_price=400,
            max_price=800,
            category_name="Smartphone",
        )
        print(f"{PASS} Scraper eBay (Finding API): trovati {len(listings)} annunci")
        for i, l in enumerate(listings[:3]):
            loc = l.location or "N/A"
            print(f"     {i+1}. \"{l.title}\" - {l.price}EUR ({loc})")
        if not listings:
            print("     (nessun risultato, ma la connessione funziona)")
        return True
    except Exception as e:
        print(f"{FAIL} Errore scraper eBay: {e}")
        return False


async def main():
    print("=" * 50)
    print("  DEAL FINDER - Test Configurazione")
    print("=" * 50)

    results = {}
    results["Database"] = await test_database()
    results["Telegram"] = await test_telegram()
    results["Anthropic"] = await test_anthropic()
    results["Subito Scraper"] = await test_subito_scraper()
    results["eBay"] = await test_ebay()

    print("\n" + "=" * 50)
    print("  RIEPILOGO")
    print("=" * 50)
    all_ok = True
    for name, result in results.items():
        if result is None:
            status = "[SKIP]"
        elif result:
            status = PASS
        else:
            status = FAIL
            all_ok = False
        print(f"  {status} {name}")

    print()
    if all_ok:
        print("Tutto funziona! Quando avrai le API key eBay, riesegui questo test.")
    else:
        print("Correggi gli errori sopra e riesegui: python test_setup.py")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
