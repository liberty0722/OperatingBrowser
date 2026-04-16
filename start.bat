@echo off
chcp 65001 >nul 2>&1
cd /d %~dp0
echo ========================================
echo   AI Browser Controller - Starting...
echo ========================================
echo.

:: 检查 uv 是否安装
where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] 未检测到 uv，正在安装...
    pip install uv
    if %errorlevel% neq 0 (
        echo [X] uv 安装失败，请手动执行: pip install uv
        pause
        exit /b 1
    )
    echo [OK] uv 安装成功
)

:: 检查 .env 是否存在
if not exist ".env" (
    if exist ".env.example" (
        echo [!] 未检测到 .env 配置文件，正在从 .env.example 创建...
        copy .env.example .env >nul
        echo [OK] 已创建 .env 文件
        echo.
        echo ==========================================
        echo  请编辑 .env 文件，填入你的 API Key：
        echo    OPENAI_API_KEY=你的密钥
        echo    OPENAI_BASE_URL=你的API地址
        echo    OPENAI_MODEL=你的模型名
        echo ==========================================
        echo.
        notepad .env
        echo 编辑完成后请按任意键继续...
        pause >nul
    ) else (
        echo [X] 未找到 .env 和 .env.example，请手动创建 .env 配置文件
        pause
        exit /b 1
    )
)

:: 安装依赖（uv sync 会自动处理）
echo [*] 正在检查/安装依赖...
uv sync
if %errorlevel% neq 0 (
    echo [X] 依赖安装失败
    pause
    exit /b 1
)

:: 检查并安装 Playwright 浏览器
echo [*] 正在检查 Playwright 浏览器...
uv run playwright install chromium 2>nul
echo [OK] Playwright 浏览器就绪

echo.
echo ========================================
echo   启动完成，打开浏览器访问:
echo   http://localhost:5000
echo ========================================
echo.

:: 自动打开浏览器
start http://localhost:5000

:: 启动服务
uv run python app.py
pause
