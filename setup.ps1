# AssistentInet — Автоустановка для Windows
# Запуск: правой кнопкой на файле -> "Выполнить с помощью PowerShell"
# Или в PowerShell: Set-ExecutionPolicy Bypass -Scope Process; .\setup.ps1

$ErrorActionPreference = "Stop"
$InstallDir = "$env:USERPROFILE\AssistentInet"

function Write-Step($n, $text) {
    Write-Host ""
    Write-Host "  [$n] $text" -ForegroundColor Cyan
}

function Write-OK($text) {
    Write-Host "      OK: $text" -ForegroundColor Green
}

function Write-Fail($text) {
    Write-Host "      ОШИБКА: $text" -ForegroundColor Red
    Write-Host ""
    Read-Host "Нажми Enter для выхода"
    exit 1
}

Clear-Host
Write-Host ""
Write-Host "  ╔══════════════════════════════════════╗" -ForegroundColor Yellow
Write-Host "  ║    AssistentInet — Автоустановка     ║" -ForegroundColor Yellow
Write-Host "  ╚══════════════════════════════════════╝" -ForegroundColor Yellow

# ── Шаг 1: Python ─────────────────────────────────────────────────────────────
Write-Step "1/4" "Проверка Python..."

$pythonOk = $false
try {
    $ver = python --version 2>&1
    if ($ver -match "Python 3") {
        Write-OK $ver
        $pythonOk = $true
    }
} catch {}

if (-not $pythonOk) {
    Write-Host "      Python не найден. Скачиваю установщик..." -ForegroundColor Yellow

    $pythonUrl = "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe"
    $installer  = "$env:TEMP\python_installer.exe"

    Write-Host "      Загрузка (~25 МБ)..." -ForegroundColor Yellow
    try {
        Invoke-WebRequest -Uri $pythonUrl -OutFile $installer -UseBasicParsing
    } catch {
        Write-Fail "Не удалось скачать Python. Проверь интернет-соединение."
    }

    Write-Host "      Установка Python (тихий режим)..." -ForegroundColor Yellow
    $args = "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0"
    Start-Process -FilePath $installer -ArgumentList $args -Wait

    # Обновляем PATH в текущей сессии
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "User") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH", "Machine")

    try {
        $ver = python --version 2>&1
        if ($ver -match "Python 3") {
            Write-OK "Установлен: $ver"
        } else {
            Write-Fail "Python установлен, но не найден в PATH. Перезапусти скрипт."
        }
    } catch {
        Write-Fail "Установка не удалась. Установи Python вручную с python.org"
    }
}

# ── Шаг 2: Скачать проект ─────────────────────────────────────────────────────
Write-Step "2/4" "Скачиваю проект AssistentInet..."

$zipUrl  = "https://github.com/unidel2035/AssistentInet/archive/refs/heads/master.zip"
$zipFile = "$env:TEMP\AssistentInet.zip"

try {
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipFile -UseBasicParsing
} catch {
    Write-Fail "Не удалось скачать проект с GitHub."
}

if (Test-Path $InstallDir) {
    Remove-Item $InstallDir -Recurse -Force
}

Expand-Archive -Path $zipFile -DestinationPath "$env:TEMP\AssistentInet_tmp" -Force
Move-Item "$env:TEMP\AssistentInet_tmp\AssistentInet-master" $InstallDir
Remove-Item "$env:TEMP\AssistentInet_tmp" -Recurse -Force
Remove-Item $zipFile

Write-OK "Установлен в $InstallDir"

# ── Шаг 3: Зависимости ────────────────────────────────────────────────────────
Write-Step "3/4" "Устанавливаю зависимости Python..."

Set-Location $InstallDir
try {
    python -m pip install -r requirements.txt --quiet --disable-pip-version-check
} catch {
    Write-Fail "Ошибка при установке зависимостей."
}
Write-OK "Все пакеты установлены"

# ── Шаг 4: Демо-запуск ────────────────────────────────────────────────────────
Write-Step "4/4" "Запускаю демо-режим..."

Write-Host ""
Write-Host "  ✓ Установка завершена!" -ForegroundColor Green
Write-Host ""
Write-Host "  Открываю браузер: http://localhost:8090" -ForegroundColor White
Write-Host "  Для остановки нажми Ctrl+C" -ForegroundColor Gray
Write-Host ""

Start-Process "http://localhost:8090"
Start-Sleep -Seconds 2
python server.py --demo --web 8090
