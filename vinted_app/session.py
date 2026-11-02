from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Any

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from .config import Settings

# Страница выбора способа входа/регистрации (ссылка кнопки «Регистрация | Вход» в шапке сайта).
LOGIN_PATH = "/member/signup/select_type?ref_url=%2F"
# Отвечает 200 с данными пользователя, если вы вошли, и 401/403, если нет.
CURRENT_USER_PATH = "/api/v2/users/current"


class VintedError(RuntimeError):
    pass


class NotLoggedInError(VintedError):
    pass


def decode_jwt_payload(token: str) -> dict | None:
    """Читает полезную нагрузку JWT без проверки подписи (только для информации)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return None


class VintedSession:
    """Браузер с постоянным профилем: однажды войдя, вы остаётесь в аккаунте между запусками.

    Пример:
        with VintedSession() as vs:
            user = vs.require_user()
            print(user["login"])
    """

    def __init__(self, settings: Settings | None = None, *, headless: bool = True) -> None:
        self.settings = settings or Settings.from_env()
        self.headless = headless
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    # --- жизненный цикл -------------------------------------------------------

    def __enter__(self) -> "VintedSession":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> None:
        self.settings.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        try:
            self.context = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.profile_dir),
                headless=self.headless,
                executable_path=self.settings.chromium_path,
                viewport={"width": 1280, "height": 900},
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            # Сначала открываем сайт, как обычный браузер: Vinted выставляет/продлевает
            # cookies сессии, и дальнейшие API-запросы идут уже с ними.
            self.page.goto(self.settings.base_url + "/", wait_until="domcontentloaded")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.context is not None:
            try:
                self.context.close()
            except PlaywrightError:
                pass  # окно уже закрыто пользователем
            self.context = None
            self.page = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None

    # --- API ------------------------------------------------------------------

    def api_get(self, path: str) -> tuple[int, Any]:
        """GET к API Vinted с cookies текущей сессии браузера. Возвращает (HTTP-статус, JSON)."""
        if self.context is None:
            raise VintedError("Сессия не открыта: используйте `with VintedSession() as vs:`")
        resp = self.context.request.get(
            self.settings.base_url + path, headers={"accept": "application/json"}
        )
        try:
            data = resp.json()
        except ValueError:
            raise VintedError(
                f"{path}: HTTP {resp.status}, ответ не JSON — вероятно, антибот-проверка сайта. "
                "Откройте сайт с окном браузера (--headed) и пройдите проверку вручную."
            ) from None
        return resp.status, data

    def current_user(self) -> dict | None:
        """Данные текущего пользователя или None, если вход не выполнен."""
        status, data = self.api_get(CURRENT_USER_PATH)
        if status == 200 and isinstance(data, dict):
            return data.get("user", data)
        if status in (401, 403):
            if "captcha-delivery" in json.dumps(data):
                raise VintedError("Запрос заблокирован антиботом (DataDome). Повторите с --headed.")
            return None
        raise VintedError(f"Неожиданный ответ {CURRENT_USER_PATH}: HTTP {status}: {str(data)[:300]}")

    def is_logged_in(self) -> bool:
        return self.current_user() is not None

    def require_user(self) -> dict:
        user = self.current_user()
        if user is None:
            raise NotLoggedInError("Вы не вошли в Vinted. Выполните: python -m vinted_app login")
        return user

    def session_expires_at(self) -> datetime | None:
        """До какого момента действует сохранённая сессия (срок refresh-токена)."""
        if self.context is None:
            return None
        for cookie in self.context.cookies(self.settings.base_url):
            if cookie["name"] == "refresh_token_web":
                payload = decode_jwt_payload(cookie["value"]) or {}
                if "exp" in payload:
                    return datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        return None

    # --- вход -----------------------------------------------------------------

    def login_interactive(self, timeout: float = 600, poll_interval: float = 3) -> dict:
        """Открывает страницу входа и ждёт, пока пользователь войдёт сам в окне браузера.

        Логин/пароль, капчу и код подтверждения вводит человек — скрипт их не видит
        и не хранит. После входа cookies остаются в папке профиля.
        """
        if self.headless:
            raise VintedError("Для входа нужно окно браузера: создайте VintedSession(headless=False)")
        user = self.current_user()
        if user is not None:
            return user

        self.page.goto(self.settings.base_url + LOGIN_PATH, wait_until="domcontentloaded")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            try:
                user = self.current_user()
            except PlaywrightError as e:
                raise VintedError("Окно браузера закрыто до завершения входа.") from e
            except VintedError:
                user = None  # например, пользователь как раз проходит антибот-проверку
            if user is not None:
                return user
        raise NotLoggedInError(f"Вход не выполнен за {int(timeout)} с.")
