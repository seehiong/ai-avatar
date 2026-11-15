@echo off
rem Windows: set up (first run) and start the backend, which also serves the web client.
rem Then open http://127.0.0.1:8080. Extra args go to server.py (e.g. run.bat --model qwen3.8:27b).
rem Needs: Python 3.10+ (python.org installer, "py" launcher) and Ollama for Windows with a chat model.
rem --photoreal also starts the photoreal avatar service (tools\photoreal) in its own window. It uses
rem tools\photoreal\.venv\Scripts\python.exe (or %USERPROFILE%\miniforge3\envs\avatar-gpu\python.exe, or AVATAR_GPU_PY).
rem PHOTOREAL_ARGS tunes it (default: --idle-stride 3 --talk-stride 5; pass "--library" for 44 fps pre-rendered motion).
setlocal EnableExtensions
cd /d "%~dp0"

rem --- split off --photoreal; everything else goes to server.py
set "PHOTOREAL=0"
set "ARGS="
:args
if "%~1"=="" goto argsdone
if /i "%~1"=="--photoreal" (set "PHOTOREAL=1") else (set ARGS=%ARGS% %1)
shift
goto args
:argsdone

rem --- pick a Python: prefer the "py" launcher, fall back to "python" on PATH
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3.10+ not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  exit /b 1
)

set "VPY=backend\.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo Creating virtual environment...
  %PY% -m venv backend\.venv || exit /b 1
  "%VPY%" -m pip install --upgrade pip
  "%VPY%" -m pip install -r backend\requirements.txt || exit /b 1
)

rem --- Piper: fallback voice
if not exist "backend\voices\en_US-lessac-medium.onnx" (
  "%VPY%" -m piper.download_voices --data-dir backend\voices en_US-lessac-medium
)

rem --- Kokoro-82M (Apache-2.0): default, more natural voice (curl.exe ships with Windows 10/11)
if not exist "backend\voices\kokoro\model.onnx" (
  if not exist "backend\voices\kokoro" mkdir "backend\voices\kokoro"
  set "H=https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/resolve/main"
  call :kokoro
)

rem --- neural lip sync: wav2arkit (Apache-2.0, ~400 MB): speech audio -> ARKit blendshapes
if not exist "backend\models\wav2arkit\wav2arkit_cpu.onnx.data" (
  if not exist "backend\models\wav2arkit" mkdir "backend\models\wav2arkit"
  set "W=https://huggingface.co/myned-ai/wav2arkit_cpu/resolve/main"
  call :wav2arkit
)

if "%PHOTOREAL%"=="1" (
  call :photoreal || exit /b 1
  echo Starting backend - open http://127.0.0.1:8080/?avatar=photoreal once it says "listening"
) else (
  echo Starting backend - open http://127.0.0.1:8080 once it says "listening"
)
cd backend
"..\%VPY%" server.py %ARGS%
exit /b %ERRORLEVEL%

:wav2arkit
for %%f in (wav2arkit_cpu.onnx wav2arkit_cpu.onnx.data config.json) do (
  curl.exe -fL -o "backend\models\wav2arkit\%%f" "%W%/%%f" || echo wav2arkit download failed; phoneme visemes will be used
)
exit /b 0

:photoreal
if not defined AVATAR_GPU_PY (
  if exist "%CD%\tools\photoreal\.venv\Scripts\python.exe" (
    set "AVATAR_GPU_PY=%CD%\tools\photoreal\.venv\Scripts\python.exe"
  ) else (
    set "AVATAR_GPU_PY=%USERPROFILE%\miniforge3\envs\avatar-gpu\python.exe"
  )
)
if not exist "%AVATAR_GPU_PY%" (
  echo Photoreal needs the GPU Python environment ^(see README, "Photoreal face"^). Set AVATAR_GPU_PY to its python.exe.
  exit /b 1
)
if not defined PHOTOREAL_ARGS set "PHOTOREAL_ARGS=--idle-stride 3 --talk-stride 5"
set "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1"
echo Starting the photoreal avatar service in its own window ^(ws://127.0.0.1:8766^) with %AVATAR_GPU_PY%
start "ai-avatar photoreal" /D "%CD%\tools\photoreal" cmd /k ""%AVATAR_GPU_PY%" photoreal_server.py %PHOTOREAL_ARGS%"
exit /b 0

:kokoro
curl.exe -fL -o "backend\voices\kokoro\model.onnx" "%H%/onnx/model.onnx" || echo Kokoro download failed; Piper will be used
for %%v in (am_michael am_adam am_eric am_liam am_puck bm_george) do (
  curl.exe -fsL -o "backend\voices\kokoro\%%v.bin" "%H%/voices/%%v.bin"
)
exit /b 0
