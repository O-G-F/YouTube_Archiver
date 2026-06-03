# YouTube Local Archiver

YouTube の動画・メタデータ・字幕・コメントを `yt-dlp` でローカル保存するアーカイブシステム。
本リポジトリは要件定義書の **Phase 0（設計・土台）** と **Phase 1（URL登録・単体ダウンロード）** の実装です。

- Backend: **FastAPI**（Web API）
- Worker: **RQ**（Redis ベースのジョブキュー）
- DB: **PostgreSQL**（本番 / Docker）/ **SQLite**（ローカル CLI・テスト）
- CLI: **Typer**
- ダウンロード: **yt-dlp**（subprocess 実行。設計方針 4.2）
- 同梱ツール: **ffmpeg / ffprobe / Deno**（Docker イメージ内）

> Phase 2 以降（チャンネルクロールの本実装、YouTube Data API / Takeout、Discord Bot、AI 日記、
> YouTube 風プレイヤー）は未実装です。本 README 末尾の[ロードマップ](#ロードマップ)を参照してください。

---

## 実装済み機能（Phase 0–2B）

- URL 登録 → 種別判定（動画 / 再生リスト / チャンネル / タブ）→ ジョブ化
- 7 種の組み込みダウンロードプロファイル（最高画質 mkv / 1080p / proxy mp4 / flac / opus / metadata / comments）+ 全字幕版
- `yt-dlp` を **subprocess** 実行し、**実行コマンド・stdout・stderr を分離して保存**
- ダウンロード結果（動画・音声・サムネ・info.json・説明文・字幕・リンク）を DB に**相対パス**で登録
- **ダウンロードジョブ**と**メタデータ更新ジョブの分離** — コメント更新時に動画本体を再取得しない（要件 4.3 / 5.5）
- **再生リスト / チャンネル展開（Phase 2A）** — flat extraction → `collections`/`collection_items` 差分検出 → 新規のみ子 download ジョブ投入
- **再クロール・scheduler（Phase 2B）** — `crawl_policy`（manual/new_only/refresh）、`removed_at` 検出、scheduler 常駐、チャンネルルート URL のタブ自動展開、レート制御・429 retryable、DB ユニーク制約
- 字幕は安全な許可リスト（`ja,en`、`all` は明示時のみ）、YouTube JS チャレンジ対応（`--remote-components ejs:github` + deno）、`curl_cffi` 同梱
- 失敗ジョブ管理（ステータス・エラーメッセージ・再実行・キャンセル）+ `partial_success` / retryable
- 運用補強（Phase 1.5）: `doctor` 診断、ジョブログ API/CLI（path traversal 対策）、profile dry-run
- Web API（登録 / 展開 / コレクション / 再クロール / scheduler / ジョブ / プロファイル / 動画 / 診断）と CLI の両方
- Docker Compose（web / worker / scheduler / postgres / redis / migrate）
- Alembic マイグレーション（0001 初期 + 0002 jobs.meta + 0003 unique制約）+ pytest（123 tests）

---

## クイックスタート（Docker Compose）

```bash
cp .env.example .env
# .env を編集（特に *_HOST_PATH をNAS/SSDの実パスに、必要なら DATABASE_URL/REDIS_URL）

docker compose up -d --build
# migrate サービスがスキーマ適用 + プロファイル投入を行ってから web/worker が起動します
```

- Web UI / API ドキュメント: <http://localhost:8000/docs>
- ヘルスチェック: <http://localhost:8000/api/health>

URL を登録してみる:

```bash
curl -X POST localhost:8000/api/archive/url \
  -H 'content-type: application/json' \
  -d '{"url":"https://youtu.be/dQw4w9WgXcQ","profile":"video_compressed_1080p"}'
```

コンテナ内 CLI:

```bash
docker compose exec web archiver jobs list
docker compose exec web archiver profiles list
```

### NAS / 外付け SSD への保存

保存先はコンテナ固定ではなく**ホスト側のパスをマウント**します（要件 3）。`.env` の `*_HOST_PATH` を変更するだけで移行できます。DB には `ARCHIVE_ROOT` からの**相対パス**が保存されるため、保存ルートを移しても `ARCHIVE_ROOT`（ホスト側マウント）を差し替えるだけで復元できます。

```env
# .env（例）
ARCHIVE_HOST_PATH=/Volume1/NAS/youtube-archiver/archive
LOG_HOST_PATH=/Volume1/NAS/youtube-archiver/logs
# 外付けSSDなら例: /mnt/external/youtube-archiver/archive
```

---

## クイックスタート（ローカル / Docker なし）

PostgreSQL / Redis なしで、SQLite + インライン実行だけでも動きます。

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# 環境変数（最低限）。SQLite を使うので DATABASE_URL は sqlite:// にします
export ARCHIVE_ROOT=./data/archive
export LOG_ROOT=./data/logs
export CONFIG_ROOT=./data/config
export DATABASE_URL="sqlite:///./data/archiver.sqlite3"

archiver init                                   # スキーマ作成 + プロファイル投入
archiver download enqueue "https://youtu.be/dQw4w9WgXcQ" --profile metadata_only --now
archiver jobs list
```

- `--now` を付けると **Redis 不要**でその場で実行します（ローカル確認に便利）。
- Redis を使う場合は別ターミナルで `archiver worker` を起動し、`--now` なしで `enqueue` します。
- Web サーバは `archiver server`（= `uvicorn app.main:app`）。

---

## ダウンロードプロファイル（要件 7）

| プロファイル | 種別 | 概要 | 既定字幕 |
|---|---|---|---|
| `video_best_archive` | video | 最高画質 `bestvideo+bestaudio/best`、mkv、コメント/ライブチャット/サムネ/info.json を保存（長期保存用） | `ARCHIVE_SUB_LANGS`（既定: 限定） |
| `video_best_archive_all_subs` | video | `video_best_archive` と同じだが**全字幕言語**（`--sub-langs all`）を取得（重く 429 を誘発しやすい） | `all` |
| `video_compressed_1080p` | video | **既定**。1080p 以下、可能なら mp4 互換、Web 再生向け | `DEFAULT_SUB_LANGS`（限定） |
| `video_proxy_1080p_mp4` | video | ブラウザ再生用 proxy（H.264/AAC mp4、本体アーカイブは置換しない） | `DEFAULT_SUB_LANGS`（限定） |
| `audio_flac_best` | audio | `bestaudio` → flac、サムネ埋め込み | （字幕なし） |
| `audio_opus_save_space` | audio | `bestaudio` → opus（可能なら再エンコード回避） | （字幕なし） |
| `metadata_only` | metadata | 本体なし。info.json / 説明文 / 字幕 / サムネのみ | `DEFAULT_SUB_LANGS`（限定） |
| `comments_refresh_only` | metadata | 本体なし。コメント更新専用（`metadata_refresh` ジョブが使用） | （字幕なし） |

プロファイルは DB（`download_profiles`）に投入され、Web/CLI から参照されます。`metadata_flags`（extras の真偽）と `ytdlp_args`（フォーマット/画質/コンテナ）に分離して保持します。

---

## 字幕言語と YouTube JS チャレンジ（429 対策）

**`--sub-langs all` は既定では使いません。** `all` は YouTube が大量の自動翻訳字幕（数百言語）を返すため `HTTP 429 (Too Many Requests)` を誘発し、ジョブが失敗します。代わりに設定可能な許可リストを既定にしています（要件定義の「共通オプション `--sub-langs all`」はこの理由で既定から除外）。

| 設定（env / `archiver.yaml`） | 既定値 | 用途 |
|---|---|---|
| `DEFAULT_SUB_LANGS` | `ja,en` | 通常プロファイルの字幕言語。**完全一致コードで指定**（後述） |
| `ARCHIVE_SUB_LANGS` | `ja,en` | `video_best_archive` 用。**完全保存したい場合のみ** `all` を設定 |
| `YTDLP_REMOTE_COMPONENTS` | `ejs:github` | YouTube の署名 / n チャレンジ解決に必須。空でも既定で有効、`none` で無効化 |
| `DENO_PATH` | `/usr/local/bin/deno` | チャレンジ解決スクリプトを実行する JS ランタイム |

> **`en.*` のような正規表現は使わないでください。** yt-dlp は sub-langs をアンカー付き正規表現として扱うため、`en.*` は英語由来の**自動翻訳字幕**（`en-de-DE` / `en-fr` / `en-en` …）を数百件マッチしてしまい、`all` と同様に 429 を誘発します。`ja,en` のように**完全一致コード**を列挙してください（必要なら `ja,en,en-US,en-orig`）。

- 全字幕が必要なときは **`video_best_archive_all_subs` プロファイル** か **`ARCHIVE_SUB_LANGS=all`** を明示指定してください。
- YouTube 向けには `--remote-components ejs:github` と `--js-runtimes deno:<path>` が**常に同時に**付与されます（要件 6/7）。これがないと `Signature solving failed` / `n challenge solving failed` の警告とスロットリングが発生します。`YTDLP_REMOTE_COMPONENTS` を空にしても（古い `.env` 対策で）既定の `ejs:github` が適用され、無効化は `none` を明示したときだけです。

### partial_success（部分成功）

字幕 1 言語の 429 など一部だけ失敗しても、info.json / 説明文 / 字幕などが保存できていれば、ジョブ全体を `failed` にせず **`partial_success`** にします（取得済みファイルは DB 登録済み、`error_message` に警告を保持）。`partial_success` ジョブは `retry` で再実行できます。

---

## yt-dlp 引数の組み立て（旧 conf の継承）

旧運用の `base → platform → overlay` 構成（設計 4.1）をそのまま継承し、次の順で argv を構築します（`app/services/profiles.py`）:

```
共通ランタイム引数  →  YouTube オーバーレイ  →  プロファイル  →  ジョブ/コンテキスト引数
(--no-abort-on-error,   (--write-subs,           (-f, --merge-      (-o 出力, --download-archive,
 --retries 5,            --sponsorblock-mark,      output-format,     --no-playlist, --cookies,
 --concurrent-fragments  限定 --sub-langs)          --extract-audio    --ffmpeg-location, --js-runtimes
 10, --write-info-json,                            ...)               deno:..., --remote-components ejs:github)
 --windows-filenames …)
```

- 引数は**必ずリストで** subprocess に渡し、シェル文字列化しません（URL のシェルインジェクション回避）。
- `--ignore-config` を常時付与し、システムの yt-dlp 設定に影響されない再現可能な実行にします。
- 実行コマンドは `command.txt` に記録（再実行用）。`--password` 等の機密値はマスク、`--cookies` の**パス**は再実行のため保持（中身はログに出ません。要件 12）。

---

## ジョブの種類と「本体再取得の分離」

| type | 内容 | download-archive | skip-download |
|---|---|---|---|
| `download` | 単体動画（常に `--no-playlist`） | あり（重複回避） | なし |
| `expand` | 再生リスト/チャンネルを子の `download` へ展開 | — | — |
| `metadata_refresh` | info.json + コメントのみ更新 | **なし（`--no-download-archive`）** | **あり** |

`metadata_refresh` は `--skip-download` かつ download-archive を使わないため、**保存済み動画の本体を再ダウンロードせずコメントだけ更新**できます（要件 4.3 / 5.5）。取得した info.json は元ファイルを上書きせず `metadata_snapshots/<video_id>/<UTC>/` に保存し、`metadata_snapshots` テーブルに記録します。

---

## Phase 2A: 再生リスト・チャンネル展開（expand）

再生リスト/チャンネル URL を **flat extraction**（`yt-dlp --flat-playlist --dump-single-json --skip-download`、本体は落とさない）で動画 ID 一覧へ展開し、`collections` / `collection_items` に保存して、**新規動画のみ**子 `download` ジョブを作成します。

```bash
# 小さな再生リストを metadata_only で検証（まずはこれ）
docker compose exec web archiver source add-playlist "https://www.youtube.com/playlist?list=XXXX" \
  --profile metadata_only --max-items 5 --now

# チャンネルのタブ別登録（タブごとに expand job）
docker compose exec web archiver source add-channel "https://www.youtube.com/@handle" \
  --videos --shorts --streams --profile metadata_only --max-items 20

# 任意の playlist/channel URL を展開
docker compose exec web archiver source expand "https://www.youtube.com/@handle/videos" --profile metadata_only

# 結果確認
docker compose exec web archiver jobs show 1          # meta: discovered/created/skipped/removed/capped
docker compose exec web archiver collections list
docker compose exec web archiver collections items 1
```

### 差分検出（diff）

- 同じ URL を再展開すると **同一 collection** を再利用します（URL 正規化で突き合わせ）。
- 既存 item は `last_seen_at` を更新。今回見つからなかった item は `removed_at` を設定（**ただし `--max-items` で打ち切った回は誤検出を避けるため removed を付けません**）。
- 同一 collection 内で **同じ video_id を重複登録しません**。
- 既に**有効な download ジョブ**（queued/running/success/partial_success）がある動画には**重複ジョブを作りません**（`skipped_existing_count`）。失敗ジョブのみの動画は再投入されます。

### EXPAND_MAX_ITEMS（暴走防止）

- env `EXPAND_MAX_ITEMS`（既定 `0` = 無制限）。正の整数なら、その件数で**抽出を打ち切り**ます（`yt-dlp -I 1:N`）。
- ジョブ単位の一時上書きは CLI/API の `--max-items` / `max_items`（`jobs.meta.max_items` に保存）。
- **注意**: 大規模チャンネル（数千〜数万本）を `EXPAND_MAX_ITEMS=0` で展開すると、その数だけ子 download ジョブが作られます。初回は必ず小さな playlist + `metadata_only` + `--max-items` で挙動を確認してください。

### expand の結果とログ

- 件数は `jobs.meta`（`discovered_count` / `created_jobs_count` / `skipped_existing_count` / `removed_count` / `capped` / `collection_id`）に記録され、`GET /api/jobs/{id}` と `archiver jobs show` で確認できます。
- flat extraction の **`command.txt` / `yt-dlp.stdout.log`（JSON）/ `yt-dlp.stderr.log`** は通常の download ジョブと同じ場所（`LOG_ROOT/jobs/<id>/`）に保存され、失敗時は `archiver jobs logs <id> --stderr` や `GET /api/jobs/{id}/logs/stderr` で追えます。
- YouTube 抽出でも `--remote-components ejs:github` と `--js-runtimes deno:<path>` を付与します。`--sub-langs all` は使いません。

> チャンネル**ルート**URL（`/@handle`）はタブ一覧を返すことがあり、動画 ID が 0 件になる場合があります。確実に動画を取るには `/videos` `/shorts` `/streams` のタブ URL を使ってください（`add-channel` は自動でタブ URL を生成します）。

---

## Phase 2B: 再クロール・scheduler・レート制御

### チャンネルルート URL の自動展開

`add-channel` はルート URL（`/@handle` や `/channel/UC...`）を受け取り、**指定したタブだけ**を `/videos` `/shorts` `/streams` の URL に展開して expand job を作ります。**フラグなしのルート URL は誤爆防止のためエラー**になります（タブ URL を直接渡した場合はそのタブだけ実行）。

```bash
docker compose exec web archiver source add-channel "https://www.youtube.com/@handle" \
  --videos --shorts --streams --profile metadata_only --max-items 3 --now
docker compose exec web archiver source add-channel "https://www.youtube.com/@handle" --videos --max-items 3 --now
# フラグなしのルート URL はエラー:
docker compose exec web archiver source add-channel "https://www.youtube.com/@handle"   # -> error
```

### crawl_policy と再クロール

各 collection は `crawl_policy` を持ちます（既定 `new_only`）。

| policy | 動作 |
|---|---|
| `manual` | scheduler はスキップ。手動 refresh のみ |
| `new_only` | 新規動画のみ子 download job を作成（**removed 検出なし**） |
| `refresh` | last_seen_at 更新 + **removed_at 検出**も行う |

```bash
docker compose exec web archiver collections set-policy 1 refresh
docker compose exec web archiver collections refresh 1 --now           # 手動再クロール（removed検出あり）
docker compose exec web archiver collections refresh-all --now         # enabled全件（各policy尊重）
docker compose exec web archiver collections enable 1     # / disable 1
```

API: `POST /api/collections/{id}/refresh`、`POST /api/collections/refresh-all`、`PATCH /api/collections/{id}`（`enabled`/`crawl_policy`/`profile`）。

### scheduler サービス

`scheduler` コンテナは常駐し、`SCHEDULER_INTERVAL_SECONDS` ごとに enabled かつ非 manual の collection へ expand job を投入します。**`SCHEDULER_ENABLED=false`（既定）の間は各パスが no-op**（ループ自体は回ります）。有効化は `.env`:

```env
SCHEDULER_ENABLED=true
SCHEDULER_INTERVAL_SECONDS=3600
```

- 手動 1 パス: `archiver scheduler run-once`（`SCHEDULER_ENABLED` に関係なく実行）/ `POST /api/scheduler/run-once`。
- 状態: `GET /api/scheduler/status`。
- scheduler のログは `LOG_ROOT/scheduler/scheduler.log` に追記されます。
- scheduler が作った job は `job.meta.scheduled_by`（`scheduler` / `manual` / `manual_refresh` 等）で識別できます。

### removed_at の意味と capped

- `removed_at` は「**前回までは在ったが今回の再クロールで見つからなかった**」項目に付きます（削除/非公開/限定公開などの目安）。再発見時は `removed_at` を解除し `last_seen_at` を更新します。
- **`capped=true`（`EXPAND_MAX_ITEMS` や `--max-items` で抽出を打ち切った回）では removed_at を更新しません。** 一覧の途中までしか見ていないため、見えなかった = 削除とは限らないからです。

### レート制御・429・partial_success

連続投入で YouTube の HTTP 429 を誘発しにくくするための設定（`.env`）:

| 設定 | 既定 | 効果 |
|---|---|---|
| `DOWNLOAD_JOB_DELAY_SECONDS` | `0` | 各 download job 開始前に待機（1ワーカーでも間隔を空ける） |
| `YTDLP_RETRY_BACKOFF_SECONDS` | `0` | yt-dlp `--retry-sleep`（429 等のリトライ間隔） |
| `MAX_CONCURRENT_DOWNLOAD_JOBS` | `1` | 想定同時数（実体は `docker compose up --scale worker=N` で調整） |

- 子 download job が一部だけ取得できた場合（例: 1 言語の字幕が 429）は **`failed` ではなく `partial_success`**（取得済みは DB 登録済み・`jobs retry` 可）。
- 出力ゼロの 429 は `failed` にしつつ **`job.meta.retryable=true` / `reason=http_429`** を記録します。`archiver jobs retry <id>` や次回の再クロールで再試行されます（failed は「有効ジョブ」に数えないため再クロールで再投入対象になります）。
- `curl_cffi` を同梱しており、yt-dlp のブラウザ impersonation により impersonation 警告と一部 403/429 を低減します。

### 大量チャンネル登録時の注意

- **必ず小さく始める**: 初回は `--max-items 3` + `metadata_only` で挙動確認。
- `EXPAND_MAX_ITEMS=0`（無制限）で巨大チャンネルを `refresh` policy + scheduler 有効にすると、毎周期で大量の子 job が生成され得ます。`EXPAND_MAX_ITEMS` と `DOWNLOAD_JOB_DELAY_SECONDS` を併用してください。
- `collection_items` には `(collection_id, youtube_video_id)` の **DB ユニーク制約**があり、コードレベルに加え DB 側でも重複を防ぎます（migration `0003`）。

---

## ストレージ構成

```
ARCHIVE_ROOT/
  youtube/
    videos/<channel_id>/<video_id>/<title> [<id>].{mkv,mp4,info.json,description,jpg,*.vtt,*.live_chat.json}
    audio/<channel_id>/<video_id>/<title> [<id>].{flac,opus,...}
    metadata_snapshots/<video_id>/<UTC>/<id>.info.json
    archive/history.txt            # --download-archive（本体の重複回避）
LOG_ROOT/
  jobs/<job_id>/{command.txt, yt-dlp.stdout.log, yt-dlp.stderr.log}
```

DB の `media_files.path` / `subtitles.path` / `metadata_snapshots.path` は **`ARCHIVE_ROOT` からの相対パス**、`jobs.log_path` は **`LOG_ROOT` からの相対パス**です。

---

## CLI リファレンス

```bash
archiver init                       # スキーマ作成(SQLite) + プロファイル投入
archiver server [--host --port]     # Web サーバ起動
archiver worker                     # RQ ワーカー起動

archiver profiles list

archiver download enqueue URL [--profile NAME] [--now] [--priority N]
archiver download run [--limit N]   # キュー済みジョブをインライン実行(Redis不要)

archiver source add-url URL [--profile NAME] [--now]
archiver source add-playlist URL [--profile NAME] [--now] [--max-items N]
archiver source add-channel URL [--videos] [--shorts] [--streams] [--profile NAME] [--now] [--max-items N]
archiver source expand URL [--profile NAME] [--now] [--max-items N]   # playlist/channel 展開

archiver collections list
archiver collections show COLLECTION_ID
archiver collections items COLLECTION_ID [--include-removed] [--limit N]

archiver comments refresh --video-id VIDEO_ID [--profile NAME] [--now]
archiver comments refresh-all [--limit N] [--now]

archiver jobs list [--status S] [--type T] [--limit N]
archiver jobs show JOB_ID                         # 詳細（status/ログパス/動画/profile/出力先）
archiver jobs logs JOB_ID [--command|--stdout|--stderr] [--tail N]
archiver jobs retry JOB_ID [--now]
archiver jobs cancel JOB_ID

archiver profiles command PROFILE URL             # dry-run: 実行せずコマンドだけ表示
archiver doctor                                   # 環境診断（書込/ツール/DB/Redis）

# --- Phase 2B: 再クロール / scheduler ---
archiver collections refresh COLLECTION_ID [--now] [--max-items N]   # 1件再クロール（removed検出あり）
archiver collections refresh-all [--now] [--max-items N]            # enabled全件（policy尊重）
archiver collections enable COLLECTION_ID
archiver collections disable COLLECTION_ID
archiver collections set-policy COLLECTION_ID new_only|refresh|manual
archiver scheduler run-once [--max-items N]        # 1パスだけ実行（SCHEDULER_ENABLED無関係）
archiver scheduler run                             # ループ常駐（scheduler コンテナが使用）
```

> macOS でローカルに `archiver worker` を使うと fork 由来の問題が出る場合があります。ローカル確認では `--now` / `download run` のインライン実行を推奨します。Docker（Linux）では worker が正常動作します。

---

## Web API リファレンス

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/health` | 稼働状況（DB / Redis / yt-dlp バージョン） |
| GET | `/api/profiles` | プロファイル一覧 |
| POST | `/api/archive/url` | URL を 1 件登録（`{"url","profile","priority"}`） |
| POST | `/api/archive/current-tab` | 同上（ブラウザ拡張/ブックマークレット用、要件 5.1.4） |
| POST | `/api/archive/batch` | URL を複数登録（`{"urls":[...],"profile"}`） |
| POST | `/api/archive/expand` | playlist/channel を展開（`{"url","profile","max_items"}`） |
| POST | `/api/sources/playlist` | 再生リスト登録（`{"url","profile","max_items"}`） |
| POST | `/api/sources/channel` | チャンネル登録（`{"url","videos","shorts","streams","profile","max_items"}` → タブ毎に expand job） |
| GET | `/api/collections` | コレクション一覧（item_count 付き） |
| GET | `/api/collections/{id}` | コレクション詳細 |
| GET | `/api/collections/{id}/items` | アイテム一覧（`?include_removed=&limit=&offset=`） |
| POST | `/api/collections/{id}/refresh` | 1件再クロール（`?max_items=`） |
| POST | `/api/collections/refresh-all` | enabled 全件を再クロール |
| POST | `/api/collections/{id}/enable` ・ `/disable` | scheduler 対象の有効/無効 |
| PATCH | `/api/collections/{id}` | `{"enabled","crawl_policy","profile"}` を更新 |
| GET | `/api/scheduler/status` | scheduler 設定と対象数 |
| POST | `/api/scheduler/run-once` | 手動で1パス実行 |
| GET | `/api/doctor` | 環境診断（書込可否 / ツール版 / DB / Redis） |
| GET | `/api/jobs` | ジョブ一覧（`?status=&type=&limit=&offset=`） |
| GET | `/api/jobs/{id}` | ジョブ詳細（status/error/ログパス/出力先/動画/profile） |
| GET | `/api/jobs/{id}/logs` | command/stdout/stderr をまとめて取得（`?tail=N`） |
| GET | `/api/jobs/{id}/logs/{stdout\|stderr\|command}` | 単一ログを生テキストで取得（`?tail=N`） |
| GET | `/api/jobs/{id}/log` | （後方互換）末尾のみの JSON |
| POST | `/api/jobs/{id}/retry` | 失敗/キャンセル/部分成功ジョブの再実行 |
| POST | `/api/jobs/{id}/cancel` | ジョブのキャンセル |
| POST | `/api/profiles/{name}/build-command` | dry-run（`{"url"}`）。cookie/secret はマスク |
| GET | `/api/videos` | 保存済み動画一覧（`?q=&limit=&offset=`） |
| GET | `/api/videos/{id}` | 動画詳細（メディアファイル/字幕数/コメント数） |

OpenAPI は `/docs`（Swagger UI）/ `/redoc`。

ログ API は **`LOG_ROOT/jobs/<id>/` 配下の 3 ファイルのみ**を読み、解決後パスが `LOG_ROOT` 内にあることを検証します（path traversal 対策）。それ以外は 404。

---

## 運用・デバッグ手順（Phase 1.5）

### metadata_only の動作確認

```bash
docker compose exec web archiver download enqueue "https://youtu.be/dQw4w9WgXcQ" --profile metadata_only --now
docker compose exec web archiver jobs list          # status が success（429解消後）
```

### ログ確認

```bash
# CLI
docker compose exec web archiver jobs show 1
docker compose exec web archiver jobs logs 1 --command
docker compose exec web archiver jobs logs 1 --stderr --tail 100
# 生ファイル（コンテナ内 LOG_ROOT=/logs）
docker compose exec web sh -lc "cat /logs/jobs/1/command.txt"
docker compose exec web sh -lc "sed -n '1,200p' /logs/jobs/1/yt-dlp.stderr.log"
# API
curl -s localhost:8000/api/jobs/1/logs/stderr
curl -s localhost:8000/api/jobs/1 | jq '{status,error_message,stdout_log_path,output_dir,profile:.profile.name}'
```

### コマンドの事前確認（dry-run）

```bash
docker compose exec web archiver profiles command metadata_only "https://youtu.be/dQw4w9WgXcQ"
curl -s -XPOST localhost:8000/api/profiles/video_compressed_1080p/build-command \
  -H 'content-type: application/json' -d '{"url":"https://youtu.be/dQw4w9WgXcQ"}'
# 期待: --sub-langs ja,en と --remote-components ejs:github を含み、--sub-langs all を含まない
```

### 環境診断 / リトライ

```bash
docker compose exec web archiver doctor            # 書込可否 / yt-dlp,ffmpeg,deno / DB / Redis
curl -s localhost:8000/api/doctor | jq
docker compose exec web archiver jobs retry 1       # failed/partial_success を再実行
```

---

## トラブルシューティング（Mac / Docker Desktop）

実機検証で実際に踏んだ問題と対処です。

- **`docker compose` が見つからない / プラグインが効かない**
  Compose プラグインは `~/.docker/cli-plugins/` にあります。`DOCKER_CONFIG` を差し替えるとここが見えなくなるため、後述の回避策ではプラグインと context をシンボリックリンクで持ち込みます。
- **`error getting credentials - err: exit status 1`（Docker Desktop / OrbStack / Homebrew docker CLI の混在）**
  資格情報ヘルパ（`docker-credential-desktop`）が壊れていると、**公開イメージの pull/metadata 取得**でも失敗します。`~/.docker/config.json` を触らずに回避するには、`credsStore` を持たない一時 `DOCKER_CONFIG` を使います（`.docker-config-local/` は gitignore 済み）:
  ```bash
  mkdir -p .docker-config-local
  ln -sf ~/.docker/contexts     .docker-config-local/contexts
  ln -sf ~/.docker/cli-plugins  .docker-config-local/cli-plugins
  printf '{"auths":{},"currentContext":"desktop-linux"}' > .docker-config-local/config.json
  DOCKER_CONFIG=$PWD/.docker-config-local docker compose build --no-cache
  DOCKER_CONFIG=$PWD/.docker-config-local docker compose up -d
  ```
  ベースイメージがキャッシュ済みなら通常は再現しません（pull が走らないため）。
- **ビルドは成功するのに実行時 `ImportError`（pip ファイルが 0 バイト）**
  Docker Desktop の VM ディスク枯渇 / overlayfs 破損が原因のことがあります（`docker system df` が `bad message` を返す等）。対処: ホストの空き容量を確保 →『`docker builder prune -af`』→ 破損イメージ削除 → `docker compose build --no-cache`。Dockerfile には**依存 import 検証 RUN** を入れてあり、壊れたビルドはその場で失敗します。
- **`down -v` してもメディア/ログが残る**
  `./data/archive` と `./data/logs` は **バインドマウント**なので `down -v`（named volume 削除）では消えません。クリーンに確認したい場合は `rm -rf ./data/archive ./data/logs` を実行してください（以前の `--sub-langs all` 実行で落ちた自動翻訳 `.vtt` の残骸対策）。
- **`--remote-components` が付かない / 字幕が大量に落ちる**
  既存 `.env` が古い可能性があります。`YTDLP_REMOTE_COMPONENTS=`（空）でもコードが `ejs:github` に補正し、旧 `SUB_LANGS=all` は参照されません（新 `DEFAULT_SUB_LANGS` 未設定→既定 `ja,en`）。確実にしたい場合は `.env` を `.env.example` に合わせて更新してください。

---

## DB とマイグレーション

要件 8 の全 13 テーブルを定義しています（`app/models.py`）:
`sources, collections, collection_items, videos, media_files, subtitles, comments,
live_chat_messages, metadata_snapshots, download_profiles, jobs, watch_history_events, diary_entries`。

- PostgreSQL: **Alembic** で管理（`alembic upgrade head`）。Docker の `migrate` サービスが自動実行。
  - `0001` 初期スキーマ（全13テーブル）
  - `0002` `jobs.meta`（expand の入力 `max_items` と結果カウント・scheduler タグを保持する JSON 列）
  - `0003` `collection_items` に `(collection_id, youtube_video_id)` のユニーク制約（重複防止。既存重複は migration 内で安全に dedup）
- SQLite（ローカル/テスト）: `archiver init` がモデルから直接スキーマを作成。
- 型は PostgreSQL / SQLite 双方で動くポータブルな SQLAlchemy 型のみ使用（`BigInteger` / `JSON` 等）。`0003` は SQLite 互換のため `batch_alter_table` を使用。

マイグレーション生成（モデル変更時）:

```bash
export DATABASE_URL="postgresql+psycopg2://archiver:archiver@localhost:5432/archiver"
alembic revision --autogenerate -m "your change"
alembic upgrade head
```

---

## テスト

```bash
pip install -e ".[dev]"
pytest -q
```

SQLite + 一時ディレクトリのみで完結し、PostgreSQL / Redis は不要です。
URL 正規化・プロファイル選択・引数組み立て・ジョブ作成・DB 保存・ログ保存・API を検証します（要件 15）。

---

## セキュリティ（要件 12）

- `cookies.txt` / `.env` / `secrets/` は **`.gitignore` 済み**。リポジトリに含めないこと。
- cookies は Docker の `./secrets:/secrets:ro` マウント経由で参照（`COOKIES_FILE`）。DB へ平文保存しません。
- ログに cookie / token の**値**を出力しません（`--password` 等はマスク）。
- Web UI は LAN 内利用前提。外部公開時は前段に認証を必ず置くこと。

---

## ロードマップ（Phase 2 以降・未実装）

| Phase | 内容 |
|---|---|
| 2 | チャンネル `/videos /shorts /streams` 差分クロールの本実装、新規検出キュー |
| 3 | YouTube Data API（OAuth・高評価/再生リスト同期）、Google Takeout インポート |
| 4 | コメント/ライブチャット更新スケジューラ（`all_videos_adaptive`）、削除検出 |
| 5 | Discord Bot（自分専用 DM・user_id allowlist） |
| 6 | AI 日記生成（字幕要約 → 日次集約 → Obsidian 互換 Markdown 出力） |
| 7 | YouTube 風ローカルプレイヤー（再生 / コメント / ライブチャット同期表示） |

> 現状でも `metadata_refresh` ジョブと `comments_refresh_only` プロファイルにより、コメントの手動更新は可能です（Phase 4 のスケジューラは未実装）。

---

## 旧 conf 資産

`yt-dlp_old_configs/`（Shift-JIS）は移行元の参考として残してあります。`base/youtube/twitch + overlay` の思想は
`app/services/profiles.py` に取り込み済みです（bat 依存は廃止）。
