"""Windows masaüstü arayüzü; veri dosyaları monitor.py ile ortaktır."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

import monitor

ROOT = Path(__file__).resolve().parent


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Streetwear Takip")
        self.minsize(860, 560)
        self.configure(bg="#111315")
        self._style()
        self.status = tk.StringVar(value="Hazır")
        self._build()
        self.refresh()

    def _style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#111315")
        style.configure("Panel.TFrame", background="#1a1d20")
        style.configure("TLabel", background="#111315", foreground="#f4f4ef", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 22), foreground="#f4f4ef")
        style.configure("Muted.TLabel", foreground="#aeb5b9")
        style.configure("TButton", padding=(12, 7), font=("Segoe UI Semibold", 9))
        style.configure("Treeview", background="#1a1d20", fieldbackground="#1a1d20", foreground="#eef0ed", rowheight=31, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background="#252a2e", foreground="#d8ddda", relief="flat", font=("Segoe UI Semibold", 9))
        style.map("Treeview", background=[("selected", "#2e6f63")], foreground=[("selected", "white")])
        style.configure("TCheckbutton", background="#1a1d20", foreground="#eef0ed")
        style.configure("TEntry", fieldbackground="#252a2e", foreground="#f4f4ef", insertcolor="#f4f4ef")

    def _build(self) -> None:
        header = ttk.Frame(self, padding=(28, 24, 28, 10))
        header.pack(fill="x")
        ttk.Label(header, text="Streetwear Takip", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="yeni ürün · fiyat düşüşü · Telegram", style="Muted.TLabel").pack(side="left", padx=16, pady=7)
        ttk.Button(header, text="Şimdi kontrol et", command=self.run_check).pack(side="right")

        body = ttk.Frame(self, padding=(28, 10, 28, 8))
        body.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill="both", expand=True)
        self._stores_tab()
        self._notifications_tab()
        self._settings_tab()
        footer = ttk.Frame(self, padding=(28, 8, 28, 20))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel").pack(side="left")
        ttk.Button(footer, text="Yenile", command=self.refresh).pack(side="right")

    def _stores_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=16, style="Panel.TFrame")
        self.notebook.add(tab, text="Mağazalar")
        ttk.Label(tab, text="Etkin mağazalar ve son güvenilir tarama", style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        columns = ("store", "products", "checked", "state")
        self.stores = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse")
        for key, label, width in (("store", "Mağaza", 250), ("products", "Ürün", 100), ("checked", "Son tarama", 175), ("state", "Durum", 340)):
            self.stores.heading(key, text=label)
            self.stores.column(key, width=width, anchor="w" if key in {"store", "state"} else "center")
        self.stores.pack(fill="both", expand=True)
        self.stores.bind("<Double-1>", self.open_store)

    def _notifications_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=16, style="Panel.TFrame")
        self.notebook.add(tab, text="Bekleyenler")
        ttk.Label(tab, text="Telegram ayarlı değilse veya gönderim başarısızsa olaylar burada güvenle sıraya alınır.", style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        columns = ("kind", "brand", "name", "price")
        self.pending = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse")
        for key, label, width in (("kind", "Tür", 110), ("brand", "Mağaza", 150), ("name", "Ürün", 420), ("price", "Fiyat", 130)):
            self.pending.heading(key, text=label)
            self.pending.column(key, width=width, anchor="w")
        self.pending.pack(fill="both", expand=True)
        self.pending.bind("<Double-1>", self.open_pending)
        ttk.Button(tab, text="Seçileni favorilere ekle", command=self.add_favorite).pack(anchor="e", pady=(10, 0))

    def _settings_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=20, style="Panel.TFrame")
        self.notebook.add(tab, text="Bildirim filtreleri")
        self.new_var, self.drop_var, self.fav_var = tk.BooleanVar(), tk.BooleanVar(), tk.BooleanVar()
        ttk.Checkbutton(tab, text="Yeni ürünleri bildir", variable=self.new_var).grid(row=0, column=0, sticky="w", pady=5)
        ttk.Checkbutton(tab, text="Fiyat düşüşlerini bildir", variable=self.drop_var).grid(row=1, column=0, sticky="w", pady=5)
        ttk.Checkbutton(tab, text="Yalnızca favoriler", variable=self.fav_var).grid(row=2, column=0, sticky="w", pady=5)
        self._entry(tab, 3, "Yalnız mağazalar (virgülle ayır)", "brands")
        self._entry(tab, 4, "İçermesi gereken kelimeler", "include")
        self._entry(tab, 5, "Hariç kelimeler", "exclude")
        self._entry(tab, 6, "En az indirim yüzdesi", "discount")
        ttk.Label(tab, text="Bot token ve sohbet kimliği güvenlik için Windows ortam değişkenlerinde kalır; burada saklanmaz.", style="Muted.TLabel", wraplength=680).grid(row=7, column=0, columnspan=2, sticky="w", pady=(18, 8))
        ttk.Button(tab, text="Filtreleri kaydet", command=self.save_settings).grid(row=8, column=0, sticky="w")

    def _entry(self, tab: ttk.Frame, row: int, label: str, name: str) -> None:
        ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w", pady=(12, 3))
        entry = ttk.Entry(tab, width=58)
        entry.grid(row=row, column=1, sticky="w", padx=(18, 0), pady=(12, 3))
        setattr(self, f"{name}_entry", entry)

    def refresh(self) -> None:
        try:
            config = monitor.load_json(monitor.CONFIG, {})
            state = monitor.migrate_state(monitor.load_json(monitor.STATE, {}), config)
            settings = monitor.merge_defaults(monitor.load_json(monitor.SETTINGS, {}), monitor.DEFAULT_SETTINGS)
        except ValueError as error:
            messagebox.showerror("Dosya hatası", str(error))
            return
        for tree in (self.stores, self.pending):
            tree.delete(*tree.get_children())
        self.store_urls = {}
        for site in config.get("sites", []):
            if not site.get("enabled", True):
                continue
            name, record = site.get("name", "Adsız"), state["stores"].get(monitor.stable_store_id(site), {})
            problem = record.get("last_error")
            status = problem or "Hazır"
            self.stores.insert("", "end", iid=name, values=(name, record.get("count", "—"), record.get("checked", "Henüz taranmadı"), status))
            self.store_urls[name] = site.get("url", "")
        self.pending_urls = {}
        for index, item in enumerate(state["pending"]):
            if not isinstance(item, dict):
                continue
            price = monitor.money(item.get("new_price", item.get("price")), item.get("currency")) if "new_price" in item or "price" in item else "—"
            kind = item.get("type", "NEW_PRODUCT")
            iid = f"pending-{index}"
            self.pending.insert("", "end", iid=iid, values=(kind, item.get("store_name", item.get("brand", "")), item.get("product_name", item.get("name", "")), price))
            self.pending_urls[iid] = item.get("url", "")
        rules = settings["telegram"]
        self.new_var.set(rules["notify_new"]); self.drop_var.set(rules["notify_price_drops"]); self.fav_var.set(rules["only_favorites"])
        self._set_entry("brands", ", ".join(rules.get("brands", [])))
        self._set_entry("include", ", ".join(rules.get("include_keywords", [])))
        self._set_entry("exclude", ", ".join(rules.get("exclude_keywords", [])))
        self._set_entry("discount", str(rules.get("minimum_discount_percent", 0)))
        self.status.set(f"{len(self.stores.get_children())} etkin mağaza · {len(self.pending.get_children())} bekleyen olay")

    def _set_entry(self, name: str, value: str) -> None:
        entry = getattr(self, f"{name}_entry")
        entry.delete(0, "end"); entry.insert(0, value)

    def save_settings(self) -> None:
        try:
            existing = monitor.load_json(monitor.SETTINGS, {})
            settings = monitor.merge_defaults(existing, monitor.DEFAULT_SETTINGS)
            rules = settings["telegram"]
            rules.update({"notify_new": self.new_var.get(), "notify_price_drops": self.drop_var.get(), "only_favorites": self.fav_var.get(),
                          "brands": self._csv("brands"), "include_keywords": self._csv("include"), "exclude_keywords": self._csv("exclude"),
                          "minimum_discount_percent": float(self.discount_entry.get().replace(",", ".") or 0)})
            if rules["minimum_discount_percent"] < 0 or rules["minimum_discount_percent"] > 100:
                raise ValueError("İndirim yüzdesi 0 ile 100 arasında olmalı.")
            monitor.atomic_json(monitor.SETTINGS, settings)
            self.status.set("Bildirim filtreleri kaydedildi.")
        except ValueError as error:
            messagebox.showerror("Ayar geçersiz", str(error))

    def _csv(self, name: str) -> list[str]:
        return [item.strip() for item in getattr(self, f"{name}_entry").get().split(",") if item.strip()]

    def open_store(self, _event: object) -> None:
        selected = self.stores.selection()
        if selected and self.store_urls.get(selected[0]): webbrowser.open(self.store_urls[selected[0]])

    def open_pending(self, _event: object) -> None:
        selected = self.pending.selection()
        if selected and self.pending_urls.get(selected[0]): webbrowser.open(self.pending_urls[selected[0]])

    def add_favorite(self) -> None:
        selected = self.pending.selection()
        if not selected:
            self.status.set("Önce Bekleyenler listesinden bir ürün seçin.")
            return
        url = self.pending_urls.get(selected[0])
        if not url:
            return
        raw = monitor.load_json(monitor.FAVORITES, {"favorites": []})
        if not isinstance(raw, dict): raw = {"favorites": raw if isinstance(raw, list) else []}
        favorites = raw.setdefault("favorites", [])
        existing = monitor.favorite_urls()
        if monitor.normalize(url) not in existing:
            favorites.append({"url": url})
            monitor.atomic_json(monitor.FAVORITES, raw)
            self.status.set("Ürün favorilere eklendi.")
        else:
            self.status.set("Bu ürün zaten favorilerde.")

    def run_check(self) -> None:
        self.status.set("Tarama başlatıldı; uygulama açık kalabilir.")
        def worker() -> None:
            result = subprocess.run([sys.executable, str(ROOT / "monitor.py")], cwd=ROOT, text=True, capture_output=True)
            message = (result.stdout or result.stderr or f"Tarama {result.returncode} koduyla bitti.").strip().splitlines()[-1]
            self.after(0, lambda: (self.refresh(), self.status.set(message)))
        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    App().mainloop()