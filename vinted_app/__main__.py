from __future__ import annotations

import argparse
import dataclasses
import sys

from .config import Settings
from .session import VintedError, VintedSession


def _describe(user: dict) -> str:
    return f"{user.get('login', '?')} (id {user.get('id', '?')})"


def cmd_login(settings: Settings, args: argparse.Namespace) -> int:
    print(f"Открываю {settings.base_url} в окне браузера.")
    print("Войдите в свой аккаунт (если попросят — пройдите капчу и введите код подтверждения).")
    print("Скрипт сам заметит успешный вход. Не закрывайте окно до сообщения в консоли.\n")
    with VintedSession(settings, headless=False) as vs:
        user = vs.login_interactive(timeout=args.timeout)
        print(f"Готово, вы вошли как {_describe(user)}.")
        print(f"Сессия сохранена в папке {settings.profile_dir}/")
        expires = vs.session_expires_at()
        if expires:
            print(f"Действует примерно до {expires:%Y-%m-%d} (UTC).")
    return 0


def cmd_whoami(settings: Settings, args: argparse.Namespace) -> int:
    with VintedSession(settings, headless=not args.headed) as vs:
        user = vs.current_user()
        if user is None:
            print("Не авторизован. Выполните: python -m vinted_app login")
            return 1
        print(f"Авторизован как {_describe(user)}")
        expires = vs.session_expires_at()
        if expires:
            print(f"Сессия действует примерно до {expires:%Y-%m-%d} (UTC).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vinted_app", description="Авторизация в Vinted")
    parser.add_argument(
        "--base-url",
        help="сайт Vinted вашей страны, например https://www.vinted.pl "
        "(по умолчанию — переменная VINTED_BASE_URL или https://www.vinted.fr)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="открыть браузер и войти в аккаунт")
    p_login.add_argument("--timeout", type=int, default=600, help="сколько секунд ждать входа (600)")
    p_login.set_defaults(func=cmd_login)

    p_who = sub.add_parser("whoami", help="проверить, сохранён ли вход")
    p_who.add_argument("--headed", action="store_true", help="показать окно браузера")
    p_who.set_defaults(func=cmd_whoami)

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if args.base_url:
        settings = dataclasses.replace(settings, base_url=args.base_url.rstrip("/"))

    try:
        return args.func(settings, args)
    except VintedError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
