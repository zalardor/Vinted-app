from __future__ import annotations

import argparse
import dataclasses
import getpass
import os
import sys

from playwright.sync_api import Error as PlaywrightError

from . import service
from .config import Settings
from .messaging import get_listing, parse_listing_url, send_message_to_listing
from .service import OK, STATUS_TEXT, add_account, check_account, login_account, send_from_account
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


def cmd_send(settings: Settings, args: argparse.Namespace) -> int:
    if args.account:
        # С выбранного аккаунта: его сохранённый вход и его сайт.
        def confirm(listing, account) -> bool:
            if args.yes:
                return True
            answer = input(
                f'С аккаунта «{account.name}» отправить «{args.message}» продавцу '
                f'{listing.seller_login or "?"}? [y/N] '
            )
            return answer.strip().lower() in ("y", "yes", "д", "да")

        result = send_from_account(
            service.default_store(), args.account, args.url, args.message,
            confirm=confirm, log=print, headless=not args.headed,
        )
        if result is None:
            return 1
        if result.verified:
            print(f"✓ Сообщение отправлено продавцу {result.listing.seller_login or result.listing.seller_id}.")
        else:
            print("Сообщение отправлено, но подтвердить его в чате не удалось.")
        return 0

    # Без --account: один общий вход (папка .vinted_profile), как раньше.
    base_url, item_id = parse_listing_url(args.url)
    if not args.base_url and not os.environ.get("VINTED_BASE_URL"):
        settings = dataclasses.replace(settings, base_url=base_url)  # сайт берём из ссылки
    token = os.environ.get("VINTED_REFRESH_TOKEN") or args.token

    with VintedSession(settings, headless=not args.headed) as vs:
        user = vs.ensure_login(token)
        print(f"Вы вошли как {_describe(user)}.")
        listing = get_listing(vs, item_id)
        print(f"Объявление: {listing.title}")
        print(f"Продавец:   {listing.seller_login or '?'} (id {listing.seller_id})")
        if not args.yes:
            answer = input(f'Отправить «{args.message}» этому продавцу? [y/N] ').strip().lower()
            if answer not in ("y", "yes", "д", "да"):
                print("Отменено — ничего не отправлено.")
                return 1
        result = send_message_to_listing(vs, listing, args.message)
        if result.verified:
            print(f"✓ Сообщение отправлено продавцу {listing.seller_login or listing.seller_id}.")
        else:
            print("Сообщение отправлено, но подтвердить его в чате не удалось.")
    return 0


def cmd_accounts(settings: Settings, args: argparse.Namespace) -> int:
    store = service.default_store()

    if args.acc_cmd == "list":
        accounts = store.accounts()
        if not accounts:
            print("Аккаунтов пока нет. Добавьте, например:")
            print("  python -m vinted_app accounts add Я --site https://www.vinted.pl --consent")
            return 0
        for a in accounts:
            print(f"{a.name:<16} {(a.login or '—'):<22} {a.base_url:<28} {STATUS_TEXT[a.status]}")
        return 0

    if args.acc_cmd == "add":
        if not args.consent:
            raise VintedError("Добавьте --consent: подтвердите, что аккаунт ваш или владелец дал согласие.")
        if not args.site:
            raise VintedError("Укажите сайт аккаунта: --site https://www.vinted.pl")
        token = _secret("VINTED_REFRESH_TOKEN", f"Refresh-токен для «{args.name}» (ввод скрыт): ")
        account = add_account(store, args.name, args.site, token, consent=True, headless=not args.headed)
        print(f"Аккаунт «{account.name}» добавлен: {account.login} (id {account.user_id}).")
        return 0

    if args.acc_cmd == "login":
        account = check_account(store, args.name, headless=not args.headed)
        if account.status == OK:
            print(f"«{account.name}»: вход сохранён ({account.login}). Токен не нужен.")
            return 0
        token = _secret("VINTED_REFRESH_TOKEN", f"Новый refresh-токен для «{args.name}» (ввод скрыт): ")
        account = login_account(store, args.name, token, headless=not args.headed)
        print(f"«{account.name}»: вход выполнен ({account.login}).")
        return 0

    if args.acc_cmd == "remove":
        store.remove(args.name)
        print(f"Аккаунт «{args.name}» удалён вместе с сохранённым входом.")
        return 0

    return 2


def cmd_gui(settings: Settings, args: argparse.Namespace) -> int:
    from .gui import main as gui_main

    return gui_main()


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

    p_send = sub.add_parser("send", help="написать продавцу объявления")
    p_send.add_argument("url", help="ссылка на объявление, например https://www.vinted.pl/items/123-...")
    p_send.add_argument("message", help="текст сообщения")
    p_send.add_argument("--account", metavar="NAME", help="с какого аккаунта отправить (см. accounts list)")
    p_send.add_argument("--token", help="refresh-токен без --account (иначе VINTED_REFRESH_TOKEN или сохранённый вход)")
    p_send.add_argument("--headed", action="store_true", help="показать окно браузера")
    p_send.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    p_send.set_defaults(func=cmd_send)

    p_acc = sub.add_parser("accounts", help="несколько аккаунтов: список, добавление, вход, удаление")
    acc_sub = p_acc.add_subparsers(dest="acc_cmd", required=True)
    acc_sub.add_parser("list", help="показать аккаунты и их статус")
    p_add = acc_sub.add_parser("add", help="добавить аккаунт по refresh-токену")
    p_add.add_argument("name", help="имя аккаунта в приложении, например «Аня»")
    p_add.add_argument("--site", help="сайт аккаунта, например https://www.vinted.pl")
    p_add.add_argument("--consent", action="store_true", help="подтверждаю: аккаунт мой или владелец дал согласие")
    p_add.add_argument("--headed", action="store_true", help="показать окно браузера")
    p_login_acc = acc_sub.add_parser("login", help="войти заново, если сессия истекла (спросит токен)")
    p_login_acc.add_argument("name", help="имя аккаунта")
    p_login_acc.add_argument("--headed", action="store_true", help="показать окно браузера")
    p_rm = acc_sub.add_parser("remove", help="удалить аккаунт и его сохранённый вход")
    p_rm.add_argument("name", help="имя аккаунта")
    p_acc.set_defaults(func=cmd_accounts)

    p_gui = sub.add_parser("gui", help="открыть окно приложения")
    p_gui.set_defaults(func=cmd_gui)

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
