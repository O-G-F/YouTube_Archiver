@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem yt-dlp.bat  (YouTube / Twitch 分岐 + overlay + ログ保存)
rem
rem 修正点（2025-12-28）:
rem - call :RUN_YTDLP に「%(title)s 等の % を含むテンプレ」を引数渡ししない
rem   -> 引数再展開で -o / URL が壊れるのを根絶
rem - :RUN_YTDLP は環境変数 ARG_* を参照して実行
rem - ログヘッダは PowerShell Add-Content で安全に書き込み
rem ============================================================

set "BASE_DIR=%~dp0"
cd /d "%BASE_DIR%"

set "YTDLP=%BASE_DIR%yt-dlp.exe"
set "YTDLP_EXE=%BASE_DIR%yt-dlp.exe"
set "DENO_EXE="
set "FFMPEG_LOCATION="
set "FFMPEG_SOURCE="
set "DENO_INSTALL_LOG=%BASE_DIR%log\deno_install.log"
set "SETTINGS=%BASE_DIR%settings.ini"
set "RUNNER_PS1=%BASE_DIR%yt-dlp_runner.ps1"

set "CFG_BASE=%BASE_DIR%yt-dlp_base.conf"
set "CFG_YT=%BASE_DIR%yt-dlp_youtube.conf"
set "CFG_TW=%BASE_DIR%yt-dlp_twitch.conf"

set "OVL_SELECT=%BASE_DIR%yt-dlp.conf"
set "OVL_LIST=%BASE_DIR%yt-dlp(List).conf"
set "OVL_MUSIC=%BASE_DIR%yt-dlp(Music).conf"
set "OVL_MOVIE=%BASE_DIR%yt-dlp(Movie).conf"
set "OVL_THUMB=%BASE_DIR%yt-dlp(Thumbnail).conf"
set "OVL_PLMUSIC=%BASE_DIR%yt-dlp(Playlist-Music).conf"
set "OVL_PLMOVIE=%BASE_DIR%yt-dlp(Playlist-Movie).conf"
set "OVL_CHANNEL=%BASE_DIR%yt-dlp(ChannelArchive).conf"

set "OUTROOT_YT=D:\YouTube"
set "OUTROOT_TW=D:\Twitch"
set "LOGROOT=%BASE_DIR%log"

rem --- Twitch コメント(チャット) 取得ツール設定（任意） ---
set "TWCLI_EXE=%BASE_DIR%TwitchDownloaderCLI.exe"
set "TWCHAT_EMBED=0"
set "CHATDL_PY=py"
set "CHATDL_ARGS=-3 -m chat_downloader"

call :LOAD_SETTINGS
rem ログは常に bat と同じ実行ディレクトリ配下の log に保存する
set "LOGROOT=%BASE_DIR%log"
call :ENSURE_DIR "!LOGROOT!"

if not exist "!YTDLP!" (
  echo [ERR] yt-dlp.exe が見つかりません: "!YTDLP!"
  pause
  exit /b 1
)

call :CHECK_FILE "!RUNNER_PS1!"
call :CHECK_FILE "!CFG_BASE!"
call :CHECK_FILE "!CFG_YT!"
call :CHECK_FILE "!CFG_TW!"

call :CHECK_FILE "!OVL_SELECT!"
call :CHECK_FILE "!OVL_LIST!"
call :CHECK_FILE "!OVL_MUSIC!"
call :CHECK_FILE "!OVL_MOVIE!"
call :CHECK_FILE "!OVL_THUMB!"
call :CHECK_FILE "!OVL_PLMUSIC!"
call :CHECK_FILE "!OVL_PLMOVIE!"
call :CHECK_FILE "!OVL_CHANNEL!"

rem conf パース確認（代表で YouTube）
"!YTDLP!" --ignore-config --config-locations "!CFG_BASE!" --config-locations "!CFG_YT!" --version >nul 2>&1
if errorlevel 1 (
  echo [ERR] conf の読み込みに失敗しました: "!CFG_YT!"
  echo       conf が CP932^(Shift_JIS^) で保存されているか確認してください。
  pause
  exit /b 1
)

rem ============================================================
rem 0) 更新（更新があったときのみログ生成）
rem ============================================================
call :TS
set "TMP_UPD=%TEMP%\yt-dlp_update_!TS!.log"

"!YTDLP!" --ignore-config --update-to nightly > "!TMP_UPD!" 2>&1
type "!TMP_UPD!"

