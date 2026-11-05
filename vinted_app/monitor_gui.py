"""Окно «Монитор объявлений»: новые объявления по ссылке поиска Vinted.

Открывается кнопкой «Монитор объявлений» в главном окне. Ничего не покупает и никому не пишет.
Фото показываются, если установлен pillow (pip install pillow). Без него слежение работает, фото нет.
"""

from __future__ import annotations

import io
import queue
import threading
import webbrowser

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # на Linux Tkinter ставится отдельно
    tk = None

from .config import Settings
from .monitor import Item, normalize_search_url, watch
from .session import VintedError

MAX_CARDS = 200   # сколько карточек держать на экране
PHOTO_SIZE = 180  # сторона фото в пикселях


def _pillow_available() -> bool:
    try:
        import PIL.ImageTk  # noqa: F401
    except ImportError:
        return False
    return True


def _photo_image(data: bytes | None, master):
    """Фото для карточки. Без pillow или при ошибке возвращает None: карточка покажется без картинки."""
    if not data or not _pillow_available():
        return None
    from PIL import Image, ImageTk

    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((PHOTO_SIZE, PHOTO_SIZE))
        return ImageTk.PhotoImage(img, master=master)
    except Exception:  # битая картинка не должна ломать окно
        return None


BROWSER_HINT = (
    "Ошибка: не установлен браузер для проверок. В Терминале в папке проекта выполните "
    "python -m playwright install chromium и запустите окно заново."
)


def _friendly_error(exc: Exception) -> str:
    """Понятный текст вместо технической ошибки. Самая частая причина — не установлен браузер Playwright."""
    text = str(exc)
    low = text.lower()
    if "executable doesn't exist" in low or "playwright install" in low:
        return BROWSER_HINT
    first_line = text.splitlines()[0] if text else type(exc).__name__
    return f"Ошибка: {first_line}"


