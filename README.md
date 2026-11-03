# Vinted-app

Авторизация в аккаунте Vinted из Python через настоящий браузер (Playwright).

## Быстрый старт: main.py

1. Установите зависимости (см. «Установка» ниже).
2. Откройте `main.py` и впишите свои данные в самом начале файла:

   ```python
   VINTED_URL = "https://www.vinted.pl"   # сайт вашей страны
   EMAIL = "you@example.com"
   PASSWORD = "ваш пароль"
   REFRESH_TOKEN = ""                     # можно оставить пустым
   SHOW_BROWSER = False                   # True — показать окно (если Vinted покажет капчу)
   ```

3. Запустите `python main.py`. Если Vinted попросит код подтверждения, введите его в консоли.

Порядок входа: сначала сохранённая сессия (папка `.vinted_profile/` рядом с `main.py`),
потом `REFRESH_TOKEN`, потом `EMAIL` + `PASSWORD`. Поэтому повторные запуски не входят
заново, а данные в начале файла нужны, только когда сохранённая сессия закончилась.
Свой код пишите в `main()` после строки «дальше ваш код».

**Не коммитьте `main.py` с заполненными данными.** Чтобы git не замечал ваши
изменения в этом файле, выполните один раз:

```bash
git update-index --skip-worktree main.py
```

## Три способа войти

| Способ | Команда | Когда удобно |
|---|---|---|
| E-mail и пароль | `login --email you@example.com` | обычный вход, можно без окна |
| Refresh-токен | `login --token` | сервер / без окна, без пароля и кода |
| Вручную в окне | `login` | если сайт показывает капчу |

После любого способа сессия хранится в папке `.vinted_profile/`. Следующие запуски
уже авторизованы, а просроченный access-токен (живёт около суток) скрипт сам обновляет
через `/web/api/auth/refresh`, как это делает сайт.

**Папка `.vinted_profile/`, refresh-токен и пароль дают полный доступ к аккаунту.**
Не коммитьте их и никому не передавайте (`.vinted_profile/` и `.env` уже в `.gitignore`).
Пароль и токен скрипт нигде не сохраняет.

## Установка

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## Использование

Укажите сайт своей страны (аккаунт привязан к стране): через `--base-url` или
переменную `VINTED_BASE_URL`. По умолчанию используется `https://www.vinted.fr`.

### По e-mail и паролю

```bash
python -m vinted_app --base-url https://www.vinted.pl login --email you@example.com
# Пароль Vinted для you@example.com (ввод скрыт): ********
# Введите код подтверждения от Vinted (из письма или SMS): 1234   <- если Vinted попросит
# Готово, вы вошли как my_login (id 123456789).
```

Скрипт заполняет настоящую форму входа на сайте, поэтому сайт сам считает «отпечаток»
устройства и проходит свои проверки. Пароль можно передать через `VINTED_PASSWORD`.
Если Vinted покажет капчу, запустите с окном (`--headed`) и решите её. Код подтверждения
всё равно вводится в консоли.

### По refresh-токену

1. Войдите на сайт Vinted в обычном браузере.
2. Откройте инструменты разработчика (F12): **Application → Cookies** (в Firefox
   **Хранилище → Куки**) → `https://www.vinted.<страна>`.
3. Скопируйте значение cookie **`refresh_token_web`** (длинная строка `eyJ...`).

```bash
python -m vinted_app --base-url https://www.vinted.pl login --token
# Значение cookie refresh_token_web (ввод скрыт): <вставьте>

# или без вопросов, например на сервере:
VINTED_REFRESH_TOKEN='eyJ...' python -m vinted_app --base-url https://www.vinted.pl login --token
```

Токен должен быть с того же сайта (`--base-url`), где вы входили. Токен действует около 7 дней с момента выдачи: когда он закончится, скопируйте новый или войдите по почте. Токен анонимного
посетителя (до входа) и `access_token_web` не подойдут: скрипт об этом скажет.

### Вручную в окне

```bash
python -m vinted_app --base-url https://www.vinted.pl login
```

Откроется браузер: войдите как обычно, скрипт сам заметит вход.

### Проверить вход

```bash
python -m vinted_app --base-url https://www.vinted.pl whoami
# Авторизован как my_login (id 123456789)
```

Чтобы выйти локально, удалите папку `.vinted_profile/`.

### В своём коде

```python
from vinted_app import VintedSession

with VintedSession() as vs:                        # headless=False — показать окно
    vs.login_with_password("you@example.com", password, ask_code=input)
    # или: vs.login_with_token(refresh_token)
    user = vs.require_user()                       # NotLoggedInError, если вход не выполнен
    print("Аккаунт:", user["login"])

    status, data = vs.api_get("/api/v2/users/current")   # GET к API с cookies сессии
    vs.page.screenshot(path="vinted.png")                 # vs.page — обычная страница Playwright
```

## Настройки (переменные окружения)

| Переменная             | По умолчанию            | Что это                                    |
|------------------------|-------------------------|--------------------------------------------|
| `VINTED_BASE_URL`      | `https://www.vinted.fr` | сайт Vinted вашей страны                   |
| `VINTED_PROFILE_DIR`   | `.vinted_profile`       | папка профиля браузера с сессией           |
| `VINTED_PASSWORD`      | —                       | пароль для `login --email` (иначе спросит) |
| `VINTED_REFRESH_TOKEN` | —                       | токен для `login --token` (иначе спросит)  |
| `VINTED_CHROMIUM_PATH` | —                       | свой Chromium вместо скачанного Playwright |

## Если что-то не так

- **«Форма входа не появилась» / «Vinted показал капчу» / «ответ не JSON»** — сайт
  включил проверку «вы человек?». Повторите с `--headed` и пройдите её в окне.
- **«Vinted не принял токен»** — токен истёк или отозван (например, после выхода
  из аккаунта на сайте), либо `--base-url` не совпадает с сайтом токена.
- **Вход не выполнен за 600 с** — увеличьте время: `login --timeout 1200`.
- Автоматизация может противоречить правилам Vinted. Не делайте частых запросов,
  иначе аккаунт могут ограничить.
