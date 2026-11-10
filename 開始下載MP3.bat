@echo off
chcp 65001 >nul
title MP3 Music Downloader
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3 first.
    pause
    exit /b 1
)

python -c "import yt_dlp, imageio_ffmpeg, mutagen" >nul 2>nul
if errorlevel 1 (
    echo First run: installing yt-dlp / ffmpeg / mutagen, please wait...
    python -m pip install --user yt-dlp imageio-ffmpeg mutagen
)

echo Starting server... browser will open automatically.
echo IMPORTANT: Keep this window open while downloading.
python app.py
pause
