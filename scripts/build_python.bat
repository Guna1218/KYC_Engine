@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  build_python.bat  —  Freeze the FastAPI server into a standalone folder
REM  Run this ONCE before  npm run dist:win
REM ─────────────────────────────────────────────────────────────────────────────

echo [1/4] Creating virtual environment...
cd /d "%~dp0.."
if exist python\venv rmdir /s /q python\venv
python -m venv python\venv
if errorlevel 1 (echo ERROR: python -m venv failed & pause & exit /b 1)

echo [2/4] Installing dependencies...
python\venv\Scripts\python.exe -m pip install --upgrade pip
python\venv\Scripts\pip install fastapi "uvicorn[standard]" transformers torch safetensors Pillow pywin32 pyinstaller
if errorlevel 1 (echo ERROR: pip install failed & pause & exit /b 1)

echo [3/4] Freezing server.py with PyInstaller...
if exist python\dist rmdir /s /q python\dist
if exist python\build rmdir /s /q python\build

python\venv\Scripts\pyinstaller ^
  --onedir ^
  --name kyc_server ^
  --distpath python\dist ^
  --workpath python\build ^
  --specpath python ^
  --noconfirm ^
  --hidden-import=fastapi ^
  --hidden-import=fastapi.middleware.cors ^
  --hidden-import=starlette ^
  --hidden-import=starlette.middleware ^
  --hidden-import=starlette.middleware.cors ^
  --hidden-import=uvicorn ^
  --hidden-import=uvicorn.logging ^
  --hidden-import=uvicorn.loops ^
  --hidden-import=uvicorn.loops.auto ^
  --hidden-import=uvicorn.protocols ^
  --hidden-import=uvicorn.protocols.http ^
  --hidden-import=uvicorn.protocols.http.auto ^
  --hidden-import=uvicorn.protocols.http.h11_impl ^
  --hidden-import=uvicorn.protocols.websockets ^
  --hidden-import=uvicorn.protocols.websockets.auto ^
  --hidden-import=uvicorn.lifespan ^
  --hidden-import=uvicorn.lifespan.on ^
  --hidden-import=uvicorn.config ^
  --hidden-import=uvicorn.main ^
  --hidden-import=h11 ^
  --hidden-import=anyio ^
  --hidden-import=anyio._backends._asyncio ^
  --hidden-import=transformers ^
  --hidden-import=torch ^
  --hidden-import=safetensors ^
  --hidden-import=safetensors.torch ^
  --hidden-import=pydantic ^
  --collect-all fastapi ^
  --collect-all starlette ^
  --collect-all uvicorn ^
  python\server.py
if errorlevel 1 (echo ERROR: PyInstaller failed & pause & exit /b 1)

echo [4/4] Done! Folder is at python\dist\kyc_server\
echo Now run:  npm run dist:win
pause
