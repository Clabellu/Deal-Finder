"""Gestione comandi Telegram per controllare Deal Finder da telefono."""

import os
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from db.database import Database
from utils.logger import get_logger

logger = get_logger("bot_commands")


class BotController:
    """Riceve comandi Telegram e controlla il ciclo di scraping."""

    def __init__(self, db: Database):
        self._db = db
        self._paused = False
        self._started_at: datetime | None = None
        self._cycle_count = 0
        self._deals_found = 0
        self._app: Application | None = None

        self._token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    @property
    def is_paused(self) -> bool:
        return self._paused

    def update_stats(self, cycle_count: int, deals_found: int) -> None:
        """Aggiorna le statistiche dopo ogni ciclo."""
        self._cycle_count = cycle_count
        self._deals_found = deals_found

    async def start(self) -> None:
        """Avvia il polling per i comandi Telegram."""
        if not self._token:
            logger.warning("TELEGRAM_BOT_TOKEN non impostato, comandi disabilitati")
            return

        self._started_at = datetime.now(timezone.utc)

        self._app = (
            Application.builder()
            .token(self._token)
            .build()
        )

        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("stop", self._cmd_stop))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("deals", self._cmd_deals))
        self._app.add_handler(CommandHandler("help", self._cmd_help))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        logger.info("Bot comandi Telegram avviato")

    async def stop(self) -> None:
        """Ferma il polling comandi."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Bot comandi Telegram fermato")

    def _is_authorized(self, update: Update) -> bool:
        """Verifica che il messaggio arrivi dal chat_id autorizzato."""
        return str(update.effective_chat.id) == self._chat_id

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Riprende lo scanning (o messaggio di benvenuto)."""
        if not self._is_authorized(update):
            return

        self._paused = False
        await update.message.reply_html(
            "<b>Deal Finder attivo!</b>\n\n"
            "Sto monitorando i marketplace per te.\n"
            "Usa /help per vedere i comandi disponibili."
        )
        logger.info("Bot riattivato via comando /start")

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Mette in pausa lo scanning."""
        if not self._is_authorized(update):
            return

        self._paused = True
        await update.message.reply_html(
            "<b>Deal Finder in pausa.</b>\n\n"
            "Lo scanning e' sospeso. Usa /resume o /start per riprendere."
        )
        logger.info("Bot messo in pausa via comando /stop")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Riprende lo scanning dopo una pausa."""
        if not self._is_authorized(update):
            return

        if not self._paused:
            await update.message.reply_text("Il bot e' gia' attivo!")
            return

        self._paused = False
        await update.message.reply_html("<b>Deal Finder ripreso!</b>\nScanning riattivato.")
        logger.info("Bot ripreso via comando /resume")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Mostra lo stato attuale del bot."""
        if not self._is_authorized(update):
            return

        status = "IN PAUSA" if self._paused else "ATTIVO"
        uptime = ""
        if self._started_at:
            delta = datetime.now(timezone.utc) - self._started_at
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)
            uptime = f"{hours}h {minutes}m"

        # Statistiche dal DB
        try:
            stats = await self._db.get_stats()
        except Exception:
            stats = {"total_seen": 0, "total_notified": 0, "seen_today": 0, "notified_today": 0}

        text = (
            f"<b>Stato: {status}</b>\n\n"
            f"Uptime: {uptime}\n"
            f"Cicli completati: {self._cycle_count}\n\n"
            f"<b>Oggi:</b>\n"
            f"  Annunci analizzati: {stats['seen_today']}\n"
            f"  Deal trovati: {stats['notified_today']}\n\n"
            f"<b>Totale:</b>\n"
            f"  Annunci analizzati: {stats['total_seen']}\n"
            f"  Deal notificati: {stats['total_notified']}"
        )
        await update.message.reply_html(text)

    async def _cmd_deals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Mostra gli ultimi deal trovati."""
        if not self._is_authorized(update):
            return

        try:
            deals = await self._db.get_recent_notifications(limit=5)
        except Exception:
            await update.message.reply_text("Errore nel recupero dei deal.")
            return

        if not deals:
            await update.message.reply_text("Nessun deal trovato ancora. Continuo a cercare!")
            return

        lines = ["<b>Ultimi deal trovati:</b>\n"]
        for i, deal in enumerate(deals, 1):
            name = deal["product_name"] or "—"
            asked = deal["asked_price"] or 0
            market = deal["market_price"] or 0
            margin = deal["margin_percent"] or 0
            lines.append(
                f"{i}. <b>{name}</b>\n"
                f"   {asked:.0f}EUR (mercato {market:.0f}EUR, +{margin:.0f}%)"
            )

        await update.message.reply_html("\n".join(lines))

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Mostra i comandi disponibili."""
        if not self._is_authorized(update):
            return

        await update.message.reply_html(
            "<b>Comandi disponibili:</b>\n\n"
            "/start - Avvia/riprendi il monitoraggio\n"
            "/stop - Metti in pausa lo scanning\n"
            "/resume - Riprendi dopo una pausa\n"
            "/status - Stato del bot e statistiche\n"
            "/deals - Ultimi deal trovati\n"
            "/help - Mostra questo messaggio"
        )