findstr /I /C:"Updated yt-dlp to" /C:"Updated yt-dlp.exe to" "!TMP_UPD!" >nul
if not errorlevel 1 (
  move /Y "!TMP_UPD!" "!LOGROOT!\!TS!_00_update.log" >nul
) else (
  del /Q "!TMP_UPD!" >nul 2>&1
)

echo.
"!YTDLP!" --ignore-config --version
echo.

rem ============================================================
rem 1) プラットフォーム選択（Enter=YouTube）
rem ============================================================
:PLATFORM
cls
echo yt-dlp
echo.
echo 1. YouTube (デフォルト)
echo 2. Twitch
echo 0. 終了
echo.

set "PLAT="
set /p "PLAT=プラットフォーム選択(番号のみ): "
if "!PLAT!"=="" set "PLAT=1"
if "!PLAT!"=="0" goto END

if "!PLAT!"=="1" (
  set "PLATNAME=YouTube"
  set "PLATCFG=!CFG_YT!"
  set "OUTROOT=!OUTROOT_YT!"
  call :ENSURE_DENO
) else if "!PLAT!"=="2" (
  set "PLATNAME=Twitch"
  set "PLATCFG=!CFG_TW!"
  set "OUTROOT=!OUTROOT_TW!"
) else (
  echo.
  echo 入力が不正です。
  pause
  goto PLATFORM
)

call :SELECT_FFMPEG
call :ENSURE_DIR "!OUTROOT!"

rem ============================================================
rem 2) 動作選択（Enter=4:単体保存(動画)）
rem ============================================================
:MENU
cls
echo yt-dlp  ^(!PLATNAME!^)
echo.
echo 1.選択保存
echo 2.リスト保存^(dlurl.txt^)
echo 3.単体保存^(音楽^)
echo 4.単体保存^(動画^) ^(デフォルト^)
echo 5.サムネイル保存
echo 6.プレイリスト保存^(音楽^)
echo 7.プレイリスト保存^(動画^)
echo 8.チャンネルアーカイブ
echo 9.保存先設定^(デフォルト^)
echo 10.プラットフォーム変更
echo.
echo 0.終了
echo.

set "ACT="
set /p "ACT=動作選択(番号のみ): "
if "!ACT!"=="" set "ACT=4"
if "!ACT!"=="0" goto END
if "!ACT!"=="10" goto PLATFORM

call :TS
set "RUN_LOG=!LOGROOT!\!TS!_!PLATNAME!_ACT!ACT!.log"
set "FAILED_LOG=!LOGROOT!\!TS!_!PLATNAME!_ACT!ACT!_ERROR.log"

if "!ACT!"=="9" goto ACT_SETTINGS
if "!ACT!"=="1" goto ACT_SELECT
if "!ACT!"=="2" goto ACT_LIST
if "!ACT!"=="3" goto ACT_SINGLE_MUSIC
if "!ACT!"=="4" goto ACT_SINGLE_MOVIE
if "!ACT!"=="5" goto ACT_THUMB
if "!ACT!"=="6" goto ACT_PL_MUSIC
if "!ACT!"=="7" goto ACT_PL_MOVIE
if "!ACT!"=="8" goto ACT_CHANNEL

echo.
echo 入力が不正です。
goto AFTER


:ASK_OUTROOT
rem 保存先ルートを実行ごとに確認する。Enter の場合は現在のプラットフォーム既定値を使用。
echo.
echo 保存先ルートを入力してください。
echo 入力なしの場合は既定値を使用します: !OUTROOT!
echo 以後、conf の用途別階層^(動画 / 動画プレイリスト / Channel Archive 等^)をこの下に作成します。
set "NEWOUTROOT="
set /p "NEWOUTROOT=保存先ルート [!OUTROOT!]: "
if defined NEWOUTROOT (
  set "OUTROOT=!NEWOUTROOT!"
  if "!PLAT!"=="1" set "OUTROOT_YT=!NEWOUTROOT!"
  if "!PLAT!"=="2" set "OUTROOT_TW=!NEWOUTROOT!"
)
exit /b 0

