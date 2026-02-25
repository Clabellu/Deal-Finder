"""Deal Finder — GUI desktop con CustomTkinter."""

import asyncio
import os
import threading
import tkinter as tk
import webbrowser
from datetime import datetime, timezone
from tkinter import messagebox

import yaml
from dotenv import load_dotenv

import customtkinter as ctk

from db.database import Database
from gui.engine import MonitorEngine
from utils.logger import setup_logger, get_logger

load_dotenv()
setup_logger(level="INFO")
logger = get_logger("gui")

CONFIG_PATH = "config.yaml"

# Tema
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


class DealFinderApp(ctk.CTk):
    """Finestra principale dell'applicazione Deal Finder."""

    def __init__(self):
        super().__init__()
        self.title("Deal Finder")
        self.geometry("960x640")
        self.minsize(800, 500)

        self.engine = MonitorEngine()
        self.engine.on_status_change = self._on_engine_status
        self.engine.on_cycle_complete = self._on_cycle_complete
        self.engine.on_deal_found = self._on_deal_found
        self.engine.on_listing_analyzed = self._on_listing_analyzed
        self.engine.on_log = self._on_engine_log

        self._build_layout()
        self._show_frame("dashboard")

        # Aggiorna stats dal DB all'avvio
        self.after(500, self._load_initial_stats)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Layout ──────────────────────────────────────────────

    def _build_layout(self):
        # Sidebar
        self.sidebar = ctk.CTkFrame(self, width=180, corner_radius=0)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        # Logo
        self.logo_label = ctk.CTkLabel(
            self.sidebar, text="Deal Finder",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        self.logo_label.pack(padx=20, pady=(20, 30))

        # Navigation buttons
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        nav_items = [
            ("dashboard", "Dashboard"),
            ("analysis", "Analisi"),
            ("categories", "Categorie"),
            ("settings", "Impostazioni"),
            ("log", "Log"),
        ]
        for key, label in nav_items:
            btn = ctk.CTkButton(
                self.sidebar, text=label, height=36,
                fg_color="transparent", text_color=("gray10", "gray90"),
                hover_color=("gray70", "gray30"),
                anchor="w", command=lambda k=key: self._show_frame(k),
            )
            btn.pack(fill="x", padx=10, pady=2)
            self._nav_buttons[key] = btn

        # Versione in fondo alla sidebar
        ver_label = ctk.CTkLabel(self.sidebar, text="v1.0", text_color="gray50", font=ctk.CTkFont(size=11))
        ver_label.pack(side="bottom", pady=10)

        # Container principale
        self.main_container = ctk.CTkFrame(self, fg_color="transparent")
        self.main_container.pack(side="right", fill="both", expand=True, padx=15, pady=15)

        # Frames
        self._frames: dict[str, ctk.CTkFrame] = {}
        self._frames["dashboard"] = self._build_dashboard_frame()
        self._frames["analysis"] = self._build_analysis_frame()
        self._frames["categories"] = self._build_categories_frame()
        self._frames["settings"] = self._build_settings_frame()
        self._frames["log"] = self._build_log_frame()

    def _show_frame(self, name: str):
        for frame in self._frames.values():
            frame.pack_forget()
        self._frames[name].pack(in_=self.main_container, fill="both", expand=True)

        # Evidenzia bottone attivo
        for key, btn in self._nav_buttons.items():
            if key == name:
                btn.configure(fg_color=("gray75", "gray25"))
            else:
                btn.configure(fg_color="transparent")

        # Aggiorna dati quando si naviga
        if name == "categories":
            self._refresh_categories_list()
        elif name == "settings":
            self._load_settings_values()

    # ── Dashboard ───────────────────────────────────────────

    def _build_dashboard_frame(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.main_container, fg_color="transparent")

        # Header con titolo e bottone Start/Stop
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 15))

        ctk.CTkLabel(header, text="Dashboard", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")

        self.btn_start = ctk.CTkButton(
            header, text="Avvia Monitoraggio", width=180, height=36,
            fg_color="#2ea043", hover_color="#238636",
            command=self._toggle_engine,
        )
        self.btn_start.pack(side="right")

        self.status_label = ctk.CTkLabel(header, text="Fermo", text_color="gray60", font=ctk.CTkFont(size=13))
        self.status_label.pack(side="right", padx=15)

        # Cards statistiche
        cards_frame = ctk.CTkFrame(frame, fg_color="transparent")
        cards_frame.pack(fill="x", pady=(0, 15))
        cards_frame.columnconfigure((0, 1, 2, 3), weight=1)

        self._stat_labels: dict[str, ctk.CTkLabel] = {}
        stats_def = [
            ("cycles", "Cicli", "0"),
            ("seen_today", "Analizzati oggi", "0"),
            ("deals_today", "Deal oggi", "0"),
            ("deals_total", "Deal totali", "0"),
        ]
        for i, (key, title, default) in enumerate(stats_def):
            card = ctk.CTkFrame(cards_frame, corner_radius=10)
            card.grid(row=0, column=i, padx=5, sticky="nsew")
            ctk.CTkLabel(card, text=title, text_color="gray60", font=ctk.CTkFont(size=12)).pack(padx=15, pady=(12, 0))
            lbl = ctk.CTkLabel(card, text=default, font=ctk.CTkFont(size=28, weight="bold"))
            lbl.pack(padx=15, pady=(0, 12))
            self._stat_labels[key] = lbl

        # Tabella ultimi deal
        ctk.CTkLabel(frame, text="Ultimi deal trovati", font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", pady=(10, 5))

        self.deals_scroll = ctk.CTkScrollableFrame(frame, height=220)
        self.deals_scroll.pack(fill="both", expand=True)
        self.deals_scroll.columnconfigure(0, weight=3)
        self.deals_scroll.columnconfigure(1, weight=1)
        self.deals_scroll.columnconfigure(2, weight=1)
        self.deals_scroll.columnconfigure(3, weight=1)
        self.deals_scroll.columnconfigure(4, weight=1)

        # Header tabella
        headers = ["Prodotto", "Prezzo", "Mercato", "Margine", "Piattaforma"]
        for i, h in enumerate(headers):
            ctk.CTkLabel(
                self.deals_scroll, text=h, font=ctk.CTkFont(size=12, weight="bold"), text_color="gray60",
            ).grid(row=0, column=i, padx=8, pady=4, sticky="w")

        self._deal_rows: list[list[ctk.CTkLabel]] = []

        return frame

    def _update_deals_table(self, deals: list[dict]):
        # Rimuovi righe vecchie
        for row in self._deal_rows:
            for lbl in row:
                lbl.destroy()
        self._deal_rows.clear()

        for i, deal in enumerate(deals):
            row_labels = []
            values = [
                deal.get("product_name", "—"),
                f"{deal.get('asked_price', 0):.0f} EUR",
                f"{deal.get('market_price', 0):.0f} EUR",
                f"+{deal.get('margin_percent', 0):.0f}%",
                deal.get("platform", "—").capitalize() if "platform" in deal else "—",
            ]
            for j, val in enumerate(values):
                lbl = ctk.CTkLabel(self.deals_scroll, text=val, font=ctk.CTkFont(size=12))
                lbl.grid(row=i + 1, column=j, padx=8, pady=3, sticky="w")
                row_labels.append(lbl)
            self._deal_rows.append(row_labels)

    # ── Analisi ──────────────────────────────────────────────

    def _build_analysis_frame(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.main_container, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(header, text="Analisi", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")

        ctk.CTkButton(header, text="Pulisci", width=80, command=self._clear_analysis).pack(side="right")

        # Legenda
        legend = ctk.CTkFrame(frame, fg_color="transparent")
        legend.pack(fill="x", pady=(0, 8))
        legend_items = [
            ("#2ea043", "DEAL"),
            ("#d29922", "Sotto soglia"),
            ("#8b949e", "No prezzo eBay"),
            ("#d73a49", "Margine negativo"),
        ]
        for color, text in legend_items:
            dot = ctk.CTkLabel(legend, text="\u25cf", text_color=color, font=ctk.CTkFont(size=14))
            dot.pack(side="left", padx=(0, 2))
            ctk.CTkLabel(legend, text=text, text_color="gray60", font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 12))

        # Tabella analisi — 5 colonne
        self.analysis_scroll = ctk.CTkScrollableFrame(frame)
        self.analysis_scroll.pack(fill="both", expand=True)

        # Colonne: Prezzo Subito | Prezzo eBay | Media venduti | Margine | Link
        col_weights = [3, 2, 2, 2, 1]
        for i, w in enumerate(col_weights):
            self.analysis_scroll.columnconfigure(i, weight=w)

        headers = ["Prezzo Subito", "Prezzo eBay", "Media venduti", "Margine", "Link"]
        for i, h in enumerate(headers):
            ctk.CTkLabel(
                self.analysis_scroll, text=h,
                font=ctk.CTkFont(size=12, weight="bold"), text_color="gray60",
            ).grid(row=0, column=i, padx=6, pady=4, sticky="w")

        self._analysis_rows: list[list[tk.Widget]] = []
        self._analysis_row_count = 0

        return frame

    def _clear_analysis(self):
        for row in self._analysis_rows:
            for widget in row:
                widget.destroy()
        self._analysis_rows.clear()
        self._analysis_row_count = 0

    def _on_listing_analyzed(self, data: dict):
        """Callback dal thread del motore per ogni inserzione analizzata."""
        self.after(0, self._append_analysis_row, data)

    def _append_analysis_row(self, data: dict):
        self._analysis_row_count += 1
        row_idx = self._analysis_row_count

        status = data.get("status", "")
        asked = data.get("asked_price", 0)
        market = data.get("market_price", 0)  # mediana venduti
        margin = data.get("margin_percent", 0)
        sold = data.get("sold_count", 0)
        platform = data.get("platform", "").capitalize()
        product = data.get("product_name", "—")[:35]
        url = data.get("url", "")
        active_median = data.get("active_median", 0)
        active_count = data.get("active_count", 0)

        # Determina colore in base allo stato
        status_colors = {
            "deal":         "#2ea043",
            "sotto_soglia": "#d29922",
            "no_prezzo":    "#8b949e",
            "errore_prezzo": "#8b949e",
            "skip_llm":     "#6e7681",
            "errore_llm":   "#6e7681",
        }
        color = status_colors.get(status, "#8b949e")
        if market > 0 and margin < 0:
            color = "#d73a49"

        row_widgets: list[tk.Widget] = []

        # Colonna 1: Prezzo Subito/sorgente (prodotto + prezzo chiesto)
        col1_text = f"[{platform}] {product}\n{asked:.0f} EUR"
        lbl_subito = ctk.CTkLabel(
            self.analysis_scroll, text=col1_text,
            font=ctk.CTkFont(size=11), text_color=color, anchor="w", justify="left",
        )
        lbl_subito.grid(row=row_idx, column=0, padx=6, pady=2, sticky="w")
        row_widgets.append(lbl_subito)

        # Colonna 2: Prezzo eBay (inserzioni attive)
        if active_median > 0:
            col2_text = f"{active_median:.0f} EUR\n({active_count} attivi)"
        elif market > 0:
            col2_text = f"{market:.0f} EUR"
        else:
            error_labels = {
                "no_prezzo": "Non trovato", "errore_prezzo": "Errore",
                "skip_llm": "—", "errore_llm": "—",
            }
            col2_text = error_labels.get(status, "—")
        lbl_ebay = ctk.CTkLabel(
            self.analysis_scroll, text=col2_text,
            font=ctk.CTkFont(size=11),
            text_color=("gray90", "gray90") if (active_median > 0 or market > 0) else "gray60",
            anchor="w", justify="left",
        )
        lbl_ebay.grid(row=row_idx, column=1, padx=6, pady=2, sticky="w")
        row_widgets.append(lbl_ebay)

        # Colonna 3: Media venduti (mediana oggetti venduti)
        if market > 0:
            col3_text = f"{market:.0f} EUR\n({sold} venduti)"
        else:
            col3_text = "—"
        lbl_sold = ctk.CTkLabel(
            self.analysis_scroll, text=col3_text,
            font=ctk.CTkFont(size=11),
            text_color=("gray90", "gray90") if market > 0 else "gray60",
            anchor="w", justify="left",
        )
        lbl_sold.grid(row=row_idx, column=2, padx=6, pady=2, sticky="w")
        row_widgets.append(lbl_sold)

        # Colonna 4: Margine (% + piattaforma guadagno)
        if market > 0:
            margin_text = f"{margin:+.0f}%"
            if status == "deal":
                margin_text += f"\n{platform}"
        else:
            margin_text = "—"
        margin_color = color if market > 0 else "gray60"
        lbl_margin = ctk.CTkLabel(
            self.analysis_scroll, text=margin_text,
            font=ctk.CTkFont(size=11, weight="bold" if status == "deal" else "normal"),
            text_color=margin_color, anchor="w", justify="left",
        )
        lbl_margin.grid(row=row_idx, column=3, padx=6, pady=2, sticky="w")
        row_widgets.append(lbl_margin)

        # Colonna 5: Link
        if url:
            btn_link = ctk.CTkButton(
                self.analysis_scroll, text="Apri",
                width=50, height=24, corner_radius=4,
                font=ctk.CTkFont(size=11),
                fg_color=("gray70", "gray30"), hover_color=("gray60", "gray40"),
                command=lambda u=url: webbrowser.open(u),
            )
            btn_link.grid(row=row_idx, column=4, padx=6, pady=2, sticky="w")
            row_widgets.append(btn_link)
        else:
            lbl_no_link = ctk.CTkLabel(
                self.analysis_scroll, text="—",
                font=ctk.CTkFont(size=11), text_color="gray60",
            )
            lbl_no_link.grid(row=row_idx, column=4, padx=6, pady=2, sticky="w")
            row_widgets.append(lbl_no_link)

        self._analysis_rows.append(row_widgets)

    # ── Categorie ───────────────────────────────────────────

    def _build_categories_frame(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.main_container, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(header, text="Categorie", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")

        btn_add = ctk.CTkButton(header, text="+ Nuova categoria", width=160, command=self._add_category_dialog)
        btn_add.pack(side="right")

        self.cat_scroll = ctk.CTkScrollableFrame(frame)
        self.cat_scroll.pack(fill="both", expand=True)

        return frame

    def _refresh_categories_list(self):
        for widget in self.cat_scroll.winfo_children():
            widget.destroy()

        config = _load_config()
        categories = config.get("categories", [])

        if not categories:
            ctk.CTkLabel(self.cat_scroll, text="Nessuna categoria configurata", text_color="gray60").pack(pady=20)
            return

        for i, cat in enumerate(categories):
            card = ctk.CTkFrame(self.cat_scroll, corner_radius=8)
            card.pack(fill="x", pady=4, padx=2)

            # Top: nome + bottone rimuovi
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=12, pady=(10, 4))

            ctk.CTkLabel(
                top, text=cat.get("name", ""),
                font=ctk.CTkFont(size=15, weight="bold"),
            ).pack(side="left")

            ctk.CTkButton(
                top, text="Rimuovi", width=80, height=28,
                fg_color="#d73a49", hover_color="#cb2431",
                command=lambda idx=i: self._remove_category(idx),
            ).pack(side="right")

            # Keywords
            kw_text = ", ".join(cat.get("keywords", []))
            ctk.CTkLabel(card, text=f"Keywords: {kw_text}", text_color="gray60").pack(anchor="w", padx=12)

            # Prezzo
            ctk.CTkLabel(
                card,
                text=f"Prezzo: {cat.get('min_price', 0)} - {cat.get('max_price', 0)} EUR",
                text_color="gray60",
            ).pack(anchor="w", padx=12, pady=(0, 10))

    def _add_category_dialog(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Nuova categoria")
        dialog.geometry("420x320")
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(dialog, text="Nome categoria:").pack(anchor="w", padx=20, pady=(15, 2))
        name_entry = ctk.CTkEntry(dialog, width=360, placeholder_text="es. Smartphone")
        name_entry.pack(padx=20)

        ctk.CTkLabel(dialog, text="Keywords (separate da virgola):").pack(anchor="w", padx=20, pady=(10, 2))
        kw_entry = ctk.CTkEntry(dialog, width=360, placeholder_text="es. iphone, samsung galaxy, pixel")
        kw_entry.pack(padx=20)

        prices_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        prices_frame.pack(fill="x", padx=20, pady=(10, 0))

        ctk.CTkLabel(prices_frame, text="Prezzo min:").grid(row=0, column=0, sticky="w")
        min_entry = ctk.CTkEntry(prices_frame, width=100, placeholder_text="50")
        min_entry.grid(row=0, column=1, padx=(5, 20))

        ctk.CTkLabel(prices_frame, text="Prezzo max:").grid(row=0, column=2, sticky="w")
        max_entry = ctk.CTkEntry(prices_frame, width=100, placeholder_text="1200")
        max_entry.grid(row=0, column=3, padx=5)

        def _save():
            name = name_entry.get().strip()
            kws = [k.strip() for k in kw_entry.get().split(",") if k.strip()]
            try:
                min_p = float(min_entry.get() or "0")
                max_p = float(max_entry.get() or "99999")
            except ValueError:
                messagebox.showerror("Errore", "Prezzo non valido")
                return

            if not name or not kws:
                messagebox.showerror("Errore", "Nome e keywords sono obbligatori")
                return

            config = _load_config()
            cats = config.get("categories", [])
            cats.append({"name": name, "keywords": kws, "min_price": min_p, "max_price": max_p})
            config["categories"] = cats
            _save_config(config)
            dialog.destroy()
            self._refresh_categories_list()

        ctk.CTkButton(dialog, text="Aggiungi", width=360, command=_save).pack(padx=20, pady=20)

    def _remove_category(self, index: int):
        config = _load_config()
        cats = config.get("categories", [])
        if 0 <= index < len(cats):
            name = cats[index].get("name", "")
            if messagebox.askyesno("Conferma", f"Rimuovere la categoria '{name}'?"):
                cats.pop(index)
                config["categories"] = cats
                _save_config(config)
                self._refresh_categories_list()

    # ── Impostazioni ────────────────────────────────────────

    def _build_settings_frame(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.main_container, fg_color="transparent")

        ctk.CTkLabel(frame, text="Impostazioni", font=ctk.CTkFont(size=24, weight="bold")).pack(anchor="w", pady=(0, 15))

        settings_card = ctk.CTkFrame(frame, corner_radius=10)
        settings_card.pack(fill="x")

        # Margine minimo
        row1 = ctk.CTkFrame(settings_card, fg_color="transparent")
        row1.pack(fill="x", padx=20, pady=(15, 5))
        ctk.CTkLabel(row1, text="Margine minimo (%):").pack(side="left")
        self.margin_entry = ctk.CTkEntry(row1, width=80)
        self.margin_entry.pack(side="right")

        # Polling interval
        row2 = ctk.CTkFrame(settings_card, fg_color="transparent")
        row2.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(row2, text="Intervallo polling (secondi):").pack(side="left")
        self.polling_entry = ctk.CTkEntry(row2, width=80)
        self.polling_entry.pack(side="right")

        # Max listings per ciclo
        row3 = ctk.CTkFrame(settings_card, fg_color="transparent")
        row3.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(row3, text="Max inserzioni per ciclo:").pack(side="left")
        self.max_listings_entry = ctk.CTkEntry(row3, width=80)
        self.max_listings_entry.pack(side="right")

        # Piattaforme attive
        ctk.CTkLabel(settings_card, text="Piattaforme attive:", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=20, pady=(15, 5))

        platforms_frame = ctk.CTkFrame(settings_card, fg_color="transparent")
        platforms_frame.pack(fill="x", padx=20)

        self.platform_vars: dict[str, ctk.BooleanVar] = {}
        for plat in ["subito", "ebay"]:
            var = ctk.BooleanVar(value=False)
            self.platform_vars[plat] = var
            ctk.CTkCheckBox(platforms_frame, text=plat.capitalize(), variable=var).pack(side="left", padx=(0, 20))

        # Bottone salva
        ctk.CTkButton(settings_card, text="Salva impostazioni", command=self._save_settings).pack(pady=20)

        return frame

    def _load_settings_values(self):
        config = _load_config()
        self.margin_entry.delete(0, "end")
        self.margin_entry.insert(0, str(config.get("min_margin_percent", 25)))

        self.polling_entry.delete(0, "end")
        self.polling_entry.insert(0, str(config.get("polling_interval", 300)))

        self.max_listings_entry.delete(0, "end")
        self.max_listings_entry.insert(0, str(config.get("max_listings_per_cycle", 50)))

        active = config.get("platforms", [])
        for plat, var in self.platform_vars.items():
            var.set(plat in active)

    def _save_settings(self):
        config = _load_config()

        try:
            config["min_margin_percent"] = int(self.margin_entry.get())
            config["polling_interval"] = int(self.polling_entry.get())
            config["max_listings_per_cycle"] = int(self.max_listings_entry.get())
        except ValueError:
            messagebox.showerror("Errore", "I valori devono essere numeri interi")
            return

        platforms = [p for p, v in self.platform_vars.items() if v.get()]
        config["platforms"] = platforms

        _save_config(config)
        messagebox.showinfo("Salvato", "Impostazioni salvate! Saranno attive dal prossimo ciclo.")

    # ── Log ─────────────────────────────────────────────────

    def _build_log_frame(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.main_container, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(header, text="Log", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")

        ctk.CTkButton(header, text="Pulisci", width=80, command=self._clear_log).pack(side="right")

        self.log_textbox = ctk.CTkTextbox(frame, state="disabled", font=ctk.CTkFont(family="Consolas", size=12))
        self.log_textbox.pack(fill="both", expand=True)

        return frame

    def _append_log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_textbox.configure(state="normal")
        self.log_textbox.insert("end", f"[{timestamp}] {msg}\n")
        self.log_textbox.see("end")
        self.log_textbox.configure(state="disabled")

    def _clear_log(self):
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")

    # ── Engine control ──────────────────────────────────────

    def _toggle_engine(self):
        if self.engine.is_running:
            self.engine.stop()
        else:
            self.engine.start()

    def _on_engine_status(self, status: str):
        """Callback dal thread del motore — schedula aggiornamento GUI."""
        self.after(0, self._update_status_ui, status)

    def _update_status_ui(self, status: str):
        status_map = {
            "running": ("In esecuzione", "#2ea043"),
            "paused": ("In pausa", "#d29922"),
            "stopped": ("Fermo", "gray60"),
        }
        text, color = status_map.get(status, ("—", "gray60"))
        self.status_label.configure(text=text, text_color=color)

        if status == "running":
            self.btn_start.configure(text="Ferma monitoraggio", fg_color="#d73a49", hover_color="#cb2431")
        else:
            self.btn_start.configure(text="Avvia monitoraggio", fg_color="#2ea043", hover_color="#238636")

    def _on_cycle_complete(self, stats: dict):
        """Callback dal thread del motore dopo ogni ciclo."""
        self.after(0, self._update_stats_ui, stats)

    def _update_stats_ui(self, stats: dict):
        self._stat_labels["cycles"].configure(text=str(stats.get("cycle_count", 0)))
        self._stat_labels["seen_today"].configure(text=str(stats.get("seen_today", 0)))
        self._stat_labels["deals_today"].configure(text=str(stats.get("notified_today", 0)))
        self._stat_labels["deals_total"].configure(text=str(stats.get("total_notified", 0)))

    def _on_deal_found(self, deal: dict):
        """Callback dal thread del motore quando trova un deal."""
        self.after(0, self._append_deal_to_table, deal)

    def _append_deal_to_table(self, deal: dict):
        # Inserisci in cima (shifting existing rows)
        row_idx = len(self._deal_rows) + 1
        row_labels = []
        values = [
            deal.get("product_name", "—"),
            f"{deal.get('asked_price', 0):.0f} EUR",
            f"{deal.get('market_price', 0):.0f} EUR",
            f"+{deal.get('margin_percent', 0):.0f}%",
            deal.get("platform", "—").capitalize(),
        ]
        for j, val in enumerate(values):
            lbl = ctk.CTkLabel(self.deals_scroll, text=val, font=ctk.CTkFont(size=12))
            lbl.grid(row=row_idx, column=j, padx=8, pady=3, sticky="w")
            row_labels.append(lbl)
        self._deal_rows.append(row_labels)

    def _on_engine_log(self, msg: str):
        """Callback dal thread del motore per messaggi di log."""
        self.after(0, self._append_log, msg)

    def _load_initial_stats(self):
        """Carica statistiche dal DB all'avvio della GUI."""
        def _load():
            loop = asyncio.new_event_loop()
            try:
                db = Database()
                loop.run_until_complete(db.connect())
                stats = loop.run_until_complete(db.get_stats())
                deals = loop.run_until_complete(db.get_recent_notifications(limit=20))
                loop.run_until_complete(db.close())
                self.after(0, self._update_stats_ui, stats)
                self.after(0, self._update_deals_table, deals)
            except Exception as e:
                logger.debug("Errore caricamento stats iniziali: %s", e)
            finally:
                loop.close()

        threading.Thread(target=_load, daemon=True).start()

    def _on_close(self):
        if self.engine.is_running:
            self.engine.stop()
        self.destroy()


def run():
    """Entry point per avviare la GUI."""
    app = DealFinderApp()
    app.mainloop()
