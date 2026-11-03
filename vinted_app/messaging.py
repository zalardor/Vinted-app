"""Сообщения Vinted: продавец по ссылке на объявление, чат по объявлению, отправка сообщения.

Используются те же запросы, что и у сайта (найдены в его JS):
  POST {api}/messaging/main/inquiries                 — открыть (или получить уже открытый) чат по объявлению
  GET  {api}/messaging/main/conversations/{id}        — прочитать чат
  POST {api}/messaging/main/conversations/{id}/replies — отправить сообщение
где {api} — https://api.vinted.<страна> (см. derive_api_url).
"""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .session import VintedError, VintedSession, _error_text, _json_or_none

# Постоянное значение заголовка X-CSRF-Token из JS сайта.
CSRF_TOKEN = "75f6c9fa-dc8e-4e52-a000-e09dd4084b3e"

_ITEM_PATH = re.compile(r"/items/(\d+)")


@dataclass(frozen=True)
class Listing:
    id: str
    title: str
    seller_id: str
    seller_login: str | None


@dataclass(frozen=True)
class SendResult:
    listing: Listing
    conversation_id: str
    verified: bool  # сообщение найдено в чате после отправки


def parse_listing_url(url: str) -> tuple[str, str]:
    """'https://www.vinted.pl/items/123-name?x=1' -> ('https://www.vinted.pl', '123')."""
    url = url.strip()
    parsed = urlparse(url if "://" in url else "https://" + url)
    host = (parsed.hostname or "").lower()
    match = _ITEM_PATH.search(parsed.path)
    if "vinted." not in host or not match:
        raise VintedError("Это не ссылка на объявление Vinted (нужна ссылка вида https://www.vinted.pl/items/123...).")
    if not host.startswith("www."):
        host = "www." + host
    return f"https://{host}", match.group(1)


def derive_api_url(base_url: str) -> str:
    """Адрес API так же, как его вычисляет сайт: https://www.vinted.pl -> https://api.vinted.pl."""
    host = urlparse(base_url).hostname or ""
    domain = host[4:] if host.startswith("www.") else host
    if domain == "vinted.com":  # для США сайт добавляет «www»
        return "https://api.www.vinted.com"
    return f"https://api.{domain}"


def _api_url(vs: VintedSession) -> str:
    return vs.settings.api_url or derive_api_url(vs.settings.base_url)


def _headers(vs: VintedSession) -> dict[str, str]:
    try:
        locale = vs.page.evaluate(
            "() => document.querySelector('meta[name=\"iso-locale\"]')?.content || document.documentElement.lang || ''"
        )
    except Exception:
        locale = ""
    return {
        "accept": "application/json, text/plain, */*",
        "platform": "web",
        "x-next-app": "marketplace-web",
        "x-csrf-token": CSRF_TOKEN,
        "locale": locale or "en",
    }