:ACT_SETTINGS
cls
echo 保存先設定^(デフォルト^)
echo.
echo 現在:
echo   YouTube: !OUTROOT_YT!
echo   Twitch : !OUTROOT_TW!
echo   Log    : !LOGROOT!
echo   TWCLI  : !TWCLI_EXE!
echo   ChatDL : !CHATDL_PY! !CHATDL_ARGS!
echo.
echo 1. YouTube 保存先変更
echo 2. Twitch  保存先変更
echo 3. Log フォルダ表示^(bat直下\log固定^)
echo 4. TwitchDownloaderCLI.exe のパス変更
echo 5. chat-downloader^(Python^) の実行設定変更
echo 0. キャンセル
echo.

set "SSEL="
set /p "SSEL=選択(番号のみ): "
if "!SSEL!"=="0" goto AFTER

if "!SSEL!"=="1" (
  set "NEWPATH="
  set /p "NEWPATH=新しい YouTube 保存先: "
  if defined NEWPATH (
    set "OUTROOT_YT=!NEWPATH!"
    call :ENSURE_DIR "!OUTROOT_YT!"
    if "!PLAT!"=="1" set "OUTROOT=!OUTROOT_YT!"
  )
) else if "!SSEL!"=="2" (
  set "NEWPATH="
  set /p "NEWPATH=新しい Twitch 保存先: "
  if defined NEWPATH (
    set "OUTROOT_TW=!NEWPATH!"
    call :ENSURE_DIR "!OUTROOT_TW!"
    if "!PLAT!"=="2" set "OUTROOT=!OUTROOT_TW!"
  )
) else if "!SSEL!"=="3" (
  echo.
  echo Log フォルダは bat と同じ実行ディレクトリ配下に固定です: !LOGROOT!
) else if "!SSEL!"=="4" (
  set "NEWPATH="
  set /p "NEWPATH=TwitchDownloaderCLI.exe のパス: "
  if defined NEWPATH (
    set "TWCLI_EXE=!NEWPATH!"
  )
) else if "!SSEL!"=="5" (
  set "NEWPY="
  set /p "NEWPY=chat-downloader 用 Python(例: py / python): "
  if defined NEWPY set "CHATDL_PY=!NEWPY!"
  set "NEWARGS="
  set /p "NEWARGS=chat-downloader 実行引数(例: -3 -m chat_downloader): "
  if defined NEWARGS set "CHATDL_ARGS=!NEWARGS!"
) else (
  echo.
  echo 入力が不正です。
  goto AFTER
)

call :SAVE_SETTINGS
echo.
echo 設定を保存しました: !SETTINGS!
goto AFTER

rem ------------------------------------------------------------
rem 各動作：ARG_* をセットして :RUN_YTDLP を呼ぶ（引数なし）
rem ------------------------------------------------------------

:ACT_SELECT
call :RESET_EXTRA_ARGS
set "URL="
echo.
set /p "URL=URL: "
if not defined URL (
  echo.
  echo URL が空です。
  goto AFTER
)
call :ASK_OUTROOT
call :ENSURE_DIR "!OUTROOT!"
set "OUTTPL=!OUTROOT!\%%(title)s\%%(title)s.%%(ext)s"

rem --- format list (-F) and choose (-f) ---
set "ARG_PCONF=!PLATCFG!"
set "ARG_OCONF=!OVL_SELECT!"
set "ARG_URL=!URL!"
call :ASK_TW_CHAT
call :SHOW_FORMATS

set "FMT="
set /p "FMT=Format code (-F の 'format code') を入力 (Enter=default): "
if defined FMT set "ARG_FMT=!FMT!"

call :ASK_YT_EXTRAS

set "ARG_PCONF=!PLATCFG!"
set "ARG_OCONF=!OVL_SELECT!"
set "ARG_OUTTPL=!OUTTPL!"
set "ARG_URL=!URL!"
call :RUN_YTDLP
goto AFTER

:ACT_LIST
call :RESET_EXTRA_ARGS
set "DLURL=!BASE_DIR!dlurl.txt"
if not exist "!DLURL!" (
  echo.
  echo [ERR] dlurl.txt が見つかりません: "!DLURL!"
  goto AFTER
)
call :ASK_OUTROOT
call :ENSURE_DIR "!OUTROOT!"
set "OUTDIR=!OUTROOT!\list"
call :ENSURE_DIR "!OUTDIR!"
set "OUTTPL=!OUTDIR!\%%(title)s\%%(title)s.%%(ext)s"

call :ASK_YT_EXTRAS
set "ARG_PCONF=!PLATCFG!"
set "ARG_OCONF=!OVL_LIST!"
set "ARG_OUTTPL=!OUTTPL!"
set "ARG_BATCHFILE=!DLURL!"
set "ARG_URL="
call :RUN_YTDLP
goto AFTER

