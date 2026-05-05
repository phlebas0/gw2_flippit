#!/usr/bin/env python3
"""
GW2 Flip Tracker
----------------
Syncs your GW2 Trading Post transaction history via the official API,
matches completed buys to completed sells using FIFO, and calculates
net profit after the 15% TP fee (5% listing + 10% sales tax).

Requirements:  pip install requests
Setup:         Generate an API key at account.arena.net -> My Account -> Applications
               Required permission: tradingpost
"""

import csv
import json
import os
import sqlite3
import threading
import tkinter as tk
from collections import defaultdict, deque
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import requests

# -- Constants ----------------------------------------------------------------

DB_FILE   = "gw2_flips.db"
CONF_FILE = "gw2_config.json"
GW2_API   = "https://api.guildwars2.com/v2"
TP_TAX    = 0.15   # 5% listing fee + 10% sales tax
PAGE_SIZE = 200
APP_TITLE = "GW2 Flip Tracker"

# -- Copper helpers -----------------------------------------------------------

def copper_to_str(c: int) -> str:
    """Convert raw copper value to human-readable '1g 5s 30c' format."""
    if c < 0:
        return f"-{copper_to_str(-c)}"
    g, rem = divmod(c, 10000)
    s, cu  = divmod(rem, 100)
    parts  = []
    if g:               parts.append(f"{g}g")
    if s:               parts.append(f"{s}s")
    if cu or not parts: parts.append(f"{cu}c")
    return " ".join(parts)

# -- Config -------------------------------------------------------------------

