import os
from typing import Optional

import aiohttp

from utils.logger import get_logger

logger = get_logger("telegram")

_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    """Invia notifiche formattate su Telegram."""

    def __init__(self):
        self._token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

        if not self._token or not self._chat_id:
            logger.warning(
                "TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID non impostati: "
                "le notifiche Telegram non funzioneranno"
            )

    async def send_deal(
        self,
        product_name: str,
        asked_price: float,
        median_price: float,
        margin: float,
        margin_percent: float,
        min_price: float,
        max_price: float,
        sold_count: int,
        key_details: str,
        location: Optional[str],
        platform: str,
        url: str,
        image_url: Optional[str] = None,
        include_image: bool = True,
    ) -> bool:
        """Invia una notifica di deal su Telegram.

        Returns:
            True se l'invio ha avuto successo.
        """
        if not self._token or not self._chat_id:
            logger.warning("Telegram non configurato, notifica non inviata")
            return False

        text = self._format_message(
            product_name=product_name,
            asked_price=asked_price,
            median_price=median_price,
            margin=margin,
            margin_percent=margin_percent,
            min_price=min_price,
            max_price=max_price,
            sold_count=sold_count,
            key_details=key_details,
            location=location,
            platform=platform,
            url=url,
        )

        # Pulsante inline "Apri annuncio"
        inline_keyboard = {
            "inline_keyboard": [
                [{"text": "Apri annuncio", "url": url}]
            ]
        }

        # Se c'e' un'immagine, invia come foto con caption
        if include_image and image_url:
            success = await self._send_photo(image_url, text, inline_keyboard)
            if success:
                return True
            # Fallback a messaggio di testo se invio foto fallisce
            logger.debug("Fallback a messaggio di testo (invio foto fallito)")

        return await self._send_message(text, inline_keyboard)

    async def _send_message(self, text: str, reply_markup: dict) -> bool:
        """Invia un messaggio di testo su Telegram."""
        url = _TELEGRAM_API.format(token=self._token, method="sendMessage")
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "reply_markup": reply_markup,
        }
        return await self._post(url, payload)

    async def _send_photo(self, photo_url: str, caption: str, reply_markup: dict) -> bool:
        """Invia una foto con caption su Telegram."""
        url = _TELEGRAM_API.format(token=self._token, method="sendPhoto")
        # Tronca caption a 1024 caratteri (limite Telegram per foto)
        if len(caption) > 1024:
            caption = caption[:1021] + "..."
        payload = {
            "chat_id": self._chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
        }
        return await self._post(url, payload)

    async def _post(self, url: str, payload: dict) -> bool:
        """Esegue una POST verso l'API Telegram."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        logger.info("Notifica Telegram inviata con successo")
                        return True
                    body = await resp.text()
                    logger.error(
                        "Errore Telegram API: HTTP %d - %s", resp.status, body[:300]
                    )
                    return False
        except Exception:
            logger.exception("Errore invio notifica Telegram")
            return False

    @staticmethod
    def _format_message(
        product_name: str,
        asked_price: float,
        median_price: float,
        margin: float,
        margin_percent: float,
        min_price: float,
        max_price: float,
        sold_count: int,
        key_details: str,
        location: Optional[str],
        platform: str,
        url: str,
    ) -> str:
        """Formatta il messaggio di notifica."""
        lines = [
            f"<b>{product_name}</b>",
            "",
            f"Prezzo chiesto: <b>{asked_price:.0f} EUR</b>",
            f"Prezzo medio mercato: <b>{median_price:.0f} EUR</b>",
            f"Margine stimato: <b>{margin:.0f} EUR ({margin_percent:.0f}%)</b>",
            f"Range mercato: {min_price:.0f} - {max_price:.0f} EUR ({sold_count} venduti)",
            "",
        ]
        if key_details:
            lines.append(f"{key_details}")
        if location:
            lines.append(f"Luogo: {location}")
        lines.append(f"Piattaforma: {platform.capitalize()}")
        lines.append("")
        lines.append(f'<a href="{url}">Vedi annuncio</a>')

        return "\n".join(lines)
