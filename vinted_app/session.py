from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from playwright.sync_api import APIResponse, BrowserContext, Locator, Page, Playwright, Response, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from .config import Settings

# Страница выбора способа входа/регистрации (ссылка кнопки «Регистрация | Вход» в шапке сайта).
LOGIN_PATH = "/member/signup/select_type?ref_url=%2F"
# Форма входа по e-mail и паролю: поля input#username, input#password и кнопка submit.
LOGIN_EMAIL_PATH = "/member/login/email?ref_url=%2F"
# Отвечает 200 с данными пользователя, если вы вошли, и 401/403, если нет.
CURRENT_USER_PATH = "/api/v2/users/current"
# Обновление токенов по cookie refresh_token_web (так делает и сам сайт при ответе 401).
REFRESH_PATH = "/web/api/auth/refresh"
# Сюда форма входа отправляет логин/пароль и, при двухфакторной проверке, код.
OAUTH_PATH = "/web/api/auth/oauth"

ACCESS_COOKIE = "access_token_web"
REFRESH_COOKIE = "refresh_token_web"

CODE_INPUT = 'input[data-testid="verification-code"], input#code, input[name="code"]'
CAPTCHA_FRAME = 'iframe[src*="captcha-delivery.com"]'
MAX_CODE_ATTEMPTS = 3


class VintedError(RuntimeError):
    pass


class NotLoggedInError(VintedError):
    pass


