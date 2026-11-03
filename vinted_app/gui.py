"""Окно приложения: refresh-токен, ссылка на объявление, сообщение -> отправка продавцу.

Запуск: python gui.py   или   python -m vinted_app gui
"""

from __future__ import annotations

import dataclasses
import os
import queue
import sys
import threading

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # на Linux Tkinter ставится отдельно
    tk = None

from .config import Settings
from .messaging import Listing, get_listing, parse_listing_url, send_message_to_listing
from .session import VintedError, VintedSession, playwright_error_text

TITLE = "Vinted — сообщение продавцу"

# Ctrl+C/V/X/A при русской раскладке: Tk не узнаёт эти сочетания, поэтому смотрим на код клавиши
# (Windows и X11 соответственно).
_CTRL_KEYCODES = {
    "<<Paste>>": (86, 55),
    "<<Copy>>": (67, 54),
    "<<Cut>>": (88, 53),
    "<<SelectAll>>": (65, 38),
}


class App:
    def __init__(self, root: "tk.Tk") -> None:
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.busy = False

        root.title(TITLE)
        root.minsize(560, 520)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        frame = ttk.Frame(root, padding=16)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text="Refresh-токен (cookie refresh_token_web)").grid(row=0, column=0, sticky="w")
        self.token = ttk.Entry(frame, show="•")
        self.token.grid(row=1, column=0, sticky="ew")
        self.token_hint = ttk.Label(frame, text="Можно не вводить, если вход уже сохранён.", foreground="gray")
        self.token_hint.grid(row=2, column=0, sticky="w", pady=(2, 12))

        ttk.Label(frame, text="Ссылка на объявление").grid(row=3, column=0, sticky="w")
        self.url = ttk.Entry(frame)
        self.url.grid(row=4, column=0, sticky="ew", pady=(0, 12))

        ttk.Label(frame, text="Сообщение").grid(row=5, column=0, sticky="w")
        self.message = tk.Text(frame, height=6, wrap="word", undo=True, relief="solid", borderwidth=1)
        self.message.grid(row=6, column=0, sticky="nsew", pady=(0, 12))
        frame.rowconfigure(6, weight=1)

        self.send_button = ttk.Button(frame, text="Отправить", command=self.on_send)
        self.send_button.grid(row=7, column=0, sticky="e")

        ttk.Label(frame, text="Ход работы").grid(row=8, column=0, sticky="w", pady=(12, 0))
        self.log = tk.Text(frame, height=8, wrap="word", state="disabled", relief="solid", borderwidth=1,
                           background=root.cget("background"))
        self.log.grid(row=9, column=0, sticky="nsew")
        self.log.tag_configure("error", foreground="#c62828")
        self.log.tag_configure("ok", foreground="#2e7d32")
        frame.rowconfigure(9, weight=1)

        for widget in (self.token, self.url, self.message):
            self._add_context_menu(widget)
        root.bind_all("<Control-KeyPress>", self._on_ctrl_key, add="+")
        self.url.focus_set()
        root.after(100, self._poll)

    # --- удобство ввода ---------------------------------------------------------

    def _add_context_menu(self, widget: "tk.Widget") -> None:
        menu = tk.Menu(widget, tearoff=False)
        for label, event in (("Вырезать", "<<Cut>>"), ("Копировать", "<<Copy>>"), ("Вставить", "<<Paste>>"),
                             ("Выделить всё", "<<SelectAll>>")):
            menu.add_command(label=label, command=lambda e=event: widget.event_generate(e))
        widget.bind("<Button-3>", lambda ev: menu.tk_popup(ev.x_root, ev.y_root))

    @staticmethod
    def _on_ctrl_key(event: "tk.Event") -> str | None:
        if event.keysym.lower() in ("v", "c", "x", "a"):
            return None  # латинская раскладка — Tk справится сам
        for virtual, codes in _CTRL_KEYCODES.items():
            if event.keycode in codes:
                event.widget.event_generate(virtual)
                return "break"
        return None

    # --- журнал и состояние -----------------------------------------------------

    def _write(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", (tag,) if tag else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.send_button.configure(state="disabled" if busy else "normal", text="Подождите..." if busy else "Отправить")

    def _emit(self, kind: str, payload: object = None) -> None:
        self.events.put((kind, payload))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._write(str(payload))
                elif kind == "ok":
                    self._write(str(payload), "ok")
                elif kind == "error":
                    self._write(f"Ошибка: {payload}", "error")
                    messagebox.showerror(TITLE, str(payload), parent=self.root)
                elif kind == "token_used":
                    self.token.delete(0, "end")
                    self.token_hint.configure(text="Вход сохранён — токен больше не нужен.")
                elif kind == "confirm":
                    listing, text, answer, done = payload
                    answer["ok"] = messagebox.askyesno(
                        "Отправить сообщение?",
                        f"Продавец: {listing.seller_login or '?'} (id {listing.seller_id})\n"
                        f"Объявление: {listing.title}\n\nСообщение:\n{text}",
                        parent=self.root,
                    )
                    done.set()
                elif kind == "idle":
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # --- отправка ---------------------------------------------------------------

    def on_send(self) -> None:
        if self.busy:
            return
        token = self.token.get().strip()
        url = self.url.get().strip()
        text = self.message.get("1.0", "end").strip()
        if not url:
            messagebox.showwarning(TITLE, "Вставьте ссылку на объявление.", parent=self.root)
            return
        if not text:
            messagebox.showwarning(TITLE, "Введите текст сообщения.", parent=self.root)
            return
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._set_busy(True)
        threading.Thread(target=self._work, args=(token, url, text), daemon=True).start()

    def _confirm(self, listing: Listing, text: str) -> bool:
        answer: dict = {}
        done = threading.Event()
        self._emit("confirm", (listing, text, answer, done))
        done.wait()
        return bool(answer.get("ok"))

    def _work(self, token: str, url: str, text: str) -> None:
        """Выполняется в фоновом потоке: вся работа с браузером — здесь."""
        try:
            base_url, item_id = parse_listing_url(url)
            settings = Settings.from_env()
            settings = dataclasses.replace(settings, base_url=(os.environ.get("VINTED_BASE_URL") or base_url).rstrip("/"))
            self._emit("log", f"Открываю {settings.base_url} ...")
            with VintedSession(settings) as vs:
                user = vs.ensure_login(token or None)
                if token:
                    self._emit("token_used")
                self._emit("log", f"Вы вошли как {user.get('login', '?')} (id {user.get('id', '?')}).")
                listing = get_listing(vs, item_id)
                self._emit("log", f"Объявление: {listing.title}")
                self._emit("log", f"Продавец: {listing.seller_login or '?'} (id {listing.seller_id})")
                if not self._confirm(listing, text):
                    self._emit("log", "Отменено — ничего не отправлено.")
                    return
                self._emit("log", "Отправляю...")
                result = send_message_to_listing(vs, listing, text)
                if result.verified:
                    self._emit("ok", f"✓ Сообщение отправлено продавцу {listing.seller_login or listing.seller_id}.")
                else:
                    self._emit("log", "Сообщение отправлено, но найти его в чате для проверки не удалось.")
        except VintedError as e:
            self._emit("error", str(e))
        except Exception as e:  # ошибки браузера (Playwright) и прочее
            self._emit("error", playwright_error_text(e))
        finally:
            self._emit("idle")


def main() -> int:
    if tk is None:
        print(
            "Для окна приложения нужен Tkinter.\n"
            "  Linux:   sudo apt install python3-tk\n"
            "  Windows/macOS: он входит в стандартный установщик Python с python.org.\n"
            "Пока его нет, используйте терминал: python -m vinted_app send <ссылка> <сообщение>",
            file=sys.stderr,
        )
        return 2
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())