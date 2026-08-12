# ReplayLab

ReplayLab — настольная лаборатория для разбора и просмотра replay Warcraft III
1.26a. Приложение читает `.w3g`, строит статистику и таймлайн матча, запускает
replay в Warcraft, поддерживает Instant Seek, Skills HUD и операторскую камеру.

## Публичный стабильный канал

Текущая стабильная версия — **0.5.1**.

- [Скачать ReplayLab 0.5.1 для Windows x64](https://github.com/LivetARSmartA/ReplayLab/releases/tag/v0.5.1)
- Публичный репозиторий содержит только стабильный канал.
- Более новые номера могут существовать как внутренние development builds. Они
  не заменяют стабильную версию, пока не пройдут отдельную приёмку.

## Установка

Рекомендуемый вариант не требует Python:

1. Скачайте `ReplayLab-0.5.1.zip` на странице релиза.
2. При желании проверьте SHA-256 по файлу
   `ReplayLab-0.5.1.zip.sha256`.
3. Распакуйте архив в отдельную папку.
4. Запустите `ReplayLab.exe`.

Проверка checksum в PowerShell:

```powershell
Get-FileHash .\ReplayLab-0.5.1.zip -Algorithm SHA256
```

Ожидаемый SHA-256:

```text
E260D669B813C44A3F825DE5EB1D34B0360B056C980DDA3E7E5A186D0AD0BF3F
```

Исходный snapshot на ветке `main` можно запустить отдельно через Python 3.11+:

```powershell
python -m pip install -r requirements.txt
python run_gui.py
```

## Совместимость

- Windows 10/11 x64;
- Warcraft III 1.26a build 6401;
- `.w3g` replay;
- iCCup Launcher для автоматического открытия iCCup replay.

Live-инструменты работают fail-closed: перед доступом к процессу проверяются
сборка Warcraft, `Game.dll` и runtime-объекты. Неизвестная или несовместимая
сборка не получает скрытый fallback.

## Возможности стабильной версии

- локальный разбор `.w3g` и библиотека replay;
- таблица игроков, K/D/A, APM, крипы, инвентарь и события матча;
- чат и временная шкала убийств, серий и ключевых моментов;
- автоматический запуск replay через iCCup или `war3.exe`;
- Instant Seek и возврат назад через контролируемый перезапуск replay;
- Skills HUD с уровнями и cooldown способностей;
- Camera Engine, Follow, Smart Follow, Fly Drone и Orbit;
- экспорт отчёта в JSON;
- локальная ротируемая диагностика в
  `%LOCALAPPDATA%\ReplayLab\logs\ReplayLab.log`.

## Поддержка

- Обычные ошибки и вопросы можно отправлять через
  [GitHub Issues](https://github.com/LivetARSmartA/ReplayLab/issues).
- Уязвимости не следует публиковать в issue. Используйте приватный канал из
  [SECURITY.md](SECURITY.md).

## Назначение репозитория

Этот репозиторий — публичный стабильный release channel. Разработка и
исследовательские материалы ведутся отдельно и остаются приватными. Каждый
публичный выпуск должен иметь version tag, GitHub Release, portable ZIP,
checksum и воспроизводимый release manifest.