:ACT_SINGLE_MUSIC
call :RESET_EXTRA_ARGS
set "URL="
echo.
set /p "URL=URL: "
if not defined URL (
  echo.
  echo URL が空です。
  goto AFTER
)
call :ASK_OUTROOT
call :ENSURE_DIR "!OUTROOT!"
set "OUTDIR=!OUTROOT!\音楽"
call :ENSURE_DIR "!OUTDIR!"
set "OUTTPL=!OUTDIR!\%%(title)s\%%(title)s.%%(ext)s"

call :ASK_YT_EXTRAS
set "ARG_PCONF=!PLATCFG!"
set "ARG_OCONF=!OVL_MUSIC!"
set "ARG_OUTTPL=!OUTTPL!"
set "ARG_URL=!URL!"
call :ASK_TW_CHAT
call :RUN_YTDLP
goto AFTER

:ACT_SINGLE_MOVIE
call :RESET_EXTRA_ARGS
set "URL="
echo.
set /p "URL=URL: "
if not defined URL (
  echo.
  echo URL が空です。
  goto AFTER
)
call :ASK_OUTROOT
call :ENSURE_DIR "!OUTROOT!"
set "OUTDIR=!OUTROOT!\動画"
call :ENSURE_DIR "!OUTDIR!"
set "OUTTPL=!OUTDIR!\%%(title)s\%%(title)s.%%(ext)s"

call :ASK_YT_EXTRAS
set "ARG_PCONF=!PLATCFG!"
set "ARG_OCONF=!OVL_MOVIE!"
set "ARG_OUTTPL=!OUTTPL!"
set "ARG_URL=!URL!"
call :ASK_TW_CHAT
call :RUN_YTDLP
goto AFTER

:ACT_THUMB
set "URL="
echo.
set /p "URL=URL: "
if not defined URL (
  echo.
  echo URL が空です。
  goto AFTER
)
call :ASK_OUTROOT
call :ENSURE_DIR "!OUTROOT!"
set "OUTDIR=!OUTROOT!\サムネイル"
call :ENSURE_DIR "!OUTDIR!"
set "OUTTPL=!OUTDIR!\%%(title)s\%%(title)s.%%(ext)s"

set "ARG_PCONF=!PLATCFG!"
set "ARG_OCONF=!OVL_THUMB!"
set "ARG_OUTTPL=!OUTTPL!"
set "ARG_URL=!URL!"
call :RUN_YTDLP
goto AFTER

:ACT_PL_MUSIC
call :RESET_EXTRA_ARGS
set "URL="
echo.
set /p "URL=プレイリストURL: "
if not defined URL (
  echo.
  echo URL が空です。
  goto AFTER
)
call :ASK_OUTROOT
call :ENSURE_DIR "!OUTROOT!"
set "OUTDIR=!OUTROOT!\音楽プレイリスト"
call :ENSURE_DIR "!OUTDIR!"
set "OUTTPL=!OUTDIR!\%%(playlist_title)s\%%(title)s\%%(title)s.%%(ext)s"

call :ASK_YT_EXTRAS
set "ARG_PCONF=!PLATCFG!"
set "ARG_OCONF=!OVL_PLMUSIC!"
set "ARG_OUTTPL=!OUTTPL!"
set "ARG_URL=!URL!"
call :RUN_YTDLP
goto AFTER

:ACT_PL_MOVIE
call :RESET_EXTRA_ARGS
set "URL="
echo.
set /p "URL=プレイリストURL: "
if not defined URL (
  echo.
  echo URL が空です。
  goto AFTER
)
call :ASK_OUTROOT
call :ENSURE_DIR "!OUTROOT!"
set "OUTDIR=!OUTROOT!\動画プレイリスト"
call :ENSURE_DIR "!OUTDIR!"
set "OUTTPL=!OUTDIR!\%%(playlist_title)s\%%(title)s\%%(title)s.%%(ext)s"

call :ASK_YT_EXTRAS
set "ARG_PCONF=!PLATCFG!"
set "ARG_OCONF=!OVL_PLMOVIE!"
set "ARG_OUTTPL=!OUTTPL!"
set "ARG_URL=!URL!"
call :RUN_YTDLP
goto AFTER

