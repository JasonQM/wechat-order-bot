@echo off
cd /d "%~dp0"

rem ============================================================
rem  发送模式
rem    set SEND=--send   -> 真的往群里发报价和图片
rem    set SEND=         -> 安全模式：只识别、只落库，不发消息
rem
rem  想临时观察识别效果而不惊动客户，就把下面改成 set SEND=
rem ============================================================
set SEND=--send

rem 两轮之间的间隔（秒）。
rem 画面没变化时一轮只要几十毫秒（像素哨兵直接返回），所以 1 秒是安全的。
set INTERVAL=1

title WeChat Order Bot

:restart
echo.
echo ============================================================
echo   Bot starting   %date% %time%
if "%SEND%"=="--send" (
  echo   Mode: LIVE - replies WILL be sent to groups
) else (
  echo   Mode: SAFE - identify only, nothing is sent
)
echo   Interval: %INTERVAL%s
echo ============================================================
echo.

python src\main.py loop %SEND% --interval %INTERVAL%
set CODE=%errorlevel%

rem 退出码 2 = 已经有一个实例在跑，被单实例锁挡住了。
rem 这种情况必须【停止重启】：否则双击两次这个 bat 会得到两个窗口，
rem 第二个每 10 秒重启一轮、每轮都被拒，日志里刷满"已经有一个机器人在运行"，
rem 而且会一直占着一个终端，用户很容易误以为机器人在工作。
if "%CODE%"=="2" goto already_running

echo.
echo ------------------------------------------------------------
echo   Bot exited at %time% (code %CODE%)
echo   Restarting in 10 seconds. Close this window to stop.
echo ------------------------------------------------------------
timeout /t 10 /nobreak >nul
goto restart

:already_running
echo.
echo ------------------------------------------------------------
echo   已经有一个机器人窗口在运行，本窗口不再启动。
echo   要换成本窗口运行，请先关掉另一个窗口，再重新双击这里。
echo ------------------------------------------------------------
echo.
pause
exit /b 2
