"""Окно приложения: несколько аккаунтов и отправка сообщения продавцу с одного или нескольких выбранных аккаунтов.

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
DEFAULT_SITE = "https://www.vinted.pl"

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
        self.picked: dict[str, tk.BooleanVar] = {}  # имя аккаунта -> отмечен ли он для отправки

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

        # --- выбор аккаунтов для отправки: можно отметить несколько
        pick_box = ttk.LabelFrame(frame, text="Отправить с аккаунтов (отметьте один или несколько)", padding=10)
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
        self.send_button = ttk.Button(message_box, text="Отправить", command=self.on_send)
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

    # --- список аккаунтов и выбор -------------------------------------------------

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
            self.picked.clear()
            ttk.Label(self.picker, text="Аккаунтов пока нет. Нажмите «Добавить аккаунт».", foreground="gray").pack(
                anchor="w"
            )
            return

        # Первый запуск (список выбора пуст) — сразу отмечаем первый аккаунт, как раньше.
        first_time = not self.picked
        names = {a.name for a in accounts}
        for name in list(self.picked):
            if name not in names:
                del self.picked[name]
        for a in accounts:
            if a.name not in self.picked:
                self.picked[a.name] = tk.BooleanVar(value=first_time and a.name == accounts[0].name)
        for a in accounts:
            hint = "" if a.status == OK else f"  — {STATUS_TEXT[a.status]}"
            ttk.Checkbutton(
                self.picker, text=f"{a.name}  ({a.login or '—'}){hint}", variable=self.picked[a.name]
            ).pack(anchor="w", pady=1)

    def _selected_name(self) -> str | None:
        selected = self.tree.selection()
        return selected[0][len("acc:"):] if selected else None

    def _picked_names(self) -> list[str]:
        """Имена отмеченных для отправки аккаунтов."""
        return [name for name, var in self.picked.items() if var.get()]

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
        """Запускает долгую операцию в фоне. Одновременно идёт только одна."""
        if self.busy:
            messagebox.showinfo(TITLE, "Дождитесь окончания текущей операции.", parent=self.root)
            return False
        self._set_busy(True)

        def runner() -> None:
            try:
                fn(*args)
            except VintedError as e:
                self.events.put(("error", str(e)))
            except Exception as e:  # ошибки браузера и прочее
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
                elif kind == "log_error":
                    self._write(payload, "error")
                elif kind == "error":
                    self._write(f"Ошибка: {payload}", "error")
                    messagebox.showerror(TITLE, payload, parent=self.root)
                elif kind == "call":
                    try:
                        payload()
                    except Exception as e:  # одна ошибка не должна останавливать окно
                        self._write(f"Ошибка окна: {e}", "error")
                elif kind == "idle":
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.send_button.configure(state="disabled" if busy else "normal")

    def _write(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", (tag,) if tag else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # --- аккаунты: добавить, войти, проверить, удалить ---------------------------

    def on_add(self) -> None:
        self._account_dialog(None)

    def on_login(self) -> None:
        name = self._selected_name()
        if name is None:
            messagebox.showinfo(TITLE, "Сначала выберите аккаунт в списке.", parent=self.root)
            return
        self._account_dialog(name)

    def on_check(self) -> None:
        accounts = self.store.accounts()
        if not accounts:
            messagebox.showinfo(TITLE, "Аккаунтов пока нет.", parent=self.root)
            return
        self._clear_log()

        def work() -> None:
            for a in accounts:
                try:
                    acc = check_account(self.store, a.name, headless=True)
                    extra = f" ({acc.login})" if acc.login else ""
                    self.events.put(("log", f"{acc.name}: {STATUS_TEXT[acc.status]}{extra}"))
                except VintedError as e:
                    self.events.put(("log", f"{a.name}: проверка не удалась — {e}"))

        self._run_bg(work)

    def on_remove(self) -> None:
        name = self._selected_name()
        if name is None:
            messagebox.showinfo(TITLE, "Сначала выберите аккаунт в списке.", parent=self.root)
            return
        if self.busy:
            messagebox.showinfo(TITLE, "Дождитесь окончания текущей операции.", parent=self.root)
            return
        if not messagebox.askyesno(
            TITLE,
            f"Удалить аккаунт «{name}» из приложения?\nСохранённый вход на этом компьютере тоже будет стёрт.",
            parent=self.root,
        ):
            return
        try:
            self.store.remove(name)
        except VintedError as e:
            messagebox.showerror(TITLE, str(e), parent=self.root)
        self.refresh_accounts()

    def _account_dialog(self, name: str | None) -> None:
        """Окно добавления аккаунта (name=None) или нового входа в существующий."""
        adding = name is None
        dlg = tk.Toplevel(self.root)
        dlg.title("Новый аккаунт" if adding else f"Вход в «{name}»")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        form = ttk.Frame(dlg, padding=16)
        form.pack(fill="both", expand=True)

        name_entry = site_entry = None
        if adding:
            ttk.Label(form, text="Имя аккаунта в приложении (например, «Я» или «Аня»)").pack(anchor="w")
            name_entry = ttk.Entry(form, width=52)
            name_entry.pack(anchor="w", fill="x", pady=(2, 10))
            ttk.Label(form, text="Сайт Vinted этого аккаунта").pack(anchor="w")
            site_entry = ttk.Entry(form, width=52)
            site_entry.insert(0, DEFAULT_SITE)
            site_entry.pack(anchor="w", fill="x", pady=(2, 10))
        else:
            ttk.Label(form, text=f"Аккаунт: {name}").pack(anchor="w", pady=(0, 10))

        ttk.Label(form, text="Refresh-токен (cookie refresh_token_web)").pack(anchor="w")
        token_entry = ttk.Entry(form, width=52, show="•")
        token_entry.pack(anchor="w", fill="x", pady=(2, 10))

        consent = tk.BooleanVar(value=not adding)
        hint = ttk.Label(form, text="", foreground=RED, wraplength=460)
        if adding:
            ttk.Checkbutton(
                form, text="Аккаунт мой или владелец дал согласие на его использование", variable=consent
            ).pack(anchor="w")
            ttk.Label(
                form,
                text="Передача доступа к аккаунту нарушает правила Vinted. "
                "Ответственность за аккаунт несёт тот, кто им пользуется.",
                foreground=RED,
                wraplength=460,
            ).pack(anchor="w", pady=(4, 0))
        hint.pack(anchor="w", pady=(6, 0))

        def submit(_event=None) -> None:
            token = token_entry.get().strip()
            if adding:
                new_name = name_entry.get().strip()
                site = site_entry.get().strip()
                if not new_name:
                    hint.configure(text="Введите имя аккаунта.")
                    return
                if not consent.get():
                    hint.configure(text="Отметьте галочку согласия.")
                    return
            if not token:
                hint.configure(text="Вставьте refresh-токен.")
                return
            hint.configure(text="")

            def finish(text: str) -> None:
                self.refresh_accounts()
                dlg.destroy()
                messagebox.showinfo(TITLE, text, parent=self.root)

            def work() -> None:
                if adding:
                    acc = add_account(self.store, new_name, site, token, consent=True, headless=True)
                    text = f"Аккаунт «{acc.name}» добавлен: {acc.login} (id {acc.user_id})."
                else:
                    acc = login_account(self.store, name, token, headless=True)
                    text = f"Вход в «{acc.name}» выполнен: {acc.login}."
                self._post(lambda: finish(text))

            self._run_bg(work)

        buttons = ttk.Frame(form)
        buttons.pack(anchor="e", pady=(10, 0))
        ttk.Button(buttons, text="Отмена", command=dlg.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Войти и сохранить" if adding else "Войти", command=submit).pack(side="right")
        token_entry.bind("<Return>", submit)
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        add_clipboard_menu(token_entry)
        if adding:
            add_clipboard_menu(name_entry)
            add_clipboard_menu(site_entry)
            name_entry.focus_set()
        else:
            token_entry.focus_set()
        _grab_later(dlg)

    # --- отправка -----------------------------------------------------------------

    def on_send(self) -> None:
        names = self._picked_names()
        url = self.url.get().strip()
        text = self.message.get("1.0", "end").strip()
        if not names:
            messagebox.showwarning(TITLE, "Добавьте аккаунт и отметьте, с каких аккаунтов отправить.", parent=self.root)
            return
        if not url:
            messagebox.showwarning(TITLE, "Вставьте ссылку на объявление.", parent=self.root)
            return
        if not text:
            messagebox.showwarning(TITLE, "Введите текст сообщения.", parent=self.root)
            return
        self._clear_log()

        def work() -> None:
            def log(line: str) -> None:
                self.events.put(("log", line))

            def confirm(listing, account) -> bool:
                question = (
                    f"С аккаунта: {account.name} ({account.login or '—'})\n"
                    f"Продавец: {listing.seller_login or '?'} (id {listing.seller_id})\n"
                    f"Объявление: {listing.title}\n\nСообщение:\n{text}"
                )
                return bool(self._ask(lambda: messagebox.askyesno("Отправить сообщение?", question, parent=self.root)))

            sent = 0
            for name in names:
                # Ошибка одного аккаунта не должна мешать остальным.
                try:
                    result = send_from_account(self.store, name, url, text, confirm=confirm, log=log, headless=True)
                except VintedError as e:
                    self.events.put(("log_error", f"«{name}»: {e}"))
                    continue
                except Exception as e:  # ошибки браузера и прочее
                    self.events.put(("log_error", f"«{name}»: {playwright_error_text(e)}"))
                    continue
                if result is None:
                    log(f"«{name}»: отправка отменена.")
                    continue
                sent += 1
                seller = result.listing.seller_login or result.listing.seller_id
                if result.verified:
                    self.events.put(("ok", f"✓ Сообщение отправлено с аккаунта «{name}» продавцу {seller}."))
                else:
                    log(f"«{name}»: сообщение отправлено, но подтвердить его в чате не удалось.")
            if len(names) > 1:
                log(f"Готово: отправлено с {sent} из {len(names)} аккаунтов.")

        self._run_bg(work)


def main() -> int:
    if tk is None:
        print(
            "Для окна приложения нужен Tkinter.\n"
            "  Linux:   sudo apt install python3-tk\n"
            "  Windows/macOS: он входит в стандартный установщик Python с python.org.\n"
            "Пока его нет, используйте терминал: python -m vinted_app send <ссылка> <сообщение> --account <имя>",
            file=sys.stderr,
        )
        return 2
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0
