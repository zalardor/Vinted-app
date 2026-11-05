"""Монитор объявлений: сообщает о новых объявлениях по ссылке поиска Vinted.

Что делает: открывает страницу поиска так же, как обычный посетитель сайта, запоминает уже показанные
объявления и сообщает о появившихся. Что не делает: не покупает, никому не пишет, не использует аккаунты,
не меняет адрес и не использует прокси. Если Vinted показывает проверку, монитор ждёт дольше, а после
нескольких неудач останавливается.
"""

from __future__ import annotations

import dataclasses
import hashlib
import html as htmllib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .config import Settings
from .session import VintedError, VintedSession

MIN_INTERVAL = 120      # секунд между проверками, меньше не будет
MAX_BACKOFF = 1800      # верхняя граница паузы после проверок
MAX_BLOCKS = 5          # столько проверок подряд с блокировкой — и монитор останавливается
MAX_NEW_PER_CYCLE = 20  # сколько новых карточек открыть за одну проверку
ITEM_PAUSE = 1.5        # пауза между открытиями карточек
SEEN_LIMIT = 2000       # сколько id объявлений помнить

_ITEM_LINK = re.compile(r"/items/(\d+)-")
_PRICE = re.compile(
    r'\\?"price\\?":\s*\{\s*\\?"amount\\?":\s*\\?"([0-9.]+)\\?",\s*\\?"currency_code\\?":\s*\\?"([A-Z]{3})\\?"'
)


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    price: float | None
    currency: str | None
    url: str
    image_url: str | None = None


def _meta(page_html: str, prop: str) -> str | None:
    """Значение <meta property="..." content="...">. Порядок атрибутов не важен."""
    for tag in re.findall(r"<meta[^>]*>", page_html):
        if f'"{prop}"' in tag:
            found = re.search(r'content="([^"]*)"', tag)
            if found:
                return htmllib.unescape(found.group(1))
    return None


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def normalize_search_url(text: str) -> tuple[str, str]:
    """Ссылка поиска Vinted -> (адрес сайта, та же ссылка, но с сортировкой «сначала новые»)."""
    raw = (text or "").strip()
    if not raw:
        raise VintedError("Вставьте ссылку поиска Vinted, например https://www.vinted.pl/catalog?search_text=kurtka")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if "vinted." not in (parsed.hostname or ""):
        raise VintedError("Это не ссылка на Vinted.")
    if not parsed.path.rstrip("/").startswith("/catalog"):
        raise VintedError("Нужна ссылка на страницу поиска (путь /catalog), а не на объявление или профиль.")
    params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "order"]
    params.append(("order", "newest_first"))
    url = urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", urlencode(params), ""))
    return _origin(url), url


def search_url(base_url: str, query: str) -> str:
    return f"{base_url.rstrip('/')}/catalog?" + urlencode({"search_text": query, "order": "newest_first"})


def parse_item_ids(page_html: str) -> list[str]:
    """Id объявлений со страницы поиска в порядке появления (сначала новые)."""
    ids: list[str] = []
    for match in _ITEM_LINK.finditer(page_html):
        if match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


def parse_item(page_html: str, item_id: str, base_url: str) -> Item:
    title = _meta(page_html, "og:title")
    title = title.removesuffix(" | Vinted").strip() if title else f"объявление {item_id}"
    price_match = _PRICE.search(page_html)
    return Item(
        id=item_id,
        title=title,
        price=float(price_match.group(1)) if price_match else None,
        currency=price_match.group(2) if price_match else None,
        url=f"{base_url.rstrip('/')}/items/{item_id}",
        image_url=_meta(page_html, "og:image"),
    )


def _challenge_title(title: str) -> bool:
    low = title.lower()
    return "just a moment" in low or "cierpliwo" in low or "verifying" in low


