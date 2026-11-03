"""Авторизация в Vinted через настоящий браузер (Playwright) с сохранением сессии."""

from .config import Settings
from .messaging import Listing, SendResult, get_listing, parse_listing_url, send_message_to_listing
from .session import NotLoggedInError, VintedError, VintedSession

__all__ = [
    "Settings", "VintedSession", "VintedError", "NotLoggedInError",
    "Listing", "SendResult", "parse_listing_url", "get_listing", "send_message_to_listing",
]
