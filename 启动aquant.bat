@echo off
title aquant 启动器
cd /d "%~dp0"

echo ============================================
echo   aquant Research Room
echo ============================================
echo.

rem 检查 Node.js
where node >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Node.js，请先安装 Node 22 或更高版本: https://nodejs.org/
  pause
  exit /b 1
)

rem 检查 Python 与 pydantic
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Python，请先安装 Python 3.11 或更高版本
  pause
  exit /b 1
)
python -c "import pydantic" >nul 2>nul
if errorlevel 1 (
  echo [准备] 正在安装 Python 依赖 pydantic ...
  python -m pip install "pydantic>=2.7,<3"
  if errorlevel 1 (
    echo [错误] pydantic 安装失败，请手动执行: python -m pip install pydantic
    pause
    exit /b 1
  )
)

rem 首次运行: 安装前端依赖
if not exist "node_modules" (
  echo [准备] 首次运行，正在安装依赖，请稍候 ...
  call npm install
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试
    pause
    exit /b 1
  )
)

rem 首次运行: 下载 Electron 运行时
if not exist "node_modules\electron\dist\electron.exe" (
  echo [准备] 正在下载 Electron 运行时 ...
  call npm run setup:electron
  if errorlevel 1 (
    echo [错误] Electron 运行时下载失败，请检查网络后重试
    pause
    exit /b 1
  )
)

rem 首次运行: 构建界面
if not exist "apps\desktop\dist\index.html" (
  echo [准备] 正在构建界面，约需十几秒 ...
  call npm run build
  if errorlevel 1 (
    echo [错误] 构建失败
    pause
    exit /b 1
  )
)

echo [启动] 正在打开 aquant ...
call npm start
if errorlevel 1 (
  echo.
  echo [提示] 启动失败。如果最近修改过代码，请先手动执行 npm run build 后再双击本文件。
  pause
  exit /b 1
)

echo aquant 已退出。