def load_config() -> dict:
    if os.path.exists(CONF_FILE):
        try:
            with open(CONF_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(cfg: dict):
    with open(CONF_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# -- Database -----------------------------------------------------------------

class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS raw_buys (
                id        INTEGER PRIMARY KEY,
                item_id   INTEGER NOT NULL,
                price     INTEGER NOT NULL,
                quantity  INTEGER NOT NULL,
                created   TEXT    NOT NULL,
                purchased TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS raw_sells (
                id        INTEGER PRIMARY KEY,
                item_id   INTEGER NOT NULL,
                price     INTEGER NOT NULL,
                quantity  INTEGER NOT NULL,
                created   TEXT    NOT NULL,
                purchased TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS item_names (
                item_id INTEGER PRIMARY KEY,
                name    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS matched_flips (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id    INTEGER NOT NULL,
                item_name  TEXT    NOT NULL,
                quantity   INTEGER NOT NULL,
                buy_price  INTEGER NOT NULL,
                sell_price INTEGER NOT NULL,
                profit     INTEGER NOT NULL,
                buy_date   TEXT    NOT NULL,
                sell_date  TEXT    NOT NULL
            );
        """)
        self.conn.commit()

    def upsert_buys(self, rows: list):
        self.conn.executemany(
            "INSERT OR REPLACE INTO raw_buys "
            "VALUES (:id, :item_id, :price, :quantity, :created, :purchased)",
            rows
        )
        self.conn.commit()

    def upsert_sells(self, rows: list):
        self.conn.executemany(
            "INSERT OR REPLACE INTO raw_sells "
            "VALUES (:id, :item_id, :price, :quantity, :created, :purchased)",
            rows
        )
        self.conn.commit()

    def all_buys(self) -> list:
        return self.conn.execute(
            "SELECT * FROM raw_buys ORDER BY purchased ASC"
        ).fetchall()

    def all_sells(self) -> list:
        return self.conn.execute(
            "SELECT * FROM raw_sells ORDER BY purchased ASC"
        ).fetchall()

    def upsert_names(self, names: dict):
        self.conn.executemany(
            "INSERT OR REPLACE INTO item_names VALUES (?, ?)",
            names.items()
        )
        self.conn.commit()

    def get_names(self) -> dict:
        rows = self.conn.execute("SELECT item_id, name FROM item_names").fetchall()
        return {r["item_id"]: r["name"] for r in rows}

    def unknown_item_ids(self, ids: set) -> set:
        known = {
            r[0] for r in
            self.conn.execute("SELECT item_id FROM item_names").fetchall()
        }
        return ids - known

    def replace_flips(self, flips: list):
        self.conn.execute("DELETE FROM matched_flips")
        if flips:
            self.conn.executemany(
                "INSERT INTO matched_flips "
                "(item_id, item_name, quantity, buy_price, sell_price, "
                " profit, buy_date, sell_date) "
                "VALUES (:item_id, :item_name, :quantity, :buy_price, "
                "        :sell_price, :profit, :buy_date, :sell_date)",
                flips
            )
        self.conn.commit()

    def get_flips(self,
                  item_filter: str = "",
                  date_from: str = "",
                  date_to: str = "") -> list:
        query  = "SELECT * FROM matched_flips WHERE 1=1"
        params = []
        if item_filter:
            query += " AND item_name LIKE ?"
            params.append(f"%{item_filter}%")
        if date_from:
            query += " AND sell_date >= ?"
            params.append(date_from)
        if date_to:
            query += " AND sell_date <= ?"
            params.append(date_to)
        query += " ORDER BY sell_date DESC, item_name ASC"
        return self.conn.execute(query, params).fetchall()

    def export_csv(self, path: str, flips: list):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "Item", "Qty",
                "Buy Price (copper)", "Sell Price (copper)",
                "Net Profit (copper)", "Net Profit",
                "Buy Date", "Sell Date",
            ])
            for r in flips:
                w.writerow([
                    r["item_name"], r["quantity"],
                    r["buy_price"], r["sell_price"],
                    r["profit"], copper_to_str(r["profit"]),
                    r["buy_date"], r["sell_date"],
                ])

# -- GW2 API client -----------------------------------------------------------

class GW2Api:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def _get(self, path: str, **params):
        r = self.session.get(f"{GW2_API}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def verify_key(self) -> dict:
        return self._get("/tokeninfo")

    def fetch_all_history(self, side: str, progress_cb=None) -> list:
        """Fetch all pages of completed TP history for 'buys' or 'sells'."""
        all_rows = []
        page = 0
        while True:
            try:
                r = self.session.get(
                    f"{GW2_API}/commerce/transactions/history/{side}",
                    params={"page": page},
                    timeout=10,
                )
                if r.status_code == 404:
                    break
                r.raise_for_status()
                data = r.json()
            except requests.HTTPError:
                raise
            if not data:
                break
            all_rows.extend(data)
            if progress_cb:
                progress_cb(len(all_rows))
            total_pages = int(r.headers.get("X-Page-Total", 1))
            if page >= total_pages - 1:
                break
            page += 1
        return all_rows

    def fetch_item_names(self, item_ids: list) -> dict:
        """Batch-resolve item IDs to names, 200 at a time."""
        names = {}
        for i in range(0, len(item_ids), 200):
            chunk = item_ids[i : i + 200]
            try:
                items = self._get("/items", ids=",".join(map(str, chunk)))
                for item in items:
                    names[item["id"]] = item["name"]
            except Exception:
                pass
        return names

# -- FIFO matching ------------------------------------------------------------

def fifo_match(buys: list, sells: list, names: dict) -> list:
    """
    Match sell events against buy events per item using FIFO.
    Handles partial lots transparently.
    Returns a list of flip dicts ready for DB insertion.
    """
    buy_map  = defaultdict(list)
    sell_map = defaultdict(list)
    for b in buys:
        buy_map[b["item_id"]].append(b)
    for s in sells:
        sell_map[s["item_id"]].append(s)

    flips = []

    for item_id, item_sells in sell_map.items():
        item_buys = buy_map.get(item_id, [])
        item_name = names.get(item_id, f"Item #{item_id}")

        buy_queue = deque(
            {
                "remaining": b["quantity"],
                "price":     b["price"],
                "date":      b["purchased"][:10],
            }
            for b in sorted(item_buys, key=lambda x: x["purchased"])
        )

        for sell in sorted(item_sells, key=lambda x: x["purchased"]):
            remaining  = sell["quantity"]
            sell_date  = sell["purchased"][:10]
            sell_price = sell["price"]

            while remaining > 0 and buy_queue:
                buy         = buy_queue[0]
                matched_qty = min(remaining, buy["remaining"])
                profit      = (
                    int(sell_price * matched_qty * (1 - TP_TAX))
                    - buy["price"] * matched_qty
                )
                flips.append({
                    "item_id":    item_id,
                    "item_name":  item_name,
                    "quantity":   matched_qty,
                    "buy_price":  buy["price"],
                    "sell_price": sell_price,
                    "profit":     profit,
                    "buy_date":   buy["date"],
                    "sell_date":  sell_date,
                })
                buy["remaining"] -= matched_qty
                remaining        -= matched_qty
                if buy["remaining"] == 0:
                    buy_queue.popleft()
            # Any remaining sell qty with no matching buy = unmatched (skipped)

    return flips

# -- API Key dialog -----------------------------------------------------------

class ApiKeyDialog(tk.Toplevel):
    def __init__(self, parent, current_key: str = ""):
        super().__init__(parent)
        self.title("Set GW2 API Key")
        self.resizable(False, False)
        self.result: Optional[str] = None
        self._build(current_key)
        self.transient(parent)
        self.grab_set()
        self.wait_window()

    def _build(self, current_key: str):
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="GW2 API Key", font=("", 11, "bold")).pack(anchor="w")
        ttk.Label(
            f,
            text=(
                "Generate a key at:\n"
                "  account.arena.net  ->  My Account  ->  Applications\n\n"
                "Required permission:  tradingpost"
            ),
            foreground="grey", justify="left",
        ).pack(anchor="w", pady=(4, 10))

        self._key_var = tk.StringVar(value=current_key)
        entry = ttk.Entry(f, textvariable=self._key_var, width=56)
        entry.pack(fill="x")
        entry.focus_set()

        self._status = ttk.Label(f, text="", foreground="grey")
        self._status.pack(pady=(6, 2))

        bf = ttk.Frame(f)
        bf.pack(pady=(8, 0))
        ttk.Button(bf, text="Verify & Save", command=self._verify).pack(side="left", padx=4)
        ttk.Button(bf, text="Cancel",        command=self.destroy).pack(side="left", padx=4)

    def _verify(self):
        key = self._key_var.get().strip()
        if not key:
            self._status.config(text="Please enter a key.", foreground="red")
            return
        self._status.config(text="Verifying...", foreground="grey")
        self.update()
        try:
            info  = GW2Api(key).verify_key()
            perms = info.get("permissions", [])
            if "tradingpost" not in perms:
                self._status.config(
                    text="Key valid, but 'tradingpost' permission is missing.",
                    foreground="red",
                )
                return
            name = info.get("name", "unnamed")
            self._status.config(
                text=f"Verified: '{name}'  |  Permissions: {', '.join(perms)}",
                foreground="#2e7d32",
            )
            self.result = key
            self.after(900, self.destroy)
        except Exception as e:
            self._status.config(text=f"Error: {e}", foreground="red")

# -- Main application ---------------------------------------------------------

COL_META = {
    "Item":       ("item_name",  str),
    "Qty":        ("quantity",   int),
    "Buy Price":  ("buy_price",  int),
    "Sell Price": ("sell_price", int),
    "Profit":     ("profit",     int),
    "Buy Date":   ("buy_date",   str),
    "Sell Date":  ("sell_date",  str),
}
COLS       = list(COL_META.keys())
COL_WIDTHS = [240, 50, 110, 110, 110, 100, 100]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1000x660")
        self.minsize(800, 500)

        self.cfg       = load_config()
        self.db        = Database(DB_FILE)
        self._sort_col = "Sell Date"
        self._sort_asc = False
        self._progress_visible = False

        self._build_ui()
        self._refresh_table()

    def _build_ui(self):
        # Header
        hdr = ttk.Frame(self, padding=(10, 7))
        hdr.pack(fill="x")
        self._key_label = ttk.Label(hdr, text=self._key_display(), foreground="grey")
        self._key_label.pack(side="left")
        ttk.Button(hdr, text="API Key",  command=self._set_api_key).pack(side="left", padx=(10, 4))
        ttk.Button(hdr, text="Sync Now", command=self._sync).pack(side="left")
        self._sync_label = ttk.Label(
            hdr, text="Last sync: " + self.cfg.get("last_sync", "never"),
            foreground="grey"
        )
        self._sync_label.pack(side="right")

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        # Stats
        stats = ttk.Frame(self, padding=(10, 7))
        stats.pack(fill="x")
        self._stats = {k: tk.StringVar(value="--") for k in
                       ("total_profit", "flips", "unique_items", "best_flip")}
        for i, (label, key) in enumerate([
            ("Total Profit",  "total_profit"),
            ("Matched Flips", "flips"),
            ("Unique Items",  "unique_items"),
            ("Best Flip",     "best_flip"),
        ]):
            ttk.Label(stats, text=label + ":").grid(row=0, column=i*2,   sticky="e", padx=(10,2))
            ttk.Label(stats, textvariable=self._stats[key],
                      font=("", 10, "bold")).grid(row=0, column=i*2+1, sticky="w", padx=(0,18))

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        # Filters
        filt = ttk.Frame(self, padding=(10, 6))
        filt.pack(fill="x")
        ttk.Label(filt, text="Item:").pack(side="left")
        self._item_filter = tk.StringVar()
        self._item_filter.trace_add("write", lambda *_: self._refresh_table())
        ttk.Entry(filt, textvariable=self._item_filter, width=24).pack(side="left", padx=(4,14))
        ttk.Label(filt, text="Sold from:").pack(side="left")
        self._date_from = tk.StringVar()
        self._date_from.trace_add("write", lambda *_: self._refresh_table())
        ttk.Entry(filt, textvariable=self._date_from, width=12).pack(side="left", padx=(4,6))
        ttk.Label(filt, text="to:").pack(side="left")
        self._date_to = tk.StringVar()
        self._date_to.trace_add("write", lambda *_: self._refresh_table())
        ttk.Entry(filt, textvariable=self._date_to, width=12).pack(side="left", padx=(4,6))
        ttk.Label(filt, text="YYYY-MM-DD", foreground="grey").pack(side="left", padx=(0,10))
        ttk.Button(filt, text="Clear", command=self._clear_filters).pack(side="left")

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        # Table
        tbl = ttk.Frame(self)
        tbl.pack(fill="both", expand=True, padx=10, pady=6)
        self._tree = ttk.Treeview(tbl, columns=COLS, show="headings", selectmode="browse")
        for col, width in zip(COLS, COL_WIDTHS):
            self._tree.heading(col, text=col, command=lambda c=col: self._sort_by(c))
            self._tree.column(col, width=width,
                              anchor="w" if col == "Item" else "center", minwidth=40)
        vsb = ttk.Scrollbar(tbl, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(tbl, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tbl.rowconfigure(0, weight=1)
        tbl.columnconfigure(0, weight=1)
        self._tree.tag_configure("profit", foreground="#2e7d32")
        self._tree.tag_configure("loss",   foreground="#c62828")

        # Status bar
        bot = ttk.Frame(self, padding=(10, 5))
        bot.pack(fill="x")
        self._status_var = tk.StringVar(value="Ready. Press 'Sync Now' to load your history.")
        ttk.Label(bot, textvariable=self._status_var, foreground="grey").pack(side="left")
        self._progress = ttk.Progressbar(bot, mode="indeterminate", length=140)
        ttk.Button(bot, text="Export CSV", command=self._export_csv).pack(side="right")

    # -- Table ----------------------------------------------------------------

    def _refresh_table(self):
        flips = self.db.get_flips(
            item_filter=self._item_filter.get(),
            date_from=self._date_from.get(),
            date_to=self._date_to.get(),
        )
        field, ftype = COL_META[self._sort_col]
        flips = sorted(
            flips,
            key=lambda r: ftype(r[field]) if r[field] is not None else ftype(),
            reverse=not self._sort_asc,
        )
        self._tree.delete(*self._tree.get_children())
        for r in flips:
            tag = "profit" if r["profit"] >= 0 else "loss"
            self._tree.insert("", "end", values=(
                r["item_name"], r["quantity"],
                copper_to_str(r["buy_price"]),
                copper_to_str(r["sell_price"]),
                copper_to_str(r["profit"]),
                r["buy_date"], r["sell_date"],
            ), tags=(tag,))
        self._update_stats(flips)

    def _update_stats(self, flips: list):
        if not flips:
            for v in self._stats.values():
                v.set("--")
            return
        total  = sum(r["profit"] for r in flips)
        best   = max(r["profit"] for r in flips)
        unique = len({r["item_id"] for r in flips})
        self._stats["total_profit"].set(copper_to_str(total))
        self._stats["flips"].set(str(len(flips)))
        self._stats["unique_items"].set(str(unique))
        self._stats["best_flip"].set(copper_to_str(best))

    def _sort_by(self, col: str):
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        self._refresh_table()

    def _clear_filters(self):
        self._item_filter.set("")
        self._date_from.set("")
        self._date_to.set("")

    def _key_display(self) -> str:
        key = self.cfg.get("api_key", "")
        if not key:
            return "No API key set -- click 'API Key' to get started."
        return f"API key: {key[:8]}...{key[-4:]}"

    # -- API key --------------------------------------------------------------

    def _set_api_key(self):
        dlg = ApiKeyDialog(self, self.cfg.get("api_key", ""))
        if dlg.result:
            self.cfg["api_key"] = dlg.result
            save_config(self.cfg)
            self._key_label.config(text=self._key_display())
            if messagebox.askyesno("Sync now?",
                                   "API key saved. Sync your transaction history now?"):
                self._sync()

    # -- Sync -----------------------------------------------------------------

    def _sync(self):
        key = self.cfg.get("api_key", "")
        if not key:
            messagebox.showwarning("No API key", "Please set your GW2 API key first.")
            self._set_api_key()
            return
        self._set_busy(True, "Starting sync...")
        threading.Thread(target=self._sync_worker, args=(key,), daemon=True).start()

    def _sync_worker(self, key: str):
        api = GW2Api(key)
        try:
            self._set_status("Fetching buy history...")
            buys = api.fetch_all_history(
                "buys",
                progress_cb=lambda n: self._set_status(f"Fetching buys... ({n} so far)")
            )
            self._set_status("Fetching sell history...")
            sells = api.fetch_all_history(
                "sells",
                progress_cb=lambda n: self._set_status(f"Fetching sells... ({n} so far)")
            )
            self._set_status(f"Caching {len(buys)} buys and {len(sells)} sells...")
            self.db.upsert_buys(buys)
            self.db.upsert_sells(sells)

            all_ids = {r["item_id"] for r in buys} | {r["item_id"] for r in sells}
            unknown = self.db.unknown_item_ids(all_ids)
            if unknown:
                self._set_status(f"Resolving {len(unknown)} item names...")
                new_names = api.fetch_item_names(list(unknown))
                self.db.upsert_names(new_names)

            self._set_status("Running FIFO matching...")
            all_names = self.db.get_names()
            flips = fifo_match(self.db.all_buys(), self.db.all_sells(), all_names)
            self.db.replace_flips(flips)

            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            self.cfg["last_sync"] = ts
            save_config(self.cfg)

            n_buys, n_sells = len(buys), len(sells)
            n_flips = len(flips)
            self.after(0, lambda: self._sync_done(
                f"Sync complete -- {n_flips} matched flips from "
                f"{n_buys} buys / {n_sells} sells.", ts
            ))
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            msg  = (f"HTTP {code} from GW2 API.\n\n"
                    "If 401: check your key has 'tradingpost' permission.")
            self.after(0, lambda: self._sync_error(msg))
        except Exception as e:
            err = str(e)
            self.after(0, lambda: self._sync_error(err))

    def _sync_done(self, msg: str, ts: str):
        self._set_busy(False, msg)
        self._sync_label.config(text=f"Last sync: {ts}")
        self._refresh_table()

    def _sync_error(self, msg: str):
        self._set_busy(False, "Sync failed.")
        messagebox.showerror("Sync Error", msg)

    def _set_busy(self, busy: bool, status: str = ""):
        if status:
            self._status_var.set(status)
        if busy and not self._progress_visible:
            self._progress.pack(side="left", padx=10)
            self._progress.start(12)
            self._progress_visible = True
        elif not busy and self._progress_visible:
            self._progress.stop()
            self._progress.pack_forget()
            self._progress_visible = False

    def _set_status(self, msg: str):
        self.after(0, lambda: self._status_var.set(msg))

    # -- Export ---------------------------------------------------------------

    def _export_csv(self):
        flips = self.db.get_flips(
            item_filter=self._item_filter.get(),
            date_from=self._date_from.get(),
            date_to=self._date_to.get(),
        )
        if not flips:
            messagebox.showinfo("Nothing to export", "No flips match the current filters.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile="gw2_flips.csv",
        )
        if path:
            self.db.export_csv(path, flips)
            messagebox.showinfo("Exported", f"Exported {len(flips)} flips to:\n{path}")


# -- Entry point --------------------------------------------------------------

if __name__ == "__main__":
    app = App()
    app.mainloop()