:ACT_CHANNEL
call :RESET_EXTRA_ARGS
set "URL="
echo.
set /p "URL=チャンネルURL: "
if not defined URL (
  echo.
  echo URL が空です。
  goto AFTER
)
call :ASK_OUTROOT
call :ENSURE_DIR "!OUTROOT!"
set "OUTDIR=!OUTROOT!\Channel Archive"
call :ENSURE_DIR "!OUTDIR!"
set "OUTTPL=!OUTDIR!\%%(channel)s\%%(upload_date)s\%%(title)s\%%(title)s.%%(ext)s"

call :ASK_YT_EXTRAS
set "ARG_PCONF=!PLATCFG!"
set "ARG_OCONF=!OVL_CHANNEL!"
set "ARG_OUTTPL=!OUTTPL!"
set "ARG_URL=!URL!"
call :RUN_YTDLP
goto AFTER

:AFTER
echo.
echo ============================================================
echo 完了しました。
echo ログ: !RUN_LOG!
echo ============================================================
echo.
pause
goto MENU

:END
endlocal
exit /b 0

:ASK_TW_CHAT
rem ============================================================
rem Twitch: コメント(チャット)のダウンロード設定
rem   - Twitch のときだけ有効（PLAT=2）
rem   - URL を判定して、可能なら chat_downloader / TwitchDownloaderCLI でチャットを保存
rem   - 実体の実行は :RUN_YTDLP 内（同時実行）で行う
rem
rem 出力:
rem   ARG_TWCHAT_ENABLE      1/0
rem   ARG_TWCHAT_MODE        TDCLI / CHATDL
rem   ARG_TWCHAT_KIND        LIVE / VOD / CLIP / OTHER
rem   ARG_TWCHAT_KEY         id or username
rem   ARG_TWCHAT_KEY_SAFE    ファイル名用（記号除去）
rem   ARG_TWCHAT_OUT_JSON    保存先 JSON
rem   ARG_TWCHAT_OUT_LOG     stdout ログ
rem   ARG_TWCHAT_ERR_LOG     stderr ログ
rem   ARG_TWCHAT_STOP_ON_END 1/0 （LIVE は 1）
rem ============================================================
if not "!PLAT!"=="2" exit /b 0
if not defined ARG_URL exit /b 0

set "ARG_TWCHAT_ENABLE="
set "ARG_TWCHAT_MODE="
set "ARG_TWCHAT_KIND="
set "ARG_TWCHAT_KEY="
set "ARG_TWCHAT_KEY_SAFE="
set "ARG_TWCHAT_OUT_JSON="
set "ARG_TWCHAT_OUT_LOG="
set "ARG_TWCHAT_ERR_LOG="
set "ARG_TWCHAT_STOP_ON_END="

echo.
set "CHSEL="
set /p "CHSEL=Twitch: コメント(チャット)をダウンロードしますか? (Y/n) [Y]: "
if /i "!CHSEL!"=="n" (
  set "ARG_TWCHAT_ENABLE=0"
  exit /b 0
)

rem --- URL 種別判定（LIVE/VOD/CLIP/OTHER） ---
call :TW_PARSE_URL "!ARG_URL!"
if not defined ARG_TWCHAT_KIND (
  set "ARG_TWCHAT_ENABLE=0"
  exit /b 0
)

rem --- 保存先 ---
call :TS
set "CHATDIR=!OUTROOT!\chat"
call :ENSURE_DIR "!CHATDIR!"

set "ARG_TWCHAT_OUT_JSON=!CHATDIR!\twitch_chat_!ARG_TWCHAT_KIND!_!ARG_TWCHAT_KEY_SAFE!_!TS!.json"
set "ARG_TWCHAT_OUT_LOG=!CHATDIR!\twitch_chat_!ARG_TWCHAT_KIND!_!ARG_TWCHAT_KEY_SAFE!_!TS!.out.log"
set "ARG_TWCHAT_ERR_LOG=!CHATDIR!\twitch_chat_!ARG_TWCHAT_KIND!_!ARG_TWCHAT_KEY_SAFE!_!TS!.err.log"