class MonitorWindow:
    def __init__(self, master) -> None:
        self.top = tk.Toplevel(master)
        self.top.title("Монитор объявлений — Vinted")
        self.top.minsize(680, 560)
        self.top.protocol("WM_DELETE_WINDOW", self._on_close)
        self.events: queue.Queue = queue.Queue()
        self.stop_event: threading.Event | None = None
        self.running = False
        self.cards: list = []  # самая новая карточка — первая

        bar = ttk.Frame(self.top, padding=(12, 12, 12, 4))
        bar.pack(fill="x")
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="Ссылка поиска Vinted (фильтры цены, размера и бренда берутся из неё)").grid(
            row=0, column=0, columnspan=4, sticky="w"
        )
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(bar, textvariable=self.url_var)
        self.url_entry.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(2, 8))
        ttk.Label(bar, text="Проверять каждые, мин").grid(row=2, column=0, sticky="w")
        self.minutes_var = tk.StringVar(value="5")
        ttk.Spinbox(bar, from_=2, to=60, width=5, textvariable=self.minutes_var).grid(row=2, column=1, sticky="w")
        self.start_button = ttk.Button(bar, text="Старт", command=self.start)
        self.start_button.grid(row=2, column=2, sticky="e", padx=(8, 0))
        self.stop_button = ttk.Button(bar, text="Стоп", command=self.stop, state="disabled")
        self.stop_button.grid(row=2, column=3, sticky="e", padx=(6, 0))

        self.status = ttk.Label(self.top, text="Вставьте ссылку поиска и нажмите «Старт».", padding=(12, 0, 12, 6))
        self.status.pack(fill="x")

        holder = ttk.Frame(self.top, padding=(12, 0, 12, 12))
        holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(holder, highlightthickness=0)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

        self.url_entry.focus_set()
        self.top.after(100, self._poll)

    # --- окно ----------------------------------------------------------------------

    def exists(self) -> bool:
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def raise_window(self) -> None:
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()

    def _on_close(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
        self._unbind_wheel()
        self.top.destroy()

    def _bind_wheel(self, _event=None) -> None:
        self.top.bind_all("<MouseWheel>", self._on_wheel)
        self.top.bind_all("<Button-4>", lambda _e: self.canvas.yview_scroll(-1, "units"))
        self.top.bind_all("<Button-5>", lambda _e: self.canvas.yview_scroll(1, "units"))

    def _unbind_wheel(self, _event=None) -> None:
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.top.unbind_all(sequence)

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _status(self, text: str) -> None:
        self.status.configure(text=text)

    def _set_running(self, on: bool) -> None:
        self.running = on
        self.start_button.configure(state="disabled" if on else "normal")
        self.stop_button.configure(state="normal" if on else "disabled")
        self.url_entry.configure(state="disabled" if on else "normal")

    # --- слежение --------------------------------------------------------------------

    def start(self) -> None:
        if self.running:
            return
        try:
            _base, url = normalize_search_url(self.url_var.get())
            minutes = int(float((self.minutes_var.get() or "5").replace(",", ".")))
        except VintedError as e:
            self._status(str(e))
            return
        except ValueError:
            self._status("Введите число минут, например 5.")
            return
        notes = []
        if minutes < 2:
            minutes = 2
            self.minutes_var.set("2")
            notes.append("Чаще двух минут не проверяю: от частых запросов Vinted блокирует.")
        if not _pillow_available():
            notes.append("Фото не покажутся: установите pillow (pip install pillow).")
        self.stop_event = threading.Event()
        self._set_running(True)
        self._status(" ".join(["Запускаю монитор..."] + notes))
        threading.Thread(target=self._worker, args=(url, minutes * 60, self.stop_event), daemon=True).start()

    def stop(self) -> None:
        if self.stop_event is not None and not self.stop_event.is_set():
            self.stop_event.set()
            self._status("Останавливаю после текущей проверки...")

    def _worker(self, url: str, seconds: int, stop_event: threading.Event) -> None:
        """Фоновый поток: вся работа с браузером — здесь. В окно передаются только готовые результаты."""
        try:
            code = watch(
                Settings.from_env(),
                url,
                interval=seconds,
                fetch_images=True,
                headless=True,
                on_item=lambda item, image: self.events.put(("item", (item, image))),
                on_status=lambda text: self.events.put(("status", text)),
                stop=stop_event,
            )
            self.events.put(
                ("status", "Остановлено." if code == 0 else "Монитор остановлен: Vinted блокирует проверки.")
            )
        except VintedError as e:
            self.events.put(("status", f"Ошибка: {e}"))
        except Exception as e:  # ошибки браузера и прочее
            self.events.put(("status", _friendly_error(e)))
        finally:
            self.events.put(("idle", None))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "item":
                    item, image = payload
                    try:
                        self._add_card(item, image)
                    except Exception as e:  # одна карточка не должна останавливать окно
                        self._status(f"Ошибка окна: {e}")
                elif kind == "status":
                    self._status(payload)
                elif kind == "idle":
                    self._set_running(False)
        except queue.Empty:
            pass
        if self.exists():
            self.top.after(100, self._poll)

    # --- карточки --------------------------------------------------------------------

    def _add_card(self, item: Item, image: bytes | None) -> None:
        card = ttk.Frame(self.inner, padding=10, relief="solid", borderwidth=1)
        photo = _photo_image(image, self.top)
        if photo is not None:
            picture = ttk.Label(card, image=photo)
            picture.image = photo  # иначе картинку удалит сборщик мусора
        else:
            picture = ttk.Label(card, text="нет фото", width=24, anchor="center")
        picture.grid(row=0, column=0, rowspan=3, padx=(0, 12), sticky="n")
        ttk.Label(card, text=item.title, wraplength=420, justify="left").grid(row=0, column=1, sticky="w")
        price = f"{item.price:g} {item.currency}" if item.price is not None else "цена не определена"
        ttk.Label(card, text=price).grid(row=1, column=1, sticky="w", pady=(4, 6))
        ttk.Button(card, text="Открыть объявление", command=lambda url=item.url: webbrowser.open(url)).grid(
            row=2, column=1, sticky="w"
        )
        self.cards.insert(0, card)
        while len(self.cards) > MAX_CARDS:
            self.cards.pop().destroy()
        self._relayout()

    def _relayout(self) -> None:
        for card in self.cards:
            card.pack_forget()
        for card in self.cards:  # порядок упаковки = порядок на экране: новые сверху
            card.pack(fill="x", pady=6)