class _State:
    """Какие объявления уже видели и прошла ли первая проверка. Файл monitor/<ключ>.json."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.seen: list[str] = []
        self.baseline = False
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.seen = list(data.get("seen", []))
            self.baseline = bool(data.get("baseline", False))

    def remember(self, item_ids: list[str]) -> None:
        for item_id in item_ids:
            if item_id not in self.seen:
                self.seen.append(item_id)
        self.seen = self.seen[-SEEN_LIMIT:]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"seen": self.seen, "baseline": self.baseline}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


def _fetch_image(vs: VintedSession, image_url: str) -> bytes | None:
    try:
        resp = vs.context.request.get(image_url)
    except Exception:  # фото необязательно: объявление придёт и без него
        return None
    return resp.body() if resp.status == 200 else None


def _check(
    vs: VintedSession,
    url: str,
    state: _State,
    max_price: float | None,
    fetch_images: bool,
    stop: threading.Event,
    pause: Callable[[float], object],
) -> tuple[list[tuple[Item, bytes | None]], str | None]:
    """Одна проверка. Возвращает (новые объявления с фото, причина остановки или None)."""
    base = _origin(url)
    vs.page.goto(url, wait_until="domcontentloaded")
    vs.page.wait_for_timeout(5000)
    if _challenge_title(vs.page.title()):
        return [], "Vinted показал проверку «подождите»."
    ids = parse_item_ids(vs.page.content())

    if not state.baseline:
        state.remember(ids)  # первая проверка только запоминает текущие объявления
        state.baseline = True
        return [], None

    fresh = [i for i in ids if i not in state.seen][:MAX_NEW_PER_CYCLE]
    found: list[tuple[Item, bytes | None]] = []
    for n, item_id in enumerate(fresh):
        if stop.is_set():
            break
        if n:
            pause(ITEM_PAUSE)
        resp = vs.context.request.get(f"{base}/items/{item_id}", headers={"accept": "text/html"})
        body = resp.text()
        if resp.status in (403, 429) or ("captcha-delivery" in body and "og:title" not in body):
            return found, "Карточка объявления закрыта проверкой."
        state.remember([item_id])  # отмечаем, даже если отфильтруем — чтобы не открывать второй раз
        if resp.status != 200:
            continue
        item = parse_item(body, item_id, base)
        if max_price is not None and item.price is not None and item.price > max_price:
            continue
        image = _fetch_image(vs, item.image_url) if fetch_images and item.image_url else None
        found.append((item, image))
    return found, None


def _format(item: Item) -> str:
    price = f"{item.price:g} {item.currency}" if item.price is not None else "цена не определена"
    return f"{datetime.now():%H:%M} | {price} | {item.title} | {item.url}"


def watch(
    settings: Settings,
    url: str,
    *,
    max_price: float | None = None,
    interval: int = 300,
    once: bool = False,
    max_cycles: int | None = None,
    headless: bool = True,
    fetch_images: bool = False,
    state_dir: Path | None = None,
    on_item: Callable[[Item, bytes | None], None],
    on_status: Callable[[str], None],
    stop: threading.Event | None = None,
    sleep: Callable[[float], object] | None = None,
) -> int:
    """Следит за страницей поиска и сообщает о новых объявлениях.

    Возвращает код выхода: 0 — штатно или остановлено пользователем, 3 — остановлено из-за блокировок.
    """
    stop = stop or threading.Event()
    wait_for = sleep or (lambda seconds: stop.wait(seconds))
    base = _origin(url)
    state_dir = state_dir or Path(os.environ.get("VINTED_MONITOR_DIR", "monitor"))
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    state = _State(state_dir / f"{key}.json")
    log_path = state_dir / f"{key}.log"
    base_wait = max(MIN_INTERVAL, int(interval))
    if interval < MIN_INTERVAL:
        on_status(f"Пауза {interval} с слишком короткая, ставлю {MIN_INTERVAL} с.")
    wait = base_wait
    blocks = 0
    cycles = 0

    on_status("Слежу за поиском. Новые объявления появятся ниже.")
    if not state.baseline:
        on_status("Первая проверка запомнит текущие объявления. Новые будут приходить позже.")

    session_settings = dataclasses.replace(settings, base_url=base, profile_dir=state_dir / "profile")
    with VintedSession(session_settings, headless=headless) as vs:
        while not stop.is_set():
            cycles += 1
            found, reason = _check(vs, url, state, max_price, fetch_images, stop, wait_for)
            # Внутри проверки объявления идут от новых к старым. Передаём от старых к новым,
            # чтобы в окне самое новое оказалось сверху, а в журнале записи шли по времени.
            for item, image in reversed(found):
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(_format(item) + "\n")
                on_item(item, image)
            state.save()

            if reason:
                blocks += 1
                if blocks >= MAX_BLOCKS:
                    on_status("Vinted блокирует проверки несколько раз подряд. Монитор остановлен, попробуйте позже.")
                    return 3
                if once:
                    on_status(f"{datetime.now():%H:%M} | {reason} Попробуйте позже.")
                else:
                    wait = min(wait * 2, MAX_BACKOFF)
                    on_status(f"{datetime.now():%H:%M} | {reason} Следующая проверка через {wait // 60} мин.")
            else:
                blocks = 0
                wait = base_wait
                if not found:
                    on_status(f"{datetime.now():%H:%M} | новых нет")

            if once or (max_cycles is not None and cycles >= max_cycles):
                break
            wait_for(wait)
    return 0


def run_monitor(
    settings: Settings,
    query: str,
    *,
    max_price: float | None = None,
    interval: int = 300,
    once: bool = False,
    max_cycles: int | None = None,
    headless: bool = True,
    state_dir: Path | None = None,
    notify: Callable[[str], None] = print,
    sleep: Callable[[float], object] = time.sleep,
) -> int:
    """Терминальная версия: следит за поиском по тексту и печатает новые объявления."""
    query = query.strip()
    if not query:
        raise VintedError("Укажите, что искать, например: «kurtka zara».")
    notify(f"Запрос «{query}»" + (f", цена до {max_price:g}" if max_price is not None else "") + ". Остановить: Ctrl+C.")
    return watch(
        settings,
        search_url(settings.base_url, query),
        max_price=max_price,
        interval=interval,
        once=once,
        max_cycles=max_cycles,
        headless=headless,
        fetch_images=False,
        state_dir=state_dir,
        on_item=lambda item, image: notify(_format(item)),
        on_status=notify,
        sleep=sleep,
    )