rem --- ツール選択 ---
rem  - TwitchDownloaderCLI: VOD/CLIP のチャット取得に向く（URL/ID -> JSON）
rem  - chat-downloader: LIVE/VOD/CLIP のチャット取得に使える（Python）
if /i "!ARG_TWCHAT_KIND!"=="LIVE" (
  set "ARG_TWCHAT_MODE=CHATDL"
  set "ARG_TWCHAT_STOP_ON_END=1"

  call :ENSURE_CHATDOWNLOADER || (set "ARG_TWCHAT_ENABLE=0" & exit /b 0)
) else (
  if exist "!TWCLI_EXE!" (
    set "ARG_TWCHAT_MODE=TDCLI"
    set "ARG_TWCHAT_STOP_ON_END=0"
  ) else (
    set "ARG_TWCHAT_MODE=CHATDL"
    set "ARG_TWCHAT_STOP_ON_END=0"
  )
)

set "ARG_TWCHAT_ENABLE=1"

echo.
echo [Twitch Chat] kind = !ARG_TWCHAT_KIND!
echo [Twitch Chat] out  = "!ARG_TWCHAT_OUT_JSON!"
echo [Twitch Chat] mode = !ARG_TWCHAT_MODE!

exit /b 0

:TW_PARSE_URL
rem ============================================================
rem Twitch URL をざっくり分類して、kind と key を返す
rem   - https://www.twitch.tv/<name>              => LIVE (name)
rem   - https://www.twitch.tv/videos/<id>         => VOD  (id)
rem   - https://clips.twitch.tv/<slug> or ...     => CLIP (slug)
rem ============================================================
set "ARG_TWCHAT_KIND="
set "ARG_TWCHAT_KEY="
set "ARG_TWCHAT_KEY_SAFE="

set "PURL=%~1"
for /f "usebackq delims=" %%R in (`powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$u=$env:PURL;" ^
  "$kind='OTHER'; $key='';" ^
  "if ($u -match 'twitch\.tv/videos/(\d+)') { $kind='VOD';  $key=$Matches[1] }" ^
  "elseif ($u -match 'clips\.twitch\.tv/([^/?#]+)') { $kind='CLIP'; $key=$Matches[1] }" ^
  "elseif ($u -match 'twitch\.tv/([^/?#]+)') { $kind='LIVE'; $key=$Matches[1] }" ^
  "$safe = ($key -replace '[^\w\-\.]+','_');" ^
  "Write-Output ($kind+'|'+$key+'|'+$safe)"`) do (
  for /f "tokens=1-3 delims=|" %%a in ("%%R") do (
    set "ARG_TWCHAT_KIND=%%a"
    set "ARG_TWCHAT_KEY=%%b"
    set "ARG_TWCHAT_KEY_SAFE=%%c"
  )
)

exit /b 0


rem ============================================================
rem functions
rem ============================================================


:ENSURE_DENO
rem YouTube の署名 / n challenge 対策: Deno を確認し、なければ winget で導入する。
rem yt-dlp の EJS solver は Deno が PATH 上、または --js-runtimes で指定可能な場所にある必要がある。
set "DENO_EXE="
for /f "delims=" %%P in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$cmd=Get-Command deno -ErrorAction SilentlyContinue; if($cmd){$cmd.Source}"') do set "DENO_EXE=%%P"
if defined DENO_EXE exit /b 0

if exist "%BASE_DIR%deno.exe" (
  set "DENO_EXE=%BASE_DIR%deno.exe"
  exit /b 0
)

echo.
echo [INFO] Deno が見つかりません。YouTube challenge 対策のため winget で DenoLand.Deno をインストールします。
where winget >nul 2>nul
if errorlevel 1 (
  echo [WARN] winget が見つかりません。Deno を手動でインストールしてください。
  echo        https://docs.deno.com/runtime/getting_started/installation/
  exit /b 0
)

winget install -e --id DenoLand.Deno --accept-package-agreements --accept-source-agreements > "%DENO_INSTALL_LOG%" 2>&1
if errorlevel 1 (
  type "%DENO_INSTALL_LOG%"
  echo [WARN] DenoLand.Deno のインストールに失敗しました。上記ログを確認してください: "%DENO_INSTALL_LOG%"
  exit /b 0
)

for /f "delims=" %%P in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$cmd=Get-Command deno -ErrorAction SilentlyContinue; if($cmd){$cmd.Source; exit}; $candidates=@(Join-Path $env:USERPROFILE ''.deno\bin\deno.exe''); $pkg=Join-Path $env:LOCALAPPDATA ''Microsoft\WinGet\Packages''; if(Test-Path $pkg){$candidates += Get-ChildItem -Path $pkg -Filter deno.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName}; $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1"') do set "DENO_EXE=%%P"

