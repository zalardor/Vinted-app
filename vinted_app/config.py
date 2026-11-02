from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # Сайт вашей страны: https://www.vinted.fr, https://www.vinted.de, https://www.vinted.pl, https://www.vinted.lt ...
    # Аккаунт привязан к стране, поэтому домен должен совпадать с тем, где вы зарегистрированы.
    base_url: str = "https://www.vinted.fr"
    # Папка профиля браузера: здесь хранятся cookies, т.е. ваша сессия.
    profile_dir: Path = Path(".vinted_profile")
    # Путь к своему Chromium (необязательно). По умолчанию — браузер из `playwright install chromium`.
    chromium_path: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            base_url=os.environ.get("VINTED_BASE_URL", cls.base_url).rstrip("/"),
            profile_dir=Path(os.environ.get("VINTED_PROFILE_DIR", str(cls.profile_dir))),
            chromium_path=os.environ.get("VINTED_CHROMIUM_PATH") or None,
        )
