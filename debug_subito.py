"""Debug: scarica una pagina Subito.it e analizza la struttura HTML."""

import asyncio
import json
import re

from curl_cffi.requests import AsyncSession


_SEARCH_URL = "https://www.subito.it/annunci-italia/vendita/usato/"
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
    url = f"{_SEARCH_URL}?q=iphone+15&ps=400&pe=800&o=1"

    async with AsyncSession(
        headers=_HEADERS,
        impersonate="chrome131",
        timeout=30,
    ) as session:
        # Warm-up
        print("1. Warm-up homepage...")
        resp = await session.get("https://www.subito.it/")
        print(f"   Homepage: HTTP {resp.status_code}, {len(resp.text)} chars")

        print(f"\n2. Ricerca: {url}")
        resp = await session.get(url)
        print(f"   Risposta: HTTP {resp.status_code}, {len(resp.text)} chars")

        html = resp.text

        # Salva HTML per ispezione
        with open("debug_subito.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("   HTML salvato in debug_subito.html")

        # Analisi struttura
        print("\n3. Analisi struttura HTML:")

        # Check __NEXT_DATA__
        match = re.search(
            r'<script\s+id="__NEXT_DATA__"\s+type="application/json"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if match:
            print(f"   [TROVATO] __NEXT_DATA__: {len(match.group(1))} chars")
            try:
                data = json.loads(match.group(1))
                # Cerca "ads" ricorsivamente
                ads = _dig_for_key(data, "ads")
                if ads is not None:
                    print(f"   [TROVATO] ads array: {len(ads)} elementi")
                    if ads:
                        print(f"   Primo annuncio keys: {list(ads[0].keys()) if isinstance(ads[0], dict) else type(ads[0])}")
                else:
                    print("   [MANCANTE] nessun array 'ads' nel JSON")
                    # Mostra le chiavi top-level
                    print(f"   Chiavi top-level: {list(data.keys())}")
                    if "props" in data:
                        props = data["props"]
                        print(f"   props keys: {list(props.keys()) if isinstance(props, dict) else type(props)}")
                        if "pageProps" in props:
                            pp = props["pageProps"]
                            print(f"   pageProps keys: {list(pp.keys()) if isinstance(pp, dict) else type(pp)}")
                            # Cerca tutte le chiavi che contengono "ad" o "list" o "item"
                            interesting = [k for k in pp.keys() if any(w in k.lower() for w in ["ad", "list", "item", "result", "search"])]
                            if interesting:
                                print(f"   Chiavi interessanti in pageProps: {interesting}")
                                for k in interesting:
                                    v = pp[k]
                                    if isinstance(v, list):
                                        print(f"     {k}: lista con {len(v)} elementi")
                                    elif isinstance(v, dict):
                                        print(f"     {k}: dict con chiavi {list(v.keys())[:10]}")
                                    else:
                                        print(f"     {k}: {type(v).__name__}")
            except json.JSONDecodeError as e:
                print(f"   [ERRORE] JSON non valido: {e}")
        else:
            print("   [MANCANTE] __NEXT_DATA__ non trovato")

        # Check JSON-LD
        ld_matches = re.findall(
            r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL
        )
        print(f"\n   JSON-LD trovati: {len(ld_matches)}")
        for i, m in enumerate(ld_matches):
            try:
                ld = json.loads(m)
                t = ld.get("@type", "unknown") if isinstance(ld, dict) else type(ld).__name__
                print(f"   [{i}] @type={t}, keys={list(ld.keys())[:8] if isinstance(ld, dict) else 'N/A'}")
            except json.JSONDecodeError:
                print(f"   [{i}] JSON non valido")

        # Check script tags con "ads"
        scripts_with_ads = []
        for m in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
            if '"ads"' in m.group(1):
                scripts_with_ads.append(m.group(1)[:200])
        print(f"\n   Script con 'ads': {len(scripts_with_ads)}")
        for i, s in enumerate(scripts_with_ads[:3]):
            print(f"   [{i}] {s[:150]}...")

        # Check links to .htm
        htm_links = re.findall(r'href="[^"]*subito\.it/[^"]*\.htm"', html)
        print(f"\n   Link .htm trovati: {len(htm_links)}")
        for link in htm_links[:5]:
            print(f"   {link[:120]}")

        # Check per pattern di prezzo
        prices = re.findall(r'[\d.,]+\s*€|€\s*[\d.,]+', html)
        print(f"\n   Pattern prezzo trovati: {len(prices)}")
        for p in prices[:5]:
            print(f"   {p}")


def _dig_for_key(data, key, depth=0):
    """Cerca ricorsivamente una chiave nel JSON."""
    if depth > 10:
        return None
    if isinstance(data, dict):
        if key in data and isinstance(data[key], list):
            return data[key]
        for v in data.values():
            result = _dig_for_key(v, key, depth + 1)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = _dig_for_key(item, key, depth + 1)
            if result is not None:
                return result
    return None


if __name__ == "__main__":
    asyncio.run(main())
