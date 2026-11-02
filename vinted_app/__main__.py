from __future__ import annotations

import argparse
import dataclasses
import getpass
import os
import sys

from playwright.sync_api import Error as PlaywrightError

from .config import Settings
from .session import VintedError, VintedSession


def _describe(user: dict) -> str:
    return f"{user.get('login', '?')} (id {user.get('id', '?')})"


def _print_expiry(vs: VintedSession, prefix: str) -> None:
    expires = vs.session_expires_at()
    if expires:
        print(f"{prefix} примерно до {expires:%Y-%m-%d} (UTC).")


def _secret(env_name: str, prompt: str) -> str:
    try:
        value = os.environ.get(env_name) or getpass.getpass(prompt)
    except EOFError:
        value = ""
    if not value.strip():
        raise VintedError(f"Значение не задано: введите его или задайте переменную {env_name}.")
    return value


def cmd_login(settings: Settings, args: argparse.Namespace) -> int:
    if args.token:
        token = _secret("VINTED_REFRESH_TOKEN", "Значение cookie refresh_token_web (ввод скрыт): ")
        with VintedSession(settings, headless=not args.headed) as vs:
            user = vs.login_with_token(token)
            print(f"Готово, вы вошли как {_describe(user)}.")
            _print_expiry(vs, "Сессия действует")
        return 0

    if args.email:
        password = _secret("VINTED_PASSWORD", f"Пароль Vinted для {args.email} (ввод скрыт): ")
        print(f"Вхожу на {settings.base_url} как {args.email}...")
        with VintedSession(settings, headless=not args.headed) as vs:
            user = vs.login_with_password(args.email, password, ask_code=input, timeout=args.timeout)
            print(f"Готово, вы вошли как {_describe(user)}.")
            _print_expiry(vs, "Сессия действует")
        return 0

    print(f"Открываю {settings.base_url} в окне браузера.")
    print("Войдите в свой аккаунт (если попросят — пройдите капчу и введите код подтверждения).")
    print("Скрипт сам заметит успешный вход. Не закрывайте окно до сообщения в консоли.\n")
    with VintedSession(settings, headless=False) as vs:
        user = vs.login_interactive(timeout=args.timeout)
        print(f"Готово, вы вошли как {_describe(user)}.")
        print(f"Сессия сохранена в папке {settings.profile_dir}/")
        _print_expiry(vs, "Действует")
    return 0


def cmd_whoami(settings: Settings, args: argparse.Namespace) -> int:
    with VintedSession(settings, headless=not args.headed) as vs:
        user = vs.current_user()
        if user is None:
            print("Не авторизован. Выполните: python -m vinted_app login")
            return 1
        print(f"Авторизован как {_describe(user)}")
        _print_expiry(vs, "Сессия действует")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vinted_app", description="Авторизация в Vinted")
    parser.add_argument(
        "--base-url",
        help="сайт Vinted вашей страны, например https://www.vinted.pl "
        "(по умолчанию — переменная VINTED_BASE_URL или https://www.vinted.fr)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser(
        "login",
        help="войти в аккаунт",
        description="Без параметров — вход вручную в окне браузера.",
    )
    mode = p_login.add_mutually_exclusive_group()
    mode.add_argument(
        "--email",
        metavar="EMAIL",
        help="войти по e-mail и паролю; пароль берётся из VINTED_PASSWORD или спрашивается (ввод скрыт)",
    )
    mode.add_argument(
        "--token",
        action="store_true",
        help="войти по cookie refresh_token_web; значение берётся из VINTED_REFRESH_TOKEN "
        "или спрашивается (ввод скрыт)",
    )
    p_login.add_argument("--headed", action="store_true", help="показать окно браузера (для --email/--token)")
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
    except PlaywrightError as e:
        print(f"Ошибка браузера: {e.message.splitlines()[0]}", file=sys.stderr)
        return 2
    except EOFError:
        print("Ошибка: Vinted просит код подтверждения, но ввести его некуда (нет консоли).", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
