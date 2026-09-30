@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Монтажёр
set PYTHONIOENCODING=utf-8
if not exist ".env" copy ".env.example" ".env" >nul

if exist ".venv\installed.ok" goto run

echo.
echo  Первый запуск: устанавливаю всё необходимое. Это займёт 5-15 минут, дальше запуск мгновенный.
echo.

where winget >nul 2>nul
if errorlevel 1 goto no_winget

rem ---------- Python 3.12 ----------
set "PYEXE="
for /f "delims=" %%i in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PYEXE goto have_python
echo  [1/5] Устанавливаю Python 3.12...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE goto no_python
:have_python
echo  [1/5] Python: %PYEXE%

rem ---------- ffmpeg ----------
where ffmpeg >nul 2>nul
if not errorlevel 1 goto have_ffmpeg
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*" goto have_ffmpeg
echo  [2/5] Устанавливаю ffmpeg...
winget install -e --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
:have_ffmpeg
echo  [2/5] ffmpeg готов

rem ---------- библиотеки ----------
echo  [3/5] Устанавливаю библиотеки Python...
if not exist ".venv\Scripts\python.exe" "%PYEXE%" -m venv .venv
if errorlevel 1 goto pip_error
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto pip_error

where nvidia-smi >nul 2>nul
if errorlevel 1 goto no_gpu
echo  [4/5] Найдена видеокарта NVIDIA - ставлю библиотеки для быстрого распознавания...
".venv\Scripts\python.exe" -m pip install -r requirements-gpu.txt
if errorlevel 1 goto pip_error
goto installed
:no_gpu
echo  [4/5] Видеокарта NVIDIA не найдена - распознавание будет на процессоре, медленнее.
:installed
echo  [5/5] Модель распознавания речи...
".venv\Scripts\python.exe" -m app.model_store
echo ok> ".venv\installed.ok"

:run
echo.
echo  Монтажёр запускается, браузер откроется сам.
echo  Не закрывайте это окно, пока пользуетесь программой.
echo.
".venv\Scripts\python.exe" run.py
pause
exit /b

:no_winget
echo  Не найден winget - установщик программ Windows.
echo  Обновите "Установщик приложений" (App Installer) в Microsoft Store и запустите start.bat снова.
pause
exit /b 1

:no_python
echo  Не удалось установить Python автоматически.
echo  Установите Python 3.12 вручную: https://www.python.org/downloads/ и запустите start.bat снова.
pause
exit /b 1

:pip_error
echo  Ошибка при установке библиотек. Проверьте интернет и запустите start.bat снова.
pause
exit /b 1