if defined DENO_EXE (
  echo [INFO] Deno を検出しました: "!DENO_EXE!"
) else (
  echo [WARN] Deno のインストール後も現在のセッションで deno.exe を検出できませんでした。
  echo        PowerShell / cmd を開き直すか、Deno の PATH を確認してください。
)
exit /b 0

:SELECT_FFMPEG
rem ============================================================
rem ffmpeg 自動選択
rem 優先順位:
rem   1. システム PATH 上の ffmpeg.exe
rem   2. bat と同じディレクトリの ffmpeg.exe
rem   3. bat と同じディレクトリの ffmpeg\bin\ffmpeg.exe
rem ============================================================
set "FFMPEG_LOCATION="
set "FFMPEG_SOURCE="

rem --- 1) PATH 上の ffmpeg ---
for /f "delims=" %%P in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$cmd=Get-Command ffmpeg -CommandType Application -ErrorAction SilentlyContinue; if($cmd){$cmd.Source}"') do set "FFMPEG_SOURCE=%%P"
if defined FFMPEG_SOURCE (
  set "FFMPEG_LOCATION=!FFMPEG_SOURCE!"
  echo [INFO] ffmpeg: system PATH ^(!FFMPEG_SOURCE!^)
  exit /b 0
)

rem --- 2) bat と同じディレクトリの ffmpeg.exe ---
if exist "%BASE_DIR%ffmpeg.exe" (
  set "FFMPEG_SOURCE=%BASE_DIR%ffmpeg.exe"
  set "FFMPEG_LOCATION=%BASE_DIR%ffmpeg.exe"
  echo [INFO] ffmpeg: bat directory ^(!FFMPEG_SOURCE!^)
  exit /b 0
)

rem --- 3) bat と同じディレクトリの ffmpeg\bin\ffmpeg.exe ---
if exist "%BASE_DIR%ffmpeg\bin\ffmpeg.exe" (
  set "FFMPEG_SOURCE=%BASE_DIR%ffmpeg\bin\ffmpeg.exe"
  set "FFMPEG_LOCATION=%BASE_DIR%ffmpeg\bin\ffmpeg.exe"
  echo [INFO] ffmpeg: bat directory ffmpeg\bin ^(!FFMPEG_SOURCE!^)
  exit /b 0
)

echo [WARN] ffmpeg.exe が見つかりません。動画+音声の結合やメタデータ/サムネイル埋め込みに失敗する可能性があります。
exit /b 0

:CHECK_FILE
if exist "%~1" exit /b 0
echo [ERR] 必要なファイルが見つかりません: "%~1"
pause
exit /b 1

:ENSURE_DIR
if not exist "%~1" mkdir "%~1" >nul 2>&1
exit /b 0

:ENSURE_CHATDOWNLOADER
rem ============================================================
rem chat-downloader が無ければ自動インストールする
rem 前提: CHATDL_PY と CHATDL_ARGS が設定されていること
rem 例) CHATDL_PY=py, CHATDL_ARGS=-3 -m chat_downloader
rem ============================================================

rem --- import チェック（失敗したら未インストールと判断） ---
%CHATDL_PY% -c "import chat_downloader" >nul 2>&1
if not errorlevel 1 exit /b 0

rem --- py ランチャを使っている場合、-3 のような指定が必要になりがちなので
rem     CHATDL_ARGS の先頭トークンが -3 / -3.13 などならそれを利用する ---
set "PYVER="
for %%T in (%CHATDL_ARGS%) do (
  echo %%T | findstr /b /c:"-3" >nul 2>&1
  if not errorlevel 1 (
    set "PYVER=%%T"
  )
  goto :_PYVER_DONE
)
:_PYVER_DONE

echo.
echo [INFO] chat-downloader が見つかりません。インストールします...
if defined PYVER (
  %CHATDL_PY% %PYVER% -m pip install -U --user chat-downloader
) else (
  %CHATDL_PY% -m pip install -U --user chat-downloader
)

if errorlevel 1 (
  echo [ERR ] chat-downloader のインストールに失敗しました。
  echo       ネットワーク/プロキシ設定、pip、Python の環境を確認してください。
  exit /b 1
)

rem --- 再チェック ---
%CHATDL_PY% -c "import chat_downloader" >nul 2>&1
if errorlevel 1 (
  echo [ERR ] インストール後も chat_downloader を import できませんでした。
  exit /b 1
)

