"""Общие настройки прогона тестов.

Инвариант: тесты не пишут в рабочее дерево репозитория. Хранилище вариантов
(`app.ws._Store`) по умолчанию кладёт файлы в `backend/data/variants`, поэтому каталог
данных уводится во временный — иначе прогон меняет файлы прямо в репозитории. Переменная
ставится на импорте conftest, до импорта тестовых модулей и приложения: `_Store` читает
её и при первой загрузке с диска, и при каждой записи.
"""

import atexit
import os
import tempfile

_DATA_DIR = tempfile.TemporaryDirectory(prefix="constellation-tests-")
os.environ["CONSTELLATION_DATA_DIR"] = _DATA_DIR.name
atexit.register(_DATA_DIR.cleanup)
