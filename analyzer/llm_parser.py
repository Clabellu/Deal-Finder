import json
import os
from dataclasses import dataclass
from typing import Optional

import anthropic

from scrapers.base_scraper import Listing
from utils.logger import get_logger

logger = get_logger("llm_parser")

_SYSTEM_PROMPT = "Sei un esperto di prodotti usati e marketplace. Rispondi SOLO con JSON valido."

_USER_PROMPT_TEMPLATE = """\
Analizza questa inserzione di un marketplace e estrai le informazioni del prodotto.
Rispondi SOLO con un JSON valido, nessun altro testo.

Titolo: {title}
Descrizione: {description}
Prezzo richiesto: {price}€
Categoria: {category}

Rispondi con questo JSON:
{{
  "product_name": "nome completo del prodotto",
  "brand": "marca",
  "model": "modello specifico",
  "variant": "variante (es. colore, storage, taglia)",
  "condition": "nuovo/come_nuovo/usato_buono/usato_discreto/ricambi",
  "ebay_search_query": "marca modello variante (2-5 parole)",
  "key_details": "dettagli rilevanti per il valore (es. batteria, accessori, difetti)",
  "confidence": "alta/media/bassa",
  "skip_reason": null
}}

Regole:
- "ebay_search_query" deve contenere SOLO marca + modello + variante essenziale (es. storage, colore)
- "ebay_search_query" NON deve MAI contenere parole come: usato, nuovo, come nuovo, ottime condizioni, ricondizionato, spedizione, promo, offerta
- "ebay_search_query" deve essere corta: 2-5 parole massimo. Esempi corretti: "Samsung Galaxy S22 256GB", "iPhone 14 Pro 128GB", "PS5 Digital"
- Se l'inserzione e' per ricambi, lotti, o non e' un prodotto rivendibile, imposta skip_reason con il motivo
- "confidence" e' bassa se il titolo/descrizione sono troppo vaghi per identificare il prodotto
- Sii specifico con modello e variante quando possibile"""


@dataclass
class ParsedProduct:
    """Risultato del parsing LLM di un'inserzione."""

    product_name: str
    brand: str
    model: str
    variant: str
    condition: str
    ebay_search_query: str
    key_details: str
    confidence: str
    skip_reason: Optional[str]


class LLMParser:
    """Usa Claude API per estrarre informazioni strutturate dalle inserzioni."""

    def __init__(self, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 500, temperature: float = 0):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY non impostata nell'ambiente")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def parse_listing(self, listing: Listing) -> Optional[ParsedProduct]:
        """Analizza un'inserzione e restituisce il prodotto parsato.

        Returns:
            ParsedProduct se l'analisi ha successo e il prodotto e' valido,
            None se il parsing fallisce, la confidence e' bassa o c'e' un skip_reason.
        """
        prompt = _USER_PROMPT_TEMPLATE.format(
            title=listing.title,
            description=listing.description or "(nessuna descrizione)",
            price=listing.price,
            category=listing.category_matched,
        )

        try:
            # L'SDK Anthropic e' sincrono, lo usiamo direttamente
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            raw_text = response.content[0].text.strip()
            data = self._extract_json(raw_text)
            if data is None:
                logger.warning(
                    "Risposta LLM non e' JSON valido per listing %s: %s",
                    listing.id,
                    raw_text[:200],
                )
                return None

            parsed = ParsedProduct(
                product_name=data.get("product_name", ""),
                brand=data.get("brand", ""),
                model=data.get("model", ""),
                variant=data.get("variant", ""),
                condition=data.get("condition", ""),
                ebay_search_query=data.get("ebay_search_query", ""),
                key_details=data.get("key_details", ""),
                confidence=data.get("confidence", "bassa"),
                skip_reason=data.get("skip_reason"),
            )

            # Salta se confidence bassa o c'e' un motivo di skip
            if parsed.confidence == "bassa":
                logger.debug(
                    "Skipping listing %s: confidence bassa", listing.id
                )
                return None
            if parsed.skip_reason:
                logger.debug(
                    "Skipping listing %s: %s", listing.id, parsed.skip_reason
                )
                return None

            logger.info(
                "Parsed: %s -> %s (%s) [confidence: %s]",
                listing.title[:50],
                parsed.product_name,
                parsed.ebay_search_query,
                parsed.confidence,
            )
            return parsed

        except anthropic.APIError as e:
            logger.error("Errore API Anthropic per listing %s: %s", listing.id, e)
            return None
        except Exception:
            logger.exception("Errore inatteso nel parsing listing %s", listing.id)
            return None

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        """Estrae un oggetto JSON dal testo, gestendo eventuali wrapper markdown."""
        # Prova parsing diretto
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Prova a estrarre da blocco ```json ... ```
        import re
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Prova a trovare il primo { ... } nel testo
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass

        return None
