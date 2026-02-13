import aiosqlite
from datetime import datetime, timedelta

from utils.logger import get_logger

logger = get_logger("database")

DB_PATH = "deal_finder.db"


class Database:
    """Gestisce il database SQLite per il tracking delle inserzioni."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Apre la connessione e crea le tabelle se non esistono."""
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._create_tables()
        logger.info("Database connesso: %s", self.db_path)

    async def close(self) -> None:
        """Chiude la connessione al database."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("Database chiuso")

    async def _create_tables(self) -> None:
        assert self._db is not None
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen_listings (
                id TEXT NOT NULL,
                platform TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (id, platform)
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                product_name TEXT,
                asked_price REAL,
                market_price REAL,
                margin_percent REAL,
                notified_at TEXT NOT NULL
            );
            """
        )
        await self._db.commit()

    async def is_seen(self, listing_id: str, platform: str) -> bool:
        """Controlla se un'inserzione e' gia' stata vista."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT 1 FROM seen_listings WHERE id = ? AND platform = ?",
            (listing_id, platform),
        )
        row = await cursor.fetchone()
        return row is not None

    async def mark_seen(self, listing_id: str, platform: str, notified: bool = False) -> None:
        """Segna un'inserzione come vista."""
        assert self._db is not None
        await self._db.execute(
            """INSERT OR IGNORE INTO seen_listings (id, platform, first_seen, notified)
               VALUES (?, ?, ?, ?)""",
            (listing_id, platform, datetime.utcnow().isoformat(), int(notified)),
        )
        await self._db.commit()

    async def save_notification(
        self,
        listing_id: str,
        platform: str,
        product_name: str,
        asked_price: float,
        market_price: float,
        margin_percent: float,
    ) -> None:
        """Salva una notifica inviata nello storico."""
        assert self._db is not None
        await self._db.execute(
            """INSERT INTO notifications
               (listing_id, platform, product_name, asked_price, market_price, margin_percent, notified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                listing_id,
                platform,
                product_name,
                asked_price,
                market_price,
                margin_percent,
                datetime.utcnow().isoformat(),
            ),
        )
        await self._db.commit()

    async def cleanup_old_records(self, days: int = 30) -> int:
        """Rimuove record piu' vecchi di N giorni. Restituisce il numero di righe eliminate."""
        assert self._db is not None
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

        cursor = await self._db.execute(
            "DELETE FROM seen_listings WHERE first_seen < ?", (cutoff,)
        )
        deleted_seen = cursor.rowcount

        cursor = await self._db.execute(
            "DELETE FROM notifications WHERE notified_at < ?", (cutoff,)
        )
        deleted_notif = cursor.rowcount

        await self._db.commit()
        total = deleted_seen + deleted_notif
        if total > 0:
            logger.info(
                "Pulizia DB: rimossi %d seen_listings, %d notifications",
                deleted_seen,
                deleted_notif,
            )
        return total
