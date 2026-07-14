@echo off
chcp 65001 >nul
echo ============================================================
echo   FocusFlow Lite — 打包为 Windows EXE
echo ============================================================
echo.

REM 检查 PyInstaller
pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo [1/3] 安装 PyInstaller...
    pip install pyinstaller
) else (
    echo [1/3] PyInstaller 已安装
)

echo [2/3] 开始打包 (可能需要 1-2 分钟)...
echo.

pyinstaller --noconfirm --onefile --windowed ^
    --name "FocusFlow Lite" ^
    --add-data "eye_tracker.py;." ^
    --add-data "screen_monitor.py;." ^
    --add-data "config_camera.json;." ^
    --add-data "apikey.txt;." ^
    --hidden-import mediapipe ^
    --hidden-import cv2 ^
    --hidden-import numpy ^
    --hidden-import mss ^
    --hidden-import PIL ^
    --hidden-import imagehash ^
    --hidden-import requests ^
    --hidden-import PyQt5 ^
    --collect-all mediapipe ^
    focusflow_gui.py

if %errorlevel% equ 0 (
    echo.
    echo [3/3] ✅ 打包完成!
    echo.
    echo EXE 位置: dist\FocusFlow Lite.exe
    echo.
    echo 可以将其复制到桌面使用。
    echo 注意: apikey.txt 需要放在与 EXE 相同的目录下。
) else (
    echo.
    echo ❌ 打包失败，请检查错误信息
)

pause
