@echo off
chcp 65001 >nul
title AssistentInet — Шумомер

echo.
echo  ╔══════════════════════════════════════╗
echo  ║      AssistentInet — Шумомер         ║
echo  ╚══════════════════════════════════════╝
echo.

:: Проверяем Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ОШИБКА] Python не найден!
    echo.
    echo  Установи Python с сайта: https://www.python.org/downloads/
    echo  Важно: при установке поставь галочку "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

:: Устанавливаем зависимости (только при первом запуске)
if not exist ".deps_installed" (
    echo  [1/2] Устанавливаю зависимости...
    pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo  [ОШИБКА] Не удалось установить зависимости
        pause
        exit /b 1
    )
    echo installed > .deps_installed
    echo  [1/2] Готово.
)

:: Определяем режим запуска
echo  [2/2] Запуск сервера...
echo.

if "%1"=="demo" (
    echo  Режим: ДЕМО (без прибора)
    echo  Открой браузер: http://localhost:8090
    echo  Остановить: Ctrl+C
    echo.
    python server.py --demo --web 8090
) else if "%1"=="" (
    echo  Доступные режимы:
    echo.
    echo    start.bat demo       — демо-режим (без прибора)
    echo    start.bat COM3       — реальный прибор на порту COM3
    echo    start.bat COM4       — реальный прибор на порту COM4
    echo.
    echo  Как найти порт прибора:
    echo    1. Подключи USB кабель к шумомеру
    echo    2. Открой "Диспетчер устройств" (Win+X → Диспетчер устройств)
    echo    3. Раздел "Порты (COM и LPT)" — смотри что появилось
    echo    4. Запусти: start.bat COM3  (или другой номер)
    echo.
    pause
) else (
    echo  Режим: РЕАЛЬНЫЙ ПРИБОР (порт %1)
    echo  Открой браузер: http://localhost:8090
    echo  Остановить: Ctrl+C
    echo.
    python server.py --port %1 --web 8090
)
