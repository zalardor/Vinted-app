"""Окно приложения: несколько аккаунтов и ОДНОВРЕМЕННАЯ отправка сообщений продавцу.

Запуск: python gui.py   или   python -m vinted_app gui
"""

from __future__ import annotations

import queue
import sys
import threading

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # на Linux Tkinter ставится отдельно
    tk = None

from . import service
from .service import (
    OK,
    STATUS_TEXT,
    AccountStore,
    add_account,
    check_account,
    login_account,
    normalize_site,
    send_from_account,
)
from .session import VintedError, playwright_error_text

TITLE = "Vinted — сообщения продавцам"
RED = "#c62828"
GREEN = "#2e7d32"
DEFAULT_SITE = "https://vinted.pl"

# Ctrl+C/V/X/A при не-латинской раскладке: Tk не узнаёт эти сочетания, поэтому смотрим на код клавиши
# (Windows и X11 соответственно).
_CTRL_KEYCODES = {
    "<<Paste>>": (86, 55),
    "<<Copy>>": (67, 54),
    "<<Cut>>": (88, 53),
    "<<SelectAll>>": (65, 38),
}


def _on_ctrl_key(event):
    if event.keysym.lower() in ("v", "c", "x", "a"):
        return None  # латинская раскладка — Tk справится сам
    for virtual, codes in _CTRL_KEYCODES.items():
        if event.keycode in codes:
            event.widget.event_generate(virtual)
            return "break"
    return None


def add_clipboard_menu(widget) -> None:
    """Меню «Вырезать / Копировать / Вставить» по правой кнопке мыши."""
    menu = tk.Menu(widget, tearoff=False)
    for label, event in (
        ("Вырезать", "<<Cut>>"),
        ("Копировать", "<<Copy>>"),
        ("Вставить", "<<Paste>>"),
        ("Выделить всё", "<<SelectAll>>"),
    ):
        menu.add_command(label=label, command=lambda e=event: widget.event_generate(e))
    widget.bind("<Button-3>", lambda ev: menu.tk_popup(ev.x_root, ev.y_root))


def _grab_later(dlg) -> None:
    def grab() -> None:
        try:
            dlg.grab_set()
        except tk.TclError:  # окно ещё не показано — пропускаем
            pass

    dlg.after(100, grab)


