"""Debug: simula _parse_ad_json su ogni annuncio per trovare dove fallisce."""

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


def extract_price_from_features(features):
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


async def main():
    url = f"{_SEARCH_URL}?q=iphone+15&ps=400&pe=800&o=1"

    async with AsyncSession(
        headers=_HEADERS, impersonate="chrome131", timeout=30,
    ) as session:
        print("1. Warm-up...")
        await session.get("https://www.subito.it/")

        print(f"2. Ricerca: {url}")
        resp = await session.get(url)
        html = resp.text
        print(f"   HTTP {resp.status_code}, {len(html)} chars")

        match = re.search(
            r'<script\s+id="__NEXT_DATA__"\s+type="application/json"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if not match:
            print("   __NEXT_DATA__ non trovato!")
            return

        data = json.loads(match.group(1))
        items = (
            data.get("props", {})
            .get("pageProps", {})
            .get("initialState", {})
            .get("items", {})
        )
        original_list = items.get("originalList", [])
        print(f"\n3. originalList: {len(original_list)} annunci")

        if not original_list:
            print("   VUOTO! Provo galleryList...")
            original_list = items.get("galleryList", [])
            print(f"   galleryList: {len(original_list)} annunci")

        if not original_list:
            print("   Nessun annuncio trovato!")
            return

        # Simula _parse_ad_json per i primi 3 annunci
        for i, ad in enumerate(original_list[:3]):
            print(f"\n{'='*60}")
            print(f"ANNUNCIO {i+1}:")
            print(f"  keys: {list(ad.keys())}")

            # URN
            urn = ad.get("urn", "")
            listing_id = urn.split(":")[-1] if urn else str(ad.get("id", ""))
            print(f"  urn: {urn}")
            print(f"  listing_id: {listing_id}")
            if not listing_id:
                print("  >>> SCARTATO: no listing_id")
                continue

            # Titolo
            title = ad.get("subject", ad.get("title", ""))
            print(f"  subject: {title}")
            if not title or not title.strip():
                print("  >>> SCARTATO: no title")
                continue

            # Features (prezzo)
            features = ad.get("features", [])
            print(f"  features: {json.dumps(features, ensure_ascii=False)[:500]}")
            price = extract_price_from_features(features) if features else None
            print(f"  prezzo da features: {price}")

            # Prezzo fallback
            if price is None:
                price_data = ad.get("price", {})
                print(f"  price field: {price_data}")
                if isinstance(price_data, dict):
                    price = price_data.get("value") or price_data.get("amount")
                elif isinstance(price_data, (int, float)):
                    price = float(price_data)
                print(f"  prezzo da fallback: {price}")

            if price is None:
                print("  >>> SCARTATO: no price")
                continue

            # URLs
            urls = ad.get("urls", {})
            print(f"  urls: {json.dumps(urls, ensure_ascii=False)[:300]}")
            url_val = urls.get("default", ad.get("url", ""))
            print(f"  url finale: {url_val}")
            if not url_val:
                print("  >>> SCARTATO: no url")
                continue

            # Immagini
            images = ad.get("images", [])
            print(f"  images[0]: {json.dumps(images[0] if images else {}, ensure_ascii=False)[:300]}")

            # Geo
            geo = ad.get("geo", {})
            print(f"  geo: {json.dumps(geo, ensure_ascii=False)[:300]}")

            # Date
            date_field = ad.get("date", ad.get("dates", {}))
            print(f"  date: {json.dumps(date_field, ensure_ascii=False)[:200]}")

            print(f"  >>> OK! Titolo='{title}', Prezzo={price}")


if __name__ == "__main__":
    asyncio.run(main())
