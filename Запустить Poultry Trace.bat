@echo off
chcp 65001 >nul
cd /d "%~dp0"
where docker >nul 2>nul
if errorlevel 1 (
  echo Docker не найден. Установите Docker Desktop и запустите его, затем повторите.
  pause
  exit /b 1
)
if not exist .env (
  copy .env.example .env >nul
  echo Создан файл .env с демонстрационными паролями.
  echo Для локального ознакомления можно продолжить. Перед реальным использованием замените значения в .env.
)
docker compose up --build
pause