class App:
    def __init__(self, root, store: AccountStore | None = None) -> None:
        self.root = root
        self.store = store or service.default_store()
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        
        # Словарь для хранения состояний флажков множественного выбора аккаунтов
        self.account_vars: dict[str, tk.BooleanVar] = {}
        # Счетчик активных параллельных потоков отправки
        self.active_sending_threads = 0

        root.title(TITLE)
        root.minsize(740, 700)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        frame = ttk.Frame(root, padding=16)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)

        # --- аккаунты
        accounts_box = ttk.LabelFrame(frame, text="Аккаунты", padding=10)
        accounts_box.grid(row=0, column=0, sticky="ew")
        accounts_box.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            accounts_box, columns=("name", "login", "site", "status"), show="headings", height=5, selectmode="browse"
        )
        for col, title, width in (
            ("name", "Имя", 150),
            ("login", "Логин", 160),
            ("site", "Сайт", 180),
            ("status", "Статус", 150),
        ):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, stretch=True)
        self.tree.grid(row=0, column=0, sticky="ew")
        buttons = ttk.Frame(accounts_box)
        buttons.grid(row=1, column=0, sticky="w", pady=(8, 0))
        for text, command in (
            ("Добавить аккаунт", self.on_add),
            ("Ввести токен", self.on_login),
            ("Проверить все", self.on_check),
            ("Удалить", self.on_remove),
        ):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 6))

        # --- выбор аккаунтов для отправки (Флажки вместо переключателей)
        pick_box = ttk.LabelFrame(frame, text="Отправить с аккаунтов (выберите несколько)", padding=10)
        pick_box.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self.picker = ttk.Frame(pick_box)
        self.picker.pack(anchor="w")

        # --- сообщение
        message_box = ttk.Frame(frame)
        message_box.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        message_box.columnconfigure(0, weight=1)
        ttk.Label(message_box, text="Ссылка на объявление").grid(row=0, column=0, sticky="w")
        self.url = ttk.Entry(message_box)
        self.url.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(message_box, text="Сообщение").grid(row=2, column=0, sticky="w")
        self.message = tk.Text(message_box, height=5, wrap="word", undo=True, relief="solid", borderwidth=1)
        self.message.grid(row=3, column=0, sticky="ew")
        self.send_button = ttk.Button(message_box, text="Отправить одновременно", command=self.on_send)
        self.send_button.grid(row=4, column=0, sticky="e", pady=(8, 0))

        # --- журнал
        ttk.Label(frame, text="Ход работы").grid(row=3, column=0, sticky="w", pady=(12, 0))
        self.log = tk.Text(frame, height=8, wrap="word", state="disabled", relief="solid", borderwidth=1)
        self.log.grid(row=4, column=0, sticky="nsew")
        self.log.tag_configure("error", foreground=RED)
        self.log.tag_configure("ok", foreground=GREEN)

        for widget in (self.url, self.message):
            add_clipboard_menu(widget)
        root.bind_all("<Control-KeyPress>", _on_ctrl_key, add="+")

        self.refresh_accounts()
        root.after(100, self._poll)

    # --- список аккаунтов и множественный выбор ----------------------------------

    def refresh_accounts(self) -> None:
        accounts = self.store.accounts()
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        for a in accounts:
            site = normalize_site(a.base_url).replace("https://", "").replace("http://", "")
            self.tree.insert("", "end", iid=f"acc:{a.name}", values=(a.name, a.login or "—", site, STATUS_TEXT[a.status]))

        for widget in self.picker.winfo_children():
            widget.destroy()
        if not accounts:
            self.account_vars.clear()
            ttk.Label(self.picker, text="Аккаунтов пока нет. Нажмите «Добавить аккаунт».", foreground="gray").pack(
                anchor="w"
            )
            return

        old_vars = self.account_vars.copy()
        self.account_vars.clear()
        
        for a in accounts:
            if a.name in old_vars:
                self.account_vars[a.name] = old_vars[a.name]
            else:
                self.account_vars[a.name] = tk.BooleanVar(value=False)
                
            hint = "" if a.status == OK else f"  — {STATUS_TEXT[a.status]}"
            # Используем Checkbutton вместо Radiobutton для мульти-выбора
            ttk.Checkbutton(
                self.picker, text=f"{a.name}  ({a.login or '—'}){hint}", variable=self.account_vars[a.name]
            ).pack(anchor="w", pady=1)

    def _selected_name(self) -> str | None:
        selected = self.tree.selection()
        return selected[0][len("acc:"):] if selected else None

    # --- связь с фоновыми потоками ------------------------------------------------

    def _post(self, fn) -> None:
        """Выполнить fn в окне (вызывается из фонового потока)."""
        self.events.put(("call", fn))

    def _ask(self, fn):
        """Выполнить fn в окне и дождаться результата (вызывается из фонового потока)."""
        box: dict = {}
        done = threading.Event()

        def run() -> None:
            try:
                box["value"] = fn()
            finally:
                done.set()

        self.events.put(("call", run))
        done.wait()
        return box.get("value")

    def _run_bg(self, fn, *args) -> bool:
        """Запускает системную операцию (добавление/проверка). Не используется для рассылки."""
        if self.busy or self.active_sending_threads > 0:
            messagebox.showinfo(TITLE, "Дождитесь окончания текущей операции.", parent=self.root)
            return False
        self._set_busy(True)

        def runner() -> None:
            try:
                fn(*args)
            except VintedError as e:
                self.events.put(("error", str(e)))
            except Exception as e:
                self.events.put(("error", playwright_error_text(e)))
            finally:
                self._post(self.refresh_accounts)
                self.events.put(("idle", None))

        threading.Thread(target=runner, daemon=True).start()
        return True

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._write(payload)
                elif kind == "ok":
                    self._write(payload, "ok")
                elif kind == "error":
                    self._write(f"Ошибка: {payload}", "error")
                    messagebox.showerror(TITLE, payload, parent=self.root)
                elif kind == "call":
                    try:
                        payload()
                    except Exception as e:
                        self._write(f"Ошибка окна: {e}", "error")
                elif kind == "idle":
                    self._set_busy(False)
                elif kind == "thread_done":
                    self.active_sending_threads -= 1
                    if self.active_sending_threads <= 0:
                        self.active_sending_threads = 0
                        self._set_busy(False)
                        self._write("=== Все одновременные отправки завершены ===")
                        self.refresh_accounts()
        except queue.Empty:
            pass