echo [INFO] chat-downloader の準備ができました。
exit /b 0

:TS
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%I"
exit /b 0

:LOAD_SETTINGS
call :LOAD_SETTINGS_FILE
exit /b 0

:LOAD_SETTINGS_FILE
if not exist "%SETTINGS%" (
  call :SAVE_SETTINGS
  exit /b 0
)
for /f "usebackq tokens=1,* delims==" %%A in ("%SETTINGS%") do (
  if /i "%%A"=="OUTROOT_YT"   set "OUTROOT_YT=%%B"
  if /i "%%A"=="OUTROOT_TW"   set "OUTROOT_TW=%%B"
  if /i "%%A"=="LOGROOT"      set "LOGROOT=%%B"
  if /i "%%A"=="TWCLI_EXE"    set "TWCLI_EXE=%%B"
  if /i "%%A"=="TWCHAT_EMBED" set "TWCHAT_EMBED=%%B"
  if /i "%%A"=="CHATDL_PY"    set "CHATDL_PY=%%B"
  if /i "%%A"=="CHATDL_ARGS"  set "CHATDL_ARGS=%%B"
)
exit /b 0


:SAVE_SETTINGS
(
  echo OUTROOT_YT=%OUTROOT_YT%
  echo OUTROOT_TW=%OUTROOT_TW%
  echo LOGROOT=%BASE_DIR%log
  echo TWCLI_EXE=%TWCLI_EXE%
  echo TWCHAT_EMBED=%TWCHAT_EMBED%
  echo CHATDL_PY=%CHATDL_PY%
  echo CHATDL_ARGS=%CHATDL_ARGS%
) > "%SETTINGS%"
exit /b 0



:RESET_EXTRA_ARGS
set "ARG_FMT="
set "ARG_WRITE_COMMENTS="
set "ARG_SUBLANGS_OVERRIDE="
set "ARG_BATCHFILE="
set "ARG_TWCHAT_ENABLE="
set "ARG_TWCHAT_MODE="
set "ARG_TWCHAT_KIND="
set "ARG_TWCHAT_KEY="
set "ARG_TWCHAT_KEY_SAFE="
set "ARG_TWCHAT_OUT_JSON="
set "ARG_TWCHAT_OUT_LOG="
set "ARG_TWCHAT_ERR_LOG="
set "ARG_TWCHAT_STOP_ON_END="
set "ARG_TWCHAT_CHAT_TYPE="
exit /b 0

:ASK_YT_EXTRAS
rem YouTube (PLAT=1) のみ対象。Twitch ではスキップ。
if not "!PLAT!"=="1" exit /b 0

set "ANS="
set /p "ANS=コメントをダウンロードしますか? (Y/n) [Y]: "
if /i "!ANS!"=="n" (
  set "ARG_WRITE_COMMENTS=--no-write-comments"
) else (
  set "ARG_WRITE_COMMENTS=--write-comments"
)

set "ANS="
set /p "ANS=ライブチャットをダウンロードしますか? (Y/n) [Y]: "
if /i "!ANS!"=="n" (
  rem live chat は字幕として扱われるため除外指定
  set "ARG_SUBLANGS_OVERRIDE=all,-live_chat"
) else (
  set "ARG_SUBLANGS_OVERRIDE="
)
exit /b 0

:SHOW_FORMATS
if not defined ARG_URL exit /b 0
echo.
echo ============================================================
echo Available formats (yt-dlp -F)
echo ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Set-Location -LiteralPath $env:BASE_DIR; $args=@('--ignore-config','--config-locations',$env:CFG_BASE,'--config-locations',$env:ARG_PCONF,'--config-locations',$env:ARG_OCONF); if($env:PLATNAME -eq 'YouTube'){ if($env:DENO_EXE -and $env:DENO_EXE.Trim().Length -gt 0){ $args += @('--js-runtimes',('deno:' + $env:DENO_EXE)) }; $args += @('--remote-components','ejs:github') }; if($env:FFMPEG_LOCATION){ $args += @('--ffmpeg-location',$env:FFMPEG_LOCATION) }; $args += @('-F',$env:ARG_URL); & $env:YTDLP_EXE @args"
echo ============================================================
exit /b 0

:RUN_YTDLP
set "YTDLP_LOG=!RUN_LOG!"

powershell -NoProfile -ExecutionPolicy Bypass -File "!RUNNER_PS1!"
exit /b !ERRORLEVEL!
