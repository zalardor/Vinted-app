"""Монитор объявлений: уведомляет о новых объявлениях по поисковому запросу.

Что делает: смотрит результаты поиска Vinted так же, как обычный посетитель сайта, и сообщает о новых
объявлениях. Что не делает: не покупает, никому не пишет, не использует аккаунты и не обходит проверки сайта.
Если Vinted показывает проверку, монитор увеличивает паузу, а после нескольких неудач останавливается.
"""

from __future__ import annotations

import dataclasses
import hashlib
import html as htmllib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from .config import Settings
from .session import VintedError, VintedSession

MIN_INTERVAL = 120     # секунд между проверками, меньше не будет
MAX_BACKOFF = 1800     # верхняя граница паузы после проверок
MAX_BLOCKS = 5         # столько проверок подряд с блокировкой — и монитор останавливается
MAX_NEW_PER_CYCLE = 20 # сколько новых карточек открыть за одну проверку
ITEM_PAUSE = 1.5       # пауза между открытиями карточек
SEEN_LIMIT = 2000      # сколько id объявлений помнить

_ITEM_LINK = re.compile(r"/items/(\d+)-")
_OG_TITLE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"')
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
    title_match = _OG_TITLE.search(page_html)
    title = (
        htmllib.unescape(title_match.group(1)).removesuffix(" | Vinted").strip()
        if title_match
        else f"объявление {item_id}"
    )
    price_match = _PRICE.search(page_html)
    return Item(
        id=item_id,
        title=title,
        price=float(price_match.group(1)) if price_match else None,
        currency=price_match.group(2) if price_match else None,
        url=f"{base_url.rstrip('/')}/items/{item_id}",
    )


def _challenge_title(title: str) -> bool:
    low = title.lower()
    return "just a moment" in low or "cierpliwo" in low or "verifying" in low


class _State:
    """Какие объявления уже видели и прошла ли первая проверка. Файл monitor/<запрос>.json."""

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


def _check(
    vs: VintedSession,
    base: str,
    query: str,
    state: _State,
    max_price: float | None,
    pause: Callable[[float], None],
) -> tuple[list[Item], str | None]:
    """Одна проверка. Возвращает (новые объявления, причина остановки или None)."""
    vs.page.goto(search_url(base, query), wait_until="domcontentloaded")
    vs.page.wait_for_timeout(5000)
    if _challenge_title(vs.page.title()):
        return [], "Vinted показал проверку «подождите»."
    ids = parse_item_ids(vs.page.content())

    if not state.baseline:
        state.remember(ids)  # первая проверка только запоминает текущие объявления
        state.baseline = True
        return [], None

    fresh = [i for i in ids if i not in state.seen][:MAX_NEW_PER_CYCLE]
    found: list[Item] = []
    for n, item_id in enumerate(fresh):
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
        if max_price is None or item.price is None or item.price <= max_price:
            found.append(item)
    return found, None


def _format(item: Item) -> str:
    price = f"{item.price:g} {item.currency}" if item.price is not None else "цена не определена"
    return f"{datetime.now():%H:%M} | {price} | {item.title} | {item.url}"


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
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Следит за поиском и уведомляет о новых объявлениях. Возвращает код выхода (0 — штатно, 3 — остановлено)."""
    query = query.strip()
    if not query:
        raise VintedError("Укажите, что искать, например: «kurtka zara».")
    state_dir = state_dir or Path(os.environ.get("VINTED_MONITOR_DIR", "monitor"))
    slug = hashlib.sha1(query.lower().encode("utf-8")).hexdigest()[:10]
    state = _State(state_dir / f"{slug}.json")
    log_path = state_dir / f"{slug}.log"
    base = settings.base_url.rstrip("/")
    base_wait = max(MIN_INTERVAL, int(interval))
    if interval < MIN_INTERVAL:
        notify(f"Пауза {interval} с слишком короткая, ставлю {MIN_INTERVAL} с.")
    wait = base_wait
    blocks = 0
    cycles = 0

    notify(f"Слежу за запросом «{query}»" + (f", цена до {max_price:g}" if max_price is not None else "")
           + ". Остановить: Ctrl+C.")
    if not state.baseline:
        notify("Первая проверка запомнит текущие объявления. Уведомлю о тех, что появятся позже.")

    session_settings = dataclasses.replace(settings, profile_dir=state_dir / "profile")
    with VintedSession(session_settings, headless=headless) as vs:
        while True:
            cycles += 1
            found, reason = _check(vs, base, query, state, max_price, sleep)
            for item in found:
                line = _format(item)
                notify(line)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            state.save()

            if reason:
                blocks += 1
                if blocks >= MAX_BLOCKS:
                    notify("Vinted блокирует проверки несколько раз подряд. Монитор остановлен, попробуйте позже.")
                    return 3
                if once:
                    notify(f"{datetime.now():%H:%M} | {reason} Попробуйте позже.")
                else:
                    wait = min(wait * 2, MAX_BACKOFF)
                    notify(f"{datetime.now():%H:%M} | {reason} Следующая проверка через {wait // 60} мин.")
            else:
                blocks = 0
                wait = base_wait
                if not found:
                    notify(f"{datetime.now():%H:%M} | новых нет")

            if once or (max_cycles is not None and cycles >= max_cycles):
                return 0
            sleep(wait)
