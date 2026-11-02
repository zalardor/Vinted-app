# Vinted-app

Авторизация в аккаунте Vinted из Python через настоящий браузер (Playwright).

## Как это работает

У Vinted нет публичного API для входа, а сайт защищён Cloudflare и DataDome
(капча, проверка «вы человек?», коды подтверждения на новом устройстве).
Поэтому скрипт **не отправляет пароль сам**, а работает так:

1. `login` открывает окно браузера со страницей входа. Вы входите сами, как обычно.
2. Скрипт каждые несколько секунд проверяет `/api/v2/users/current` и, как только
   вход выполнен, сообщает, под каким аккаунтом вы вошли.
3. Cookies сессии остаются в папке `.vinted_profile/`. Следующие запуски
   (в том числе без окна) уже авторизованы. Срок действия сессии показывают `login` и `whoami`.

Пароль нигде не сохраняется. **Папка `.vinted_profile/` даёт полный доступ к аккаунту**:
не коммитьте её и никому не передавайте (она уже в `.gitignore`).

## Установка

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## Использование

Укажите сайт своей страны (аккаунт привязан к стране): через `--base-url` или
переменную окружения `VINTED_BASE_URL`. По умолчанию используется `https://www.vinted.fr`.

```bash
# 1. Войти (откроется окно браузера)
python -m vinted_app --base-url https://www.vinted.pl login

# 2. Проверить, что вход сохранён (без окна)
python -m vinted_app --base-url https://www.vinted.pl whoami
# Авторизован как my_login (id 123456789)
```

Чтобы выйти локально, удалите папку `.vinted_profile/`.

### В своём коде

```python
from vinted_app import VintedSession

with VintedSession() as vs:            # headless=False — показать окно
    user = vs.require_user()           # NotLoggedInError, если вход не выполнен
    print("Аккаунт:", user["login"])

    status, data = vs.api_get("/api/v2/users/current")   # GET к API с cookies сессии
    vs.page.screenshot(path="vinted.png")                 # vs.page — обычная страница Playwright
```

## Настройки (переменные окружения)

| Переменная             | По умолчанию            | Что это                                   |
|------------------------|-------------------------|-------------------------------------------|
| `VINTED_BASE_URL`      | `https://www.vinted.fr` | сайт Vinted вашей страны                  |
| `VINTED_PROFILE_DIR`   | `.vinted_profile`       | папка профиля браузера с сессией          |
| `VINTED_CHROMIUM_PATH` | —                       | свой Chromium вместо скачанного Playwright |

## Если что-то не так

- **«ответ не JSON… антибот-проверка»** — запустите с окном (`whoami --headed`)
  и пройдите проверку в браузере.
- **Вход не выполнен за 600 с** — увеличьте время: `login --timeout 1200`.
- Учтите, что автоматизация может противоречить правилам Vinted. Не делайте
  частых запросов, иначе аккаунт могут ограничить.
