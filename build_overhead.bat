@echo off
rem Builds Overhead.exe: a single file that runs on any Windows 10/11 PC with no Python install.
rem Run this once, on your PC, from the same folder as overhead.py.
python -m pip install --upgrade pyinstaller pillow
python -m PyInstaller --clean --noconfirm --onefile --noconsole --name Overhead --hidden-import PIL._tkinter_finder overhead.py
echo.
echo Done. Your app is at: dist\Overhead.exe
pause
