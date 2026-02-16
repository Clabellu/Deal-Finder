"""Debug: scarica una pagina Subito.it e analizza la struttura HTML/JSON."""

import asyncio
import json
import re

from bs4 import BeautifulSoup
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

        # ── ANALISI __NEXT_DATA__ ──
        print("\n3. Analisi __NEXT_DATA__:")
        match = re.search(
            r'<script\s+id="__NEXT_DATA__"\s+type="application/json"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if not match:
            print("   [MANCANTE] __NEXT_DATA__ non trovato")
            return

        print(f"   Trovato: {len(match.group(1))} chars")
        data = json.loads(match.group(1))

        # Naviga in initialState
        pp = data.get("props", {}).get("pageProps", {})
        initial_state = pp.get("initialState", {})
        print(f"\n   initialState keys: {list(initial_state.keys())}")

        # Esplora ogni chiave di initialState
        for key, value in initial_state.items():
            if isinstance(value, dict):
                print(f"\n   initialState.{key}: dict con {len(value)} chiavi")
                print(f"     chiavi: {list(value.keys())[:15]}")
                # Cerca liste che sembrano annunci
                for k2, v2 in value.items():
                    if isinstance(v2, list) and len(v2) > 0:
                        print(f"     .{k2}: lista con {len(v2)} elementi")
                        if isinstance(v2[0], dict):
                            print(f"       primo elemento keys: {list(v2[0].keys())[:15]}")
                            # Mostra un annuncio di esempio
                            sample = v2[0]
                            for sk in ["subject", "title", "name", "urn", "id", "url", "price"]:
                                if sk in sample:
                                    val = sample[sk]
                                    if isinstance(val, str) and len(val) > 100:
                                        val = val[:100] + "..."
                                    print(f"       .{sk} = {val}")
                    elif isinstance(v2, dict):
                        inner_lists = {k3: len(v3) for k3, v3 in v2.items() if isinstance(v3, list) and len(v3) > 2}
                        if inner_lists:
                            print(f"     .{k2}: dict con liste: {inner_lists}")
                            # Esplora la prima lista grande
                            for k3, v3 in v2.items():
                                if isinstance(v3, list) and len(v3) > 2:
                                    if isinstance(v3[0], dict):
                                        print(f"       .{k2}.{k3}[0] keys: {list(v3[0].keys())[:15]}")
                                        sample = v3[0]
                                        for sk in ["subject", "title", "name", "urn", "id", "url", "price", "features"]:
                                            if sk in sample:
                                                val = sample[sk]
                                                if isinstance(val, str) and len(val) > 100:
                                                    val = val[:100] + "..."
                                                print(f"         .{sk} = {val}")
                                    break
            elif isinstance(value, list) and len(value) > 0:
                print(f"\n   initialState.{key}: lista con {len(value)} elementi")
                if isinstance(value[0], dict):
                    print(f"     primo elemento keys: {list(value[0].keys())[:15]}")

        # Salva initialState come JSON leggibile
        with open("debug_initialState.json", "w", encoding="utf-8") as f:
            json.dump(initial_state, f, indent=2, ensure_ascii=False)
        print("\n   initialState salvato in debug_initialState.json")

        # ── TEST HTML PARSER ──
        print("\n4. Test HTML parser (BeautifulSoup):")
        soup = BeautifulSoup(html, "html.parser")
        links = soup.find_all("a", href=re.compile(r"subito\.it/.+\.htm"))
        print(f"   Link trovati da BS4: {len(links)}")

        # Prova a parsare i primi 3 link
        for i, link in enumerate(links[:5]):
            href = link.get("href", "")
            id_match = re.search(r"/(\d+)\.htm", href)
            if not id_match:
                continue

            print(f"\n   [{i}] {href[:80]}...")
            print(f"       ID: {id_match.group(1)}")

            # Titolo
            title = ""
            for tag in link.find_all(["h2", "h3", "span", "p"]):
                text = tag.get_text(strip=True)
                if len(text) > 10:
                    title = text
                    break
            if not title:
                title = link.get_text(strip=True)[:120]
            print(f"       Titolo: '{title[:80]}'")

            # Prezzo dentro al link
            price_in_link = ""
            for tag in link.find_all(["span", "p"], string=re.compile(r"[\d.,]+\s*€|€\s*[\d.,]+")):
                price_in_link = tag.get_text()
                break
            print(f"       Prezzo in link: '{price_in_link}'")

            # Prezzo nel parent
            price_in_parent = ""
            parent = link.parent
            if parent:
                for tag in parent.find_all(["span", "p"], string=re.compile(r"[\d.,]+\s*€|€\s*[\d.,]+")):
                    price_in_parent = tag.get_text()
                    break
            print(f"       Prezzo in parent: '{price_in_parent}'")

            # Mostra HTML del link (primi 500 chars)
            link_html = str(link)
            print(f"       HTML ({len(link_html)} chars): {link_html[:300]}...")


if __name__ == "__main__":
    asyncio.run(main())
