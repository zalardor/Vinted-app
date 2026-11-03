"""Аккаунты Vinted: список аккаунтов, вход по токену, отправка сообщения с выбранных аккаунтов.

Общая логика для терминала (`python -m vinted_app accounts ...`) и окна (`python gui.py`).
Каждый аккаунт хранит свою сессию в отдельной папке, поэтому входы не мешают друг другу.
Сообщение можно отправить с нескольких выбранных аккаунтов: каждый пишет из своей сессии, по очереди.
Refresh-токен вводится один раз — при добавлении или повторном входе. Дальше работает сохранённая
сессия в папке аккаунта. Эта папка содержит cookie с токеном, поэтому её нельзя передавать.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .config import Settings
from .messaging import Listing, SendResult, get_listing, parse_listing_url, send_message_to_listing
from .session import NotLoggedInError, VintedError, VintedSession, decode_jwt_payload, playwright_error_text

OK = "ok"
NEED_TOKEN = "need_token"
STATUS_TEXT = {OK: "вход сохранён", NEED_TOKEN: "нужен токен"}


@dataclass
class Account:
    name: str
    base_url: str
    login: str | None = None
    user_id: str | None = None
    status: str = NEED_TOKEN


def normalize_site(site: str) -> str:
    site = (site or "").strip().rstrip("/")
    if not site:
        raise VintedError("Укажите сайт Vinted, например https://www.vinted.pl")
    if not site.startswith(("http://", "https://")):
        site = "https://" + site
    return site


def _site_key(url: str) -> str:
    """vinted.pl и www.vinted.pl — один и тот же сайт."""
    host = (urlparse(normalize_site(url)).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class AccountStore:
    """Список аккаунтов (accounts/accounts.json) и папки их сессий (accounts/profiles/...)."""

    def __init__(self, root: str | Path = "accounts") -> None:
        self.root = Path(root)
        self._file = self.root / "accounts.json"
        self._lock = threading.RLock()

    def _read(self) -> list[Account]:
        if not self._file.exists():
            return []
        raw = json.loads(self._file.read_text(encoding="utf-8") or "[]")
        return [Account(**item) for item in raw]

    def _write(self, accounts: list[Account]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_name(self._file.name + ".tmp")
        tmp.write_text(json.dumps([asdict(a) for a in accounts], ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._file)

    def accounts(self) -> list[Account]:
        with self._lock:
            return self._read()

    def get(self, name: str) -> Account | None:
        return next((a for a in self.accounts() if a.name == name), None)

    def profile_dir(self, name: str) -> Path:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]
        return self.root / "profiles" / digest

    def add(self, account: Account) -> None:
        with self._lock:
            accounts = self._read()
            if any(a.name == account.name for a in accounts):
                raise VintedError(f"Аккаунт «{account.name}» уже добавлен.")
            accounts.append(account)
            self._write(accounts)

    def update(self, account: Account) -> None:
        with self._lock:
            accounts = self._read()
            for i, existing in enumerate(accounts):
                if existing.name == account.name:
                    accounts[i] = account
                    break
            else:
                raise VintedError(f"Аккаунт «{account.name}» не найден.")
            self._write(accounts)

    def remove(self, name: str) -> None:
        with self._lock:
            accounts = self._read()
            if not any(a.name == name for a in accounts):
                raise VintedError(f"Аккаунт «{name}» не найден.")
            self._write([a for a in accounts if a.name != name])
        shutil.rmtree(self.profile_dir(name), ignore_errors=True)


def default_store() -> AccountStore:
    return AccountStore(os.environ.get("VINTED_ACCOUNTS_DIR", "accounts"))


def _settings(store: AccountStore, account: Account) -> Settings:
    return dataclasses.replace(
        Settings.from_env(),
        base_url=normalize_site(account.base_url),
        profile_dir=store.profile_dir(account.name),
    )


def _require(store: AccountStore, name: str) -> Account:
    account = store.get(name)
    if account is None:
        raise VintedError(f"Аккаунт «{name}» не найден.")
    return account


def _login(store: AccountStore, account: Account, token: str | None, headless: bool) -> dict:
    """Сохранённая сессия аккаунта, а если её нет или передан токен — вход по токену."""
    with VintedSession(_settings(store, account), headless=headless) as vs:
        return vs.ensure_login(token)


def _save_login(store: AccountStore, account: Account, user: dict) -> Account:
    updated = dataclasses.replace(account, login=user.get("login"), user_id=str(user.get("id")), status=OK)
    store.update(updated)
    return updated


def _set_status(store: AccountStore, account: Account, status: str) -> None:
    store.update(dataclasses.replace(account, status=status))


def add_account(
    store: AccountStore, name: str, site: str, token: str, *, consent: bool, headless: bool = True
) -> Account:
    """Добавляет аккаунт: вход по токену один раз, дальше вход сохраняется в его папке."""
    name = (name or "").strip()
    token = (token or "").strip()
    if not name:
        raise VintedError("Введите имя аккаунта, например «Аня».")
    if not consent:
        raise VintedError("Подтвердите, что аккаунт ваш или владелец дал на это согласие.")
    if not token:
        raise VintedError("Введите refresh-токен.")
    if store.get(name) is not None:
        raise VintedError(f"Аккаунт «{name}» уже добавлен.")
    site = normalize_site(site)
    try:
        user = _login(store, Account(name=name, base_url=site), token, headless)
    except VintedError:
        shutil.rmtree(store.profile_dir(name), ignore_errors=True)
        raise
    account = Account(name=name, base_url=site, login=user.get("login"), user_id=str(user.get("id")), status=OK)
    duplicate = next((a for a in store.accounts() if a.user_id == account.user_id), None)
    if duplicate is not None:
        shutil.rmtree(store.profile_dir(name), ignore_errors=True)
        raise VintedError(f"Этот аккаунт Vinted уже добавлен как «{duplicate.name}».")
    store.add(account)
    return account


def login_account(store: AccountStore, name: str, token: str | None = None, *, headless: bool = True) -> Account:
    """Новый вход для уже добавленного аккаунта, например когда сессия истекла."""
    account = _require(store, name)
    token = (token or "").strip() or None
    if token and account.user_id:
        payload = decode_jwt_payload(VintedSession._clean_token(token))
        if payload is not None and str(payload.get("sub")) != account.user_id:
            raise VintedError(f"Этот токен не от аккаунта «{name}». Нужен токен именно этого аккаунта.")
    try:
        user = _login(store, account, token, headless)
    except NotLoggedInError:
        _set_status(store, account, NEED_TOKEN)
        raise
    return _save_login(store, account, user)


def check_account(store: AccountStore, name: str, *, headless: bool = True) -> Account:
    """Проверяет сохранённый вход без токена. Если вход истёк, статус станет «нужен токен»."""
    account = _require(store, name)
    try:
        user = _login(store, account, None, headless)
    except NotLoggedInError:
        _set_status(store, account, NEED_TOKEN)
        return dataclasses.replace(account, status=NEED_TOKEN)
    return _save_login(store, account, user)


@dataclass
class SendOutcome:
    """Итог отправки с одного аккаунта: result — None, если отменено; error — текст ошибки, если не вышло."""

    name: str
    result: SendResult | None = None
    error: str | None = None


def _send_one(
    store: AccountStore,
    account: Account,
    item_id: str,
    text: str,
    *,
    confirm: Callable[[Listing, Account], bool],
    log: Callable[[str], None],
    headless: bool,
) -> SendResult | None:
    """Пишет продавцу из сессии одного аккаунта. Возвращает None, если пользователь отменил."""
    result: SendResult | None = None
    with VintedSession(_settings(store, account), headless=headless) as vs:
        try:
            user = vs.ensure_login(None)
        except NotLoggedInError:
            _set_status(store, account, NEED_TOKEN)
            raise VintedError(
                f"Вход в «{account.name}» истёк. Нажмите «Ввести токен» и вставьте токен этого аккаунта."
            ) from None
        if account.user_id and str(user.get("id")) != account.user_id:
            _set_status(store, account, NEED_TOKEN)
            raise VintedError(f"В сохранённом входе «{account.name}» другой пользователь. Введите токен заново.")

        log(f"Аккаунт: «{account.name}» ({user.get('login', '?')}).")
        listing = get_listing(vs, item_id)
        log(f"Объявление: {listing.title}")
        log(f"Продавец: {listing.seller_login or '?'} (id {listing.seller_id})")
        if not confirm(listing, account):
            log("Отменено — ничего не отправлено.")
            return None
        result = send_message_to_listing(vs, listing, text)

    _save_login(store, account, user)
    return result


def send_from_accounts(
    store: AccountStore,
    names: list[str],
    listing_url: str,
    text: str,
    *,
    confirm: Callable[[Listing, Account], bool],
    log: Callable[[str], None] = print,
    headless: bool = True,
) -> list[SendOutcome]:
    """Пишет продавцу объявления с каждого выбранного аккаунта по очереди, каждый из своей сессии.

    Ошибка одного аккаунта не останавливает остальные: она попадает в его SendOutcome.
    Ошибки, общие для всех (пустой текст, неверная ссылка, аккаунты не выбраны), выбрасываются сразу.
    """
    text = (text or "").strip()
    if not text:
        raise VintedError("Сообщение пустое.")
    if not names:
        raise VintedError("Выберите хотя бы один аккаунт.")
    base, item_id = parse_listing_url(listing_url)

    outcomes: list[SendOutcome] = []
    for name in dict.fromkeys(names):  # один аккаунт, выбранный дважды, пишет только один раз
        outcome = SendOutcome(name=name)
        try:
            account = _require(store, name)
            if _site_key(base) != _site_key(account.base_url):
                raise VintedError(
                    f"Аккаунт «{name}» зарегистрирован на {normalize_site(account.base_url)}, "
                    f"а объявление на {base}. Выберите аккаунт этой страны."
                )
            outcome.result = _send_one(store, account, item_id, text, confirm=confirm, log=log, headless=headless)
        except VintedError as e:
            outcome.error = str(e)
        except Exception as e:  # ошибки браузера и прочее: не должны остановить остальные аккаунты
            outcome.error = playwright_error_text(e)
        outcomes.append(outcome)
    return outcomes


def send_from_account(
    store: AccountStore,
    name: str,
    listing_url: str,
    text: str,
    *,
    confirm: Callable[[Listing, Account], bool],
    log: Callable[[str], None] = print,
    headless: bool = True,
) -> SendResult | None:
    """Отправка с одного аккаунта: ошибка сразу выбрасывается. Оставлено для терминальной команды `send`."""
    outcome = send_from_accounts(store, [name], listing_url, text, confirm=confirm, log=log, headless=headless)[0]
    if outcome.error is not None:
        raise VintedError(outcome.error)
    return outcome.result
