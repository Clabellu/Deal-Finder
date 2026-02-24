"""Script diagnostico per verificare cosa restituisce eBay.it.

Esegui dal tuo PC:
    python debug_ebay.py

Salva l'HTML in debug_ebay_output.html e stampa indicatori chiave.
"""

import asyncio
import json
import re
import sys

from curl_cffi.requests import AsyncSession

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


async def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "iPhone 11 Pro 128GB"
    print(f"=== Debug eBay.it scraping per: '{query}' ===\n")

    async with AsyncSession(
        headers=_HEADERS, impersonate="chrome131", timeout=20
    ) as session:
        # 1. Warm-up homepage
        print("[1] Warm-up homepage ebay.it...")
        try:
            r0 = await session.get("https://www.ebay.it/")
            print(f"    HTTP {r0.status_code}, lunghezza={len(r0.text)}")
            # Mostra cookies ricevuti
            cookies = dict(session.cookies)
            print(f"    Cookies ricevuti: {list(cookies.keys())}")
        except Exception as e:
            print(f"    ERRORE warm-up: {e}")

        await asyncio.sleep(2)

        # 2. Accetta cookie consent se necessario
        print("\n[2] Tentativo accettazione cookie consent...")
        try:
            consent_url = "https://www.ebay.it/gdpr"
            r_consent = await session.post(
                "https://www.ebay.it/api/cmp/accept",
                headers={"Referer": "https://www.ebay.it/"},
            )
            print(f"    Consent POST: HTTP {r_consent.status_code}")
        except Exception as e:
            print(f"    Consent non disponibile (normale): {e}")

        await asyncio.sleep(1)

        # 3. Ricerca venduti
        print(f"\n[3] Ricerca venduti per '{query}'...")
        params = {
            "_nkw": query,
            "LH_Complete": "1",
            "LH_Sold": "1",
            "_sop": "13",
            "rt": "nc",
            "_ipg": "60",
        }
        try:
            r = await session.get(
                "https://www.ebay.it/sch/i.html", params=params
            )
            html = r.text
            print(f"    HTTP {r.status_code}, lunghezza={len(html)}")
            print(f"    URL finale: {r.url}")
        except Exception as e:
            print(f"    ERRORE ricerca: {e}")
            return

        # 4. Analisi HTML
        print("\n[4] Analisi contenuto HTML...")
        indicators = {
            "s-item": "s-item" in html,
            "s-item__price": "s-item__price" in html,
            "srp-results": "srp-results" in html,
            "ld+json": "ld+json" in html,
            "consent/gdpr": "consent" in html.lower() or "gdpr" in html.lower(),
            "captcha": "captcha" in html.lower(),
            '"price"': '"price"' in html,
            '"prc"': '"prc"' in html,
            "EUR": "EUR" in html,
            "RESULTS_COUNT": "SEARCH_RESULTS_COUNT" in html or "resultCount" in html,
        }
        for key, found in indicators.items():
            status = "SI" if found else "NO"
            print(f"    {key}: {status}")

        # 5. Titolo pagina
        title_match = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
        if title_match:
            print(f"\n    Titolo pagina: {title_match.group(1).strip()[:200]}")

        # 6. Conta s-item
        s_item_count = html.count('class="s-item')
        print(f"    Elementi s-item trovati: {s_item_count}")

        # 7. Controlla JSON-LD
        ld_json_count = html.count("application/ld+json")
        print(f"    Blocchi JSON-LD: {ld_json_count}")

        # 8. Cerca prezzi con regex
        price_pattern = re.compile(r"EUR\s*[\d.,]+|[\d.,]+\s*EUR|[\d.,]+\s*€|€\s*[\d.,]+")
        price_matches = price_pattern.findall(html)
        print(f"    Prezzi trovati con regex: {len(price_matches)}")
        if price_matches:
            print(f"    Primi 5: {price_matches[:5]}")

        # 9. Salva HTML
        output_file = "debug_ebay_output.html"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n[5] HTML salvato in: {output_file}")

        # 10. Primi 1000 caratteri
        print("\n[6] Primi 1000 caratteri dell'HTML:")
        print("-" * 60)
        print(html[:1000])
        print("-" * 60)

        # 11. Prova anche ricerca attiva (non venduti)
        print(f"\n[7] Ricerca inserzioni ATTIVE per '{query}'...")
        params_active = {
            "_nkw": query,
            "LH_BIN": "1",
            "_sop": "12",
            "rt": "nc",
            "_ipg": "60",
        }
        try:
            r2 = await session.get(
                "https://www.ebay.it/sch/i.html", params=params_active
            )
            html2 = r2.text
            print(f"    HTTP {r2.status_code}, lunghezza={len(html2)}")
            s_item_count2 = html2.count('class="s-item')
            print(f"    Elementi s-item: {s_item_count2}")
            price_matches2 = price_pattern.findall(html2)
            print(f"    Prezzi regex: {len(price_matches2)}")
            if price_matches2:
                print(f"    Primi 5: {price_matches2[:5]}")

            with open("debug_ebay_active.html", "w", encoding="utf-8") as f:
                f.write(html2)
            print(f"    HTML salvato in: debug_ebay_active.html")
        except Exception as e:
            print(f"    ERRORE: {e}")

    print("\n=== DONE ===")
    print("Condividi l'output di questo script per diagnosticare il problema.")


if __name__ == "__main__":
    asyncio.run(main())