def decode_jwt_payload(token: str) -> dict | None:
    """Читает полезную нагрузку JWT без проверки подписи (только для информации)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_user_token(payload: dict | None) -> bool:
    # У анонимного посетителя Vinted тоже есть токены, но со scope "public".
    return bool(payload) and payload.get("scope") != "public"


def _json_or_none(resp: APIResponse | Response) -> Any:
    try:
        return resp.json()
    except (ValueError, PlaywrightError):
        return None


def playwright_error_text(exc: Exception) -> str:
    """Короткое описание ошибки браузера (Playwright) или любой другой для показа пользователю."""
    message = getattr(exc, "message", None) or str(exc)
    first_line = message.splitlines()[0] if message else exc.__class__.__name__
    if isinstance(exc, PlaywrightError):
        return f"Ошибка браузера: {first_line}"
    return first_line


def _error_text(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("message", "error_description", "error", "errors"):
            if data.get(key):
                return str(data[key])
    return str(data)[:300] if data else "без описания"


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
            # Обновляем просроченный access-токен ДО загрузки страницы: если открыть сайт
            # только с refresh-токеном, Vinted может выдать вместо сессии новую анонимную.
            self._refresh_if_expired()
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

    # --- токены ---------------------------------------------------------------

    def _ctx(self) -> BrowserContext:
        if self.context is None:
            raise VintedError("Сессия не открыта: используйте `with VintedSession() as vs:`")
        return self.context

    def _cookie_payload(self, name: str) -> dict | None:
        for cookie in self._ctx().cookies(self.settings.base_url):
            if cookie["name"] == name:
                return decode_jwt_payload(cookie["value"])
        return None

    def refresh_tokens(self) -> dict:
        """Получает новые токены по cookie refresh_token_web. Новые cookies сохраняются в профиль."""
        resp = self._ctx().request.post(
            self.settings.base_url + REFRESH_PATH, headers={"accept": "application/json"}
        )
        data = _json_or_none(resp)
        if resp.status == 200 and isinstance(data, dict):
            return data
        # 400 — токен уже использован или отозван, 401 — недействителен, 404 — токена нет.
        if resp.status in (400, 401, 404):
            raise NotLoggedInError(f"Vinted не принял refresh-токен (HTTP {resp.status}).")
        raise VintedError(f"Не удалось обновить токены: HTTP {resp.status}: {_error_text(data)}")

    def _refresh_if_expired(self) -> None:
        refresh = self._cookie_payload(REFRESH_COOKIE)
        if not _is_user_token(refresh):
            return
        access = self._cookie_payload(ACCESS_COOKIE)
        if access is None or access.get("exp", 0) < time.time() + 60:
            try:
                self.refresh_tokens()
            except NotLoggedInError:
                pass  # сессия больше не действует — current_user() сообщит, что вход не выполнен

    def session_expires_at(self) -> datetime | None:
        """До какого момента действует сохранённая сессия (срок refresh-токена)."""
        payload = self._cookie_payload(REFRESH_COOKIE) or {}
        if "exp" in payload:
            return datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        return None

    # --- API ------------------------------------------------------------------

    def api_get(self, path: str) -> tuple[int, Any]:
        """GET к API Vinted с cookies текущей сессии браузера. Возвращает (HTTP-статус, JSON)."""
        resp = self._ctx().request.get(
            self.settings.base_url + path, headers={"accept": "application/json"}
        )
        data = _json_or_none(resp)
        if data is None:
            raise VintedError(
                f"{path}: HTTP {resp.status}, ответ не JSON — вероятно, антибот-проверка сайта. "
                "Запустите с окном браузера (--headed или SHOW_BROWSER = True) и пройдите проверку вручную."
            )
        return resp.status, data

    def current_user(self, *, _retry: bool = True) -> dict | None:
        """Данные текущего пользователя или None, если вход не выполнен."""
        status, data = self.api_get(CURRENT_USER_PATH)
        if status == 200 and isinstance(data, dict):
            return data.get("user", data)
        if status == 401 and _retry and _is_user_token(self._cookie_payload(REFRESH_COOKIE)):
            # access-токен истёк — обновляем и пробуем ещё раз, как это делает сам сайт
            try:
                self.refresh_tokens()
            except NotLoggedInError:
                return None
            return self.current_user(_retry=False)
        if status in (401, 403):
            if "captcha-delivery" in json.dumps(data):
                raise VintedError("Запрос заблокирован антиботом (DataDome). Запустите с окном браузера (--headed или SHOW_BROWSER = True).")
            return None
        raise VintedError(f"Неожиданный ответ {CURRENT_USER_PATH}: HTTP {status}: {_error_text(data)}")

    def is_logged_in(self) -> bool:
        return self.current_user() is not None

    def require_user(self) -> dict:
        user = self.current_user()
        if user is None:
            raise NotLoggedInError("Вы не вошли в Vinted. Выполните: python -m vinted_app login")
        return user

    # --- вход: refresh-токен --------------------------------------------------

    @staticmethod
    def _clean_token(refresh_token: str) -> str:
        token = refresh_token.strip().strip("\"'")
        if token.startswith(REFRESH_COOKIE + "="):
            token = token[len(REFRESH_COOKIE) + 1 :]
        return token

    def ensure_login(self, refresh_token: str | None = None) -> dict:
        """Сохранённая сессия, а если её нет (или токен от другого аккаунта) — вход по токену.

        Скопированный токен Vinted принимает только один раз, поэтому, если этот же аккаунт уже
        вошёл, токен не используется повторно.
        """
        token = self._clean_token(refresh_token or "")
        user = self.current_user()
        if user is not None:
            payload = decode_jwt_payload(token) if token else None
            if payload is None or str(payload.get("sub")) == str(user.get("id")):
                return user
        if not token:
            raise NotLoggedInError("Вход не выполнен: нужен refresh-токен (cookie refresh_token_web).")
        return self.login_with_token(token)

    def login_with_token(self, refresh_token: str) -> dict:
        """Вход по значению cookie refresh_token_web, скопированному из браузера, где вы уже вошли."""
        token = self._clean_token(refresh_token)
        payload = decode_jwt_payload(token)
        if payload is None:
            raise VintedError("Это не похоже на токен Vinted (ожидается строка вида eyJ...xxx.yyy).")
        if payload.get("purpose") == "access":
            raise VintedError(f"Это access-токен. Нужно значение cookie {REFRESH_COOKIE}.")
        if not _is_user_token(payload):
            raise VintedError(
                "Это токен анонимного посетителя (scope=public). Войдите на сайт в браузере "
                f"и скопируйте {REFRESH_COOKIE} уже после входа."
            )

        ctx = self._ctx()
        # Запоминаем текущую сессию, чтобы вернуть её, если новый токен не подойдёт.
        backup = [c for c in ctx.cookies() if c["name"] in (ACCESS_COOKIE, REFRESH_COOKIE)]
        ctx.clear_cookies(name=ACCESS_COOKIE)
        ctx.clear_cookies(name=REFRESH_COOKIE)
        host = urlparse(self.settings.base_url).hostname
        ctx.add_cookies([{
            "name": REFRESH_COOKIE, "value": token, "domain": f".{host}", "path": "/",
            "secure": True, "httpOnly": True, "sameSite": "Lax",
        }])
        try:
            self.refresh_tokens()
        except NotLoggedInError:
            ctx.clear_cookies(name=REFRESH_COOKIE)
            if backup:
                ctx.add_cookies(backup)
            raise NotLoggedInError(
                "Vinted не принял токен: он истёк, отозван или уже был использован (скопированный "
                "токен принимается один раз — скопируйте свежий), либо он с сайта другой страны "
                f"(страна в токене: {payload.get('cc', '?')}, сайт: {self.settings.base_url})."
            ) from None
        return self.require_user()

    # --- вход: e-mail и пароль ------------------------------------------------

    def login_with_password(
        self,
        email: str,
        password: str,
        ask_code: Callable[[str], str],
        timeout: float = 600,
    ) -> dict:
        """Вход по e-mail и паролю: скрипт заполняет настоящую форму входа на сайте.

        Если Vinted попросит код подтверждения (письмо/SMS), вызывается ask_code(подсказка),
        которая должна вернуть код. Капчу в режиме без окна пройти нельзя — нужен headless=False.
        """
        user = self.current_user()
        if user is not None:
            return user

        page = self.page
        auth_responses: list[Response] = []

        def on_response(resp: Response) -> None:
            if OAUTH_PATH in resp.url:
                auth_responses.append(resp)

        page.on("response", on_response)
        try:
            page.goto(self.settings.base_url + LOGIN_EMAIL_PATH, wait_until="domcontentloaded")
            try:
                # С окном даём время пройти проверку «вы человек?», если она появится.
                page.wait_for_selector("input#username", timeout=30_000 if self.headless else 180_000)
            except PlaywrightError:
                raise VintedError(
                    "Форма входа не появилась — вероятно, сайт показал проверку «вы человек?». "
                    "Запустите с окном браузера (--headed или SHOW_BROWSER = True) и пройдите её."
                ) from None
            page.fill("input#username", email)
            page.fill("input#password", password)
            page.locator("form:has(input#password) button[type=submit]").first.click()
            return self._wait_after_credentials(auth_responses, ask_code, timeout)
        finally:
            page.remove_listener("response", on_response)

    def _wait_after_credentials(
        self, auth_responses: list[Response], ask_code: Callable[[str], str], timeout: float
    ) -> dict:
        page = self.page
        deadline = time.monotonic() + timeout
        code_attempts = 0
        awaiting_code_result = False
        captcha_reported = False

        while time.monotonic() < deadline:
            # 1. Ответы сервера на логин/пароль и на код.
            while auth_responses:
                resp = auth_responses.pop(0)
                data = _json_or_none(resp)
                needs_code = isinstance(data, dict) and "control_code" in data
                if resp.status < 400 or needs_code:
                    continue
                if not awaiting_code_result:
                    raise VintedError(f"Vinted отклонил вход (HTTP {resp.status}): {_error_text(data)}")
                awaiting_code_result = False  # код не подошёл — спросим ещё раз
                if code_attempts >= MAX_CODE_ATTEMPTS:
                    raise VintedError(f"Код не подошёл {code_attempts} раза: {_error_text(data)}")

            # 2. Вошли?
            try:
                user = self.current_user()
            except VintedError:
                user = None
            if user is not None:
                return user

            # 3. Капча DataDome.
            if page.locator(CAPTCHA_FRAME).count():
                if self.headless:
                    raise VintedError("Vinted показал капчу. Запустите с окном браузера (--headed или SHOW_BROWSER = True) и решите её.")
                if not captcha_reported:
                    print("Vinted показал капчу — решите её в окне браузера.")
                    captcha_reported = True

            # 4. Код подтверждения.
            code_input = page.locator(CODE_INPUT).first
            if not awaiting_code_result and code_input.count() and code_input.is_visible():
                hint = "Введите код подтверждения от Vinted (из письма или SMS): "
                if code_attempts:
                    hint = "Код не подошёл. Введите код ещё раз: "
                code = ask_code(hint).strip()
                if code:
                    self._submit_code(code_input, code)
                    code_attempts += 1
                    awaiting_code_result = True

            time.sleep(2)
        raise NotLoggedInError(f"Вход не выполнен за {int(timeout)} с.")

    @staticmethod
    def _submit_code(code_input: Locator, code: str) -> None:
        code_input.fill(code)
        for container in ("xpath=ancestor::form[1]", "xpath=ancestor::*[@role='dialog'][1]"):
            button = code_input.locator(container).locator("button[type=submit]")
            if button.count():
                button.first.click()
                return
        code_input.press("Enter")

    # --- вход: вручную в окне -------------------------------------------------

    def login_interactive(self, timeout: float = 600, poll_interval: float = 3) -> dict:
        """Открывает страницу входа и ждёт, пока пользователь войдёт сам в окне браузера."""
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
