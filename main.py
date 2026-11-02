"""
Вход в аккаунт Vinted. Впишите свои данные ниже и запустите:  python main.py

ВНИМАНИЕ: пароль и токен дают полный доступ к аккаунту. Не коммитьте и не отправляйте
этот файл с заполненными данными (см. README: git update-index --skip-worktree main.py).
"""

# ============================ ВАШИ ДАННЫЕ ============================
VINTED_URL = "https://www.vinted.fr"  # сайт вашей страны: vinted.pl, vinted.de, vinted.lt ...
EMAIL = ""                            # почта от аккаунта Vinted
PASSWORD = ""                         # пароль
REFRESH_TOKEN = ""                    # значение cookie refresh_token_web (можно оставить пустым)
SHOW_BROWSER = False                  # True — показать окно браузера (нужно, если Vinted покажет капчу)
# =====================================================================

import os  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from vinted_app import Settings, VintedError, VintedSession  # noqa: E402


def login(vs: VintedSession) -> dict:
    """Порядок: уже сохранённая сессия → refresh-токен → почта и пароль."""
    user = vs.current_user()
    if user is not None:
        return user

    if REFRESH_TOKEN.strip():
        try:
            return vs.login_with_token(REFRESH_TOKEN)
        except VintedError as e:
            print(f"Вход по токену не удался: {e}")

    if EMAIL.strip() and PASSWORD:
        print(f"Вхожу по почте {EMAIL.strip()}...")
        return vs.login_with_password(EMAIL.strip(), PASSWORD, ask_code=input)

    raise VintedError("Не удалось войти: впишите в начале main.py REFRESH_TOKEN или EMAIL и PASSWORD.")


def main() -> int:
    settings = Settings(
        base_url=VINTED_URL.strip().rstrip("/"),
        profile_dir=Path(__file__).resolve().with_name(".vinted_profile"),  # сессия хранится рядом с main.py
        chromium_path=os.environ.get("VINTED_CHROMIUM_PATH") or None,
    )
    try:
        with VintedSession(settings, headless=not SHOW_BROWSER) as vs:
            user = login(vs)
            print(f"Вы вошли как {user.get('login', '?')} (id {user.get('id', '?')})")

            # ------------- дальше ваш код -------------
            # status, data = vs.api_get("/api/v2/users/current")
            # vs.page.goto(VINTED_URL)

    except VintedError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
