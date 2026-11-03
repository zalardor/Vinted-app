"""Запуск окна приложения Vinted: python gui.py

Окно с тремя полями: refresh-токен, ссылка на объявление, сообщение.
Терминальная версия: python -m vinted_app send <ссылка> "<сообщение>"
"""

import sys

from vinted_app.gui import main

if __name__ == "__main__":
    sys.exit(main())