def _api(vs: VintedSession, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
    headers = _headers(vs)
    if body is not None:
        headers["content-type"] = "application/json"
    for attempt in (1, 2):
        resp = vs.context.request.fetch(
            _api_url(vs) + path,
            method=method,
            headers=headers,
            data=json.dumps(body) if body is not None else None,
        )
        if resp.status == 401 and attempt == 1:
            vs.refresh_tokens()  # access-токен истёк — обновляем и повторяем, как сайт
            continue
        data = _json_or_none(resp)
        if data is None:
            raise VintedError(
                f"{path}: HTTP {resp.status}, ответ не JSON — вероятно, антибот-проверка. Попробуйте ещё раз позже."
            )
        return resp.status, data
    raise AssertionError("unreachable")


def get_listing(vs: VintedSession, item_id: str) -> Listing:
    """Название объявления и продавец — со страницы объявления (как её видит браузер)."""
    url = f"{vs.settings.base_url}/items/{item_id}"
    page_html = ""
    for attempt in range(3):
        resp = vs.context.request.get(url, headers={"accept": "text/html"})
        if resp.status == 404:
            raise VintedError("Объявление не найдено: оно удалено или ссылка неверная.")
        page_html = resp.text()
        if "seller_id" in page_html:
            break
        time.sleep(2)  # бывает страница антибот-проверки — пробуем ещё раз
    else:
        raise VintedError("Не удалось загрузить страницу объявления (возможно, антибот-проверка). Попробуйте позже.")

    q = r'\\?"'  # в данных страницы кавычки бывают экранированы: \"seller_id\":\"123\"
    match = re.search(rf'{q}item_id{q}:{q}?{item_id}{q}?,{q}seller_id{q}:{q}?(\d+)', page_html) or re.search(
        rf'{q}seller_id{q}:{q}?(\d+)', page_html
    )
    if not match:
        raise VintedError("Не удалось определить продавца на странице объявления.")
    title = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', page_html)
    login = re.search(r'data-testid="profile-username"[^>]*>([^<]+)<', page_html)
    return Listing(
        id=str(item_id),
        title=html.unescape(title.group(1)).removesuffix(" | Vinted").strip() if title else f"объявление {item_id}",
        seller_id=match.group(1),
        seller_login=html.unescape(login.group(1)).strip() if login else None,
    )


def open_conversation(vs: VintedSession, listing: Listing) -> str:
    """Чат с продавцом по объявлению: сайт делает этот же запрос при нажатии «Написать продавцу»."""
    status, data = _api(
        vs, "POST", "/messaging/main/inquiries", {"item_ids": [listing.id], "receiver_id": listing.seller_id}
    )
    if status not in (200, 201) or not isinstance(data, dict) or not data.get("conversation_id"):
        raise VintedError(f"Не удалось открыть чат с продавцом (HTTP {status}): {_error_text(data)}")
    return str(data["conversation_id"])


def get_conversation(vs: VintedSession, conversation_id: str) -> dict:
    status, data = _api(vs, "GET", f"/messaging/main/conversations/{conversation_id}")
    if status != 200 or not isinstance(data, dict):
        raise VintedError(f"Не удалось прочитать чат (HTTP {status}): {_error_text(data)}")
    return data


def send_reply(vs: VintedSession, conversation_id: str, text: str) -> dict:
    status, data = _api(
        vs,
        "POST",
        f"/messaging/main/conversations/{conversation_id}/replies",
        {"content": text, "is_personal_data_sharing_check_skipped": False},
    )
    if status not in (200, 201):
        raise VintedError(f"Vinted не принял сообщение (HTTP {status}): {_error_text(data)}")
    return data if isinstance(data, dict) else {}


def _has_message(conversation: dict, text: str, user_id: Any) -> bool:
    for message in (conversation.get("messages") or [])[-5:]:
        entity = (message.get("data") or {}).get("entity") or {}
        if entity.get("body") == text and str(entity.get("user_id")) == str(user_id):
            return True
    return False


def send_message_to_listing(vs: VintedSession, listing: Listing, text: str) -> SendResult:
    """Открывает чат с продавцом объявления, проверяет собеседника и отправляет сообщение."""
    text = text.strip()
    if not text:
        raise VintedError("Сообщение пустое.")
    me = vs.require_user()
    if str(listing.seller_id) == str(me.get("id")):
        raise VintedError("Это ваше собственное объявление — написать самому себе нельзя.")

    conversation_id = open_conversation(vs, listing)
    conversation = get_conversation(vs, conversation_id)
    dump = json.dumps(conversation, ensure_ascii=False)
    if listing.seller_id not in dump and (not listing.seller_login or listing.seller_login not in dump):
        raise VintedError("Vinted вернул чат не с этим продавцом — сообщение НЕ отправлено.")
    if conversation.get("allow_reply") is False:
        raise VintedError("В этом чате Vinted не разрешает писать — сообщение НЕ отправлено.")

    send_reply(vs, conversation_id, text)
    verified = _has_message(get_conversation(vs, conversation_id), text, me.get("id"))
    return SendResult(listing=listing, conversation_id=conversation_id, verified=verified)
