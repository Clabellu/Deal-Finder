from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Listing:
    """Rappresenta un'inserzione trovata su un marketplace."""

    id: str                         # ID univoco (platform_id)
    platform: str                   # "subito", "vinted", "wallapop", "ebay"
    title: str                      # Titolo originale dell'inserzione
    description: str                # Descrizione completa
    price: float                    # Prezzo in EUR
    currency: str                   # "EUR"
    url: str                        # Link diretto all'inserzione
    image_url: Optional[str]        # URL della prima immagine
    location: Optional[str]         # Citta'/zona del venditore
    category_matched: str           # Quale categoria di config ha matchato
    timestamp: str                  # Quando e' stata pubblicata (ISO format)
    raw_data: Optional[dict] = field(default=None, repr=False)


class BaseScraper(ABC):
    """Classe astratta per tutti gli scraper di marketplace."""

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Nome della piattaforma (es. 'subito')."""
        ...

    @abstractmethod
    async def search(
        self,
        keyword: str,
        min_price: float,
        max_price: float,
        category_name: str,
    ) -> list[Listing]:
        """Cerca inserzioni per keyword e range di prezzo.

        Args:
            keyword: Termine di ricerca.
            min_price: Prezzo minimo EUR.
            max_price: Prezzo massimo EUR.
            category_name: Nome della categoria che ha generato la ricerca.

        Returns:
            Lista di Listing trovate.
        """
        ...
