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

## 実装済み機能（Phase 0–4A）

- URL 登録 → 種別判定（動画 / 再生リスト / チャンネル / タブ）→ ジョブ化
- 7 種の組み込みダウンロードプロファイル（最高画質 mkv / 1080p / proxy mp4 / flac / opus / metadata / comments）+ 全字幕版
- `yt-dlp` を **subprocess** 実行し、**実行コマンド・stdout・stderr を分離して保存**
- ダウンロード結果（動画・音声・サムネ・info.json・説明文・字幕・リンク）を DB に**相対パス**で登録
- **ダウンロードジョブ**と**メタデータ更新ジョブの分離** — コメント更新時に動画本体を再取得しない（要件 4.3 / 5.5）
- **再生リスト / チャンネル展開（Phase 2A）** — flat extraction → `collections`/`collection_items` 差分検出 → 新規のみ子 download ジョブ投入
- **再クロール・scheduler（Phase 2B）** — `crawl_policy`（manual/new_only/refresh）、`removed_at` 検出、scheduler 常駐、チャンネルルート URL のタブ自動展開、レート制御・429 retryable、DB ユニーク制約
- **Google Takeout インポート（Phase 3A/3B）** — ZIP を内容ベース判定（多言語対応）、視聴履歴 / 検索履歴 / 登録チャンネル / 再生リストを正規化保存（`watch_history_events` / `search_history_events` / `collections`(channel/takeout_playlist) / `collection_items` / Video stub）、重複統合、`import-all`、subscriptions enqueue、preview / dry-run / limit、path traversal・zip slip 対策
- **コメント / メタデータ更新（Phase 4A）** — 本体を再DLせず info.json・コメント更新、`comments` 正規化保存と新規/更新/消失/再発見の差分、adaptive refresh policy（土台）、metadata_snapshots（checksum 付き）、429/コメント無効/削除の分類、`COMMENT_REFRESH_MAX_COMMENTS`
- **scheduler 連携コメント定期更新・live chat 取得（Phase 4B）** — scheduler が `next_comments_refresh_at` の期限切れ動画へ自動でコメント更新ジョブを投入（`SCHEDULER_COMMENTS_ENABLED`、1パス上限 `SCHEDULER_COMMENTS_LIMIT_PER_RUN`、frozen/recent 除外）、429 で `comment_refresh_failures` 加算＋backoff 再スケジュール、`live_chat_refresh` ジョブ（本体・コメント再DLなし、`--write-subs --sub-langs live_chat`）で `live_chat_messages` を正規化保存（super chat/メンバー/差分対応）、非ライブ動画は `not_available` 扱いでエラーにしない
- **管理用 Web UI（Phase 5A）** — React + Vite + TypeScript の管理コンソールを FastAPI が同一オリジンで配信（`/`）。Dashboard / Jobs / Job 詳細（ログ tab・secret マスク）/ Videos / Video 詳細（簡易プレイヤー・comments/live chat・snapshots・refresh ボタン）/ Collections / Collection 詳細 / Add(URL・expand・channel) / Takeout / Settings・Doctor。CLI/curl なしで登録・refresh・ログ確認が可能。secret/cookie/token は UI・ログに出さない
- **視聴 UI / プレイヤー・検索（Phase 5B）** — 保存済み動画を**ブラウザでシーク再生**（HTTP Range 対応の `media` 配信）。YouTube 風 Video 詳細（プレイヤー＋タイトル＋チャンネル＋説明折りたたみ＋コメント親子表示＋ライブチャット super chat/メンバー区別＋同 channel/同 collection の関連動画）。横断検索（`/api/search`：動画/コメント/ライブチャット/コレクション）、Videos のチャンネル/状態フィルタ・並び替え・サムネ・ページング、Job の 429/partial_success 分類表示、Library（liked/history/subscriptions/playlists の将来分類）。body 未保存（metadata_only）は「未保存」と明示
- **高評価リスト（liked videos）/ ライブラリ・DL 安定化（Phase 6A）** — Google Takeout の「高く評価した動画」を `liked_videos` に正規化保存（CSV/JSON/HTML・言語差異対応、video stub 連携、dedup、`raw_json` 既定非返却）。Library で実 count 表示＋専用画面（メタデータ未取得を明示、`metadata_only` enqueue で本体を保存せず後追い取得）。検索に `liked_video` 追加。download ジョブの stderr 分類を強化（429 / Incomplete data received / fragments / subtitles / impersonation）して `job.meta.classification` に保存し UI で原因・retryable を表示
- **Hybrid Liked Videos Sync（Phase 6B）** — 高評価の**全履歴は Google Takeout「マイ アクティビティ」**から取得（YouTube Data API は実用上 ~5000 件で頭打ちのため）。Takeout ZIP の種別自動判定（`youtube_takeout` / `my_activity_takeout` / `takeout_index` / `unknown`）+ discover/inspect、My Activity の `高く評価しました` / `Liked` 抽出（`低く評価`/`高評価を削除`/`を視聴` は除外）、**source 区別**（`takeout_my_activity` / `takeout_youtube` / `youtube_data_api`）で同一 DB へ統合（youtube_video_id でクロス source dedup）。**逐次更新は YouTube Data API（OAuth・差分・既存到達で停止）**で、API quota/auth エラーも分類表示。Hybrid `library bootstrap`。**OAuth 既定無効でも全機能が安全に起動**（secret/token は非表示）
- **実 DL 安定化・retry/backoff・字幕再取得（Phase 7A）** — 全ジョブ種別が `job.meta.classification`（429/incomplete_data/fragments/subtitles/comments/live_chat/impersonation/quota/auth/token、`reasons[]`/`retryable`/`partial`/`summary`）を**永続保存**。**再試行可能な失敗だけ抽出**（`GET /api/jobs/retryable`、`retry-all --reason`）、download に**指数バックオフ retry**（`next_retry_at`・回数上限で無限ループ防止、scheduler 自動再投入は任意）。**字幕だけ再取得**する `subtitles_refresh` ジョブ（本体非DL）。`partial_success` を failed と明確に区別。cookies / browser cookies / PO-token は configured yes/no のみ表示（値は UI/API/log に出さない）
- 字幕は安全な許可リスト（`ja,en`、`all` は明示時のみ）、YouTube JS チャレンジ対応（`--remote-components ejs:github` + deno）、`curl_cffi` 同梱
- 失敗ジョブ管理（ステータス・エラーメッセージ・再実行・キャンセル）+ `partial_success` / retryable
- 運用補強（Phase 1.5）: `doctor` 診断、ジョブログ API/CLI（path traversal 対策）、profile dry-run
- Web API（登録 / 展開 / コレクション / 再クロール / scheduler / ジョブ / プロファイル / 動画 / 診断）と CLI の両方
- Docker Compose（web / worker / scheduler / postgres / redis / migrate）
- Alembic マイグレーション（0001 初期 〜 0009 job retry fields）+ pytest（247 tests）+ フロント Vitest（18 tests）

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
| `live_chat_refresh_only` | metadata | 本体・コメントなし。ライブチャット専用（`--write-subs --sub-langs live_chat`、`live_chat_refresh` ジョブが使用） | `live_chat` |

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

## Phase 3A: Google Takeout インポート

Google Takeout の YouTube データ（**視聴履歴・検索履歴・登録チャンネル・再生リスト**）を ZIP から読み取り、まず**視聴履歴を `watch_history_events` に正規化保存**します。OAuth / YouTube Data API は次フェーズ（3B）です。

### ZIP の配置

ZIP は **`TAKEOUT_IMPORT_ROOT`（コンテナ内 `/takeout_imports`、ホスト側 `TAKEOUT_HOST_PATH`、既定 `./data/takeout`）配下**に置きます。Web upload は未対応で、配置済み ZIP をパス指定で読みます。**`/takeout_imports` 配下以外のパスは拒否**（path traversal 対策）、ZIP は**メモリ上で読み取り**（ディスク展開なし・zip slip ガード）。

```bash
# ホスト側にコピー（Docker の場合）
cp ~/Downloads/takeout-XXXX.zip ./data/takeout/

docker compose exec web archiver takeout list-files "/takeout_imports/takeout-XXXX.zip"
docker compose exec web archiver takeout preview   "/takeout_imports/takeout-XXXX.zip"
# まずは小さく
docker compose exec web archiver takeout import    "/takeout_imports/takeout-XXXX.zip" --limit 10
docker compose exec web archiver watch-history stats
docker compose exec web archiver watch-history list --limit 20
```

API: `POST /api/takeout/preview`・`POST /api/takeout/import`（`{"path","limit","dry_run"}`）・`GET /api/watch-history`・`GET /api/watch-history/stats`。

### 仕様

- **ファイル判定は内容ベース**（言語差異に強い）。`watch-history.json` のような英語名と `検索履歴.json` のような日本語名が混在しても判定可能。Takeout ZIP のファイル名は cp437→UTF-8 で復元します。
- **形式**: JSON を主とし、HTML 視聴履歴も best-effort で対応。CSV（登録チャンネル/再生リスト/高評価）は preview 件数のみ。
- **video_id** は `titleUrl` から抽出。取れない場合も title / raw_json を保持。
- **重複防止**: `source + youtube_video_id + watched_at`（video_id が無い場合は `source + title + watched_at`）。DB にも `(source, youtube_video_id, watched_at)` ユニーク制約（migration `0004`）。再 import は skip されます。
- `--dry-run` は DB に書き込みません。`--limit N` は走査件数の上限。
- import は `type=takeout_import` の job として `jobs` に記録され、件数が `job.meta` に入ります（`jobs show`）。

### プライバシー（重要）

- **視聴履歴・検索履歴・`raw_json` は個人情報**です。`watch_history_events.raw_json` は API では既定で返しません（`include_raw=true` 指定時のみ）。
- import ログには件数のみを出し、過剰な個人情報は出力しません。
- Takeout ZIP は **`.gitignore` 済み**（`*.zip` / `takeout-*.zip` / `data/`）。Git にコミットしないでください。
- これらのデータは後続の **AI 視聴日記生成・高評価同期・再生リスト登録**の基盤として利用予定です。

---

## Phase 3B: 残りの Takeout データ正規化

Phase 3A の視聴履歴に加えて、**検索履歴 / 登録チャンネル / 再生リスト**を DB へ正規化します。OAuth/API なしで、Takeout だけから初期 DB を構築できます。

```bash
Z="/takeout_imports/takeout.zip"
# まずは小さく（各 limit）
docker compose exec web archiver takeout import-all "$Z" \
  --limit-watch 10 --limit-search 10 --limit-subscriptions 5 --limit-playlists 2 --limit-items 3
# 個別にも
docker compose exec web archiver takeout import-subscriptions "$Z" --limit 5
docker compose exec web archiver takeout import-playlists "$Z" --limit-playlists 2 --limit-items 3
docker compose exec web archiver takeout playlists "$Z"           # 一覧 preview
docker compose exec web archiver search-history stats
docker compose exec web archiver subscriptions list
```

### マッピング

| Takeout | 保存先 | 備考 |
|---|---|---|
| 検索履歴 (`検索履歴.json`) | `search_history_events`（新規テーブル） | `query` / `searched_at` / `raw_json`。dedup `(source, query, searched_at)` |
| 登録チャンネル (`登録チャンネル.csv`) | `collections`（`type=channel`, `enabled=false`, `crawl_policy=manual`） + 1つの `sources`（`type=channel_subscription`） | `youtube_channel_id` / `title` / `url`。dedup は channel_id |
| 再生リスト (`再生リスト.csv` + 各 `〇〇 の動画.csv`） | `collections`（`type=takeout_playlist`） + `collection_items` + `videos`（stub） | item は `video_id`/`position`/追加日時。stub Video を作成しリンク |

- **既存の YouTube playlist expand collection（`type=playlist`、url=正規URL）とは衝突しません**。Takeout 由来は `type=takeout_playlist`、url=`takeout:playlist:<id|title>` という別名前空間です。
- 登録チャンネルは `type=channel` の **disabled** collection として入るため、scheduler では自動クロールされません（`subscriptions enqueue` で明示的にタブ展開）。

### 登録チャンネルのクロール（enqueue）

`subscriptions enqueue` は登録チャンネルを Phase 2A/2B の **channel expand** に流します（直接 download はしません）。タブ（`--videos/--shorts/--streams`）の指定が必須で、**`--limit` で対象チャンネル数を制限**できます（大量チャンネル対策）。

```bash
docker compose exec web archiver subscriptions enqueue --videos --profile metadata_only --max-items 3 --limit 2 --now
```

### import-all と job 記録

`import-all` は watch → search → subscriptions → playlists を順に実行し、各 import を `type=takeout_import`（`meta.kind` で区別）の job として記録します。`jobs show <id>` で件数を確認できます。

### プライバシー（重要）

- **検索履歴は特に個人的**です。`search_history_events.raw_json` は API 既定で返しません（`include_raw=true` のみ）。`watch_history_events` も同様。
- import ログは件数のみで、検索語・視聴タイトルを大量出力しません。
- preview の samples は各種**最大5件**に制限。
- Takeout ZIP は `.gitignore` 済み。**OAuth / YouTube Data API 連携は Phase 3C 以降**です。

---

## Phase 4A: コメント / メタデータ更新（本体を再DLしない）

保存済み/登録済み動画に対し、**動画本体を再ダウンロードせず** info.json・コメントを更新します。`comments_refresh` ジョブは `comments_refresh_only` プロファイル（`--skip-download --write-info-json --write-comments --no-download-archive`）を使い、専用の一時スナップショットディレクトリに出力します（**元の `video.info.json` は上書きしません**）。

```bash
# 保存済みの小さな動画でまず確認（COMMENT_REFRESH_MAX_COMMENTS=20 など小さく）
docker compose exec web archiver comments refresh "dQw4w9WgXcQ" --now
docker compose exec web archiver comments stats "dQw4w9WgXcQ"
docker compose exec web archiver comments list "dQw4w9WgXcQ" --limit 20
docker compose exec web archiver comments snapshots "dQw4w9WgXcQ"
# adaptive policy で「更新すべき動画」を抽出して一括
docker compose exec web archiver comments refresh-all --limit-videos 5 --now
```

API: `POST /api/comments/refresh`（公式は `{"target":"<id|url>"}`。後方互換で `{"video":"<id|url>"}` も可。両方同時指定は 400）、`POST /api/videos/{id}/comments/refresh`、`GET /api/videos/{id}/comments`・`/stats`、`POST /api/comments/refresh-all`。

```bash
# 公式（target）
curl -s -XPOST localhost:8000/api/comments/refresh -H 'content-type: application/json' -d '{"target":"dQw4w9WgXcQ"}'
# 後方互換（video）も動作
curl -s -XPOST localhost:8000/api/comments/refresh -H 'content-type: application/json' -d '{"video":"dQw4w9WgXcQ"}'
```

### コメントの差分検出

`comments` テーブル（`(video_id, comment_id)` ユニーク）へ正規化保存し、毎回の取得集合と既存を比較します。

- **新規** → 追加（`inserted_count`）
- **text/like_count 変化** → 更新（`updated_count`、`updated_at` を更新）
- **今回見つからなかった既存コメント** → `is_deleted_or_missing=true`（`marked_missing_count`）
- **再発見** → `is_deleted_or_missing=false`（`refound_count`）

> **capped（取得上限に達した）回は missing を付けません。** 上限で打ち切ったときに「見えなかった＝削除」と誤判定しないためです（コメント無効と判定された回も同様にスキップ）。

### COMMENT_REFRESH_MAX_COMMENTS

`COMMENT_REFRESH_MAX_COMMENTS`（既定 `200`、`0`=無制限）。yt-dlp の `--extractor-args youtube:max_comments=N` で**取得量を制御**します。バイラル動画は数十万コメントになり得るため、初回は小さい値（例 20）で確認してください。

### adaptive refresh policy（土台）

`videos` に `last_comments_refresh_at` / `next_comments_refresh_at` / `comments_state` を持ち、`comments refresh-all` が「更新すべき動画」を抽出します（`app/services/comment_policy.py`、scheduler から呼べる関数として分離）。

| 動画の経過日数 | 次回更新間隔 |
|---|---|
| 〜7日 | 毎日 |
| 〜30日 | 3日ごと |
| 〜180日 | 週1 |
| 180日超 | 月1 |
| `comments_disabled` / `unavailable` / `frozen` | 更新停止（frozen） |

### 429 / コメント無効 / 削除・非公開

- **HTTP 429**: 即 failed にせず、取得済み snapshot/コメントがあれば `partial_success`、無ければ failed だが `job.meta.retryable=true`（`reason=http_429`）。`jobs retry` や次回 refresh-all で再試行されます。
- **コメント無効**: `comments_state=comments_disabled` を記録し frozen（自動更新対象外）。
- **削除/非公開/メンバー限定**: `comments_state=unavailable`。
- 判定は yt-dlp stderr の内容ベース（`comment_policy.classify_comment_state`）。

### metadata_snapshots

取得した info.json は `ARCHIVE_ROOT/youtube/metadata_snapshots/<video_id>/<UTC>/` に保存し、`metadata_snapshots`（`snapshot_type=comments_refresh|metadata_refresh`、`checksum`(sha256)、相対 `path`）に記録。`GET /api/videos/{id}/snapshots` / `archiver comments snapshots VIDEO_ID` で一覧。

### job.meta / ログ

`comments_refresh` ジョブも `command.txt` / `yt-dlp.stdout.log` / `yt-dlp.stderr.log` を保存し、`job.meta` に `target_video_id` / `fetched_comments_count` / `inserted_count` / `updated_count` / `marked_missing_count` / `refound_count` / `snapshot_id` / `capped` / `comments_state` を記録（`jobs show <id>` で確認）。

### プライバシー

- コメントは author 名・チャンネル ID・本文を含む**個人情報**です。`comments.raw_json` は API 既定で返しません（`include_raw=true` のみ）。
- import/refresh ログにコメント本文を大量出力しません（件数のみ）。

---

## Phase 4B: scheduler 連携コメント定期更新 / live chat 取得

### scheduler によるコメント定期更新

`scheduler` は 1 パスごとに **collection 再クロール**（`SCHEDULER_ENABLED`）と **コメント定期更新**（`SCHEDULER_COMMENTS_ENABLED`）を独立トグルで実行します。コメント更新では `next_comments_refresh_at <= now` の動画を `comment_policy.select_due_videos` で抽出し（`comments_disabled` / `unavailable` / `frozen` は除外、未更新を優先）、1 パス最大 `SCHEDULER_COMMENTS_LIMIT_PER_RUN` 件の `comments_refresh` ジョブを投入します。

```bash
# .env
SCHEDULER_ENABLED=true            # collection 再クロール
SCHEDULER_COMMENTS_ENABLED=true   # コメント定期更新
SCHEDULER_COMMENTS_LIMIT_PER_RUN=10
```

手動 1 パス（`SCHEDULER_ENABLED` に関係なく実行、対象を選択可能）:

```bash
archiver scheduler run-once --all            # collections + comments + liked passes
archiver scheduler run-once --comments        # コメントのみ
archiver scheduler run-once --collections     # 再クロールのみ
archiver scheduler run-once --liked-metadata  # liked metadata（本体非保存）【Phase 7D】
archiver scheduler run-once --liked-archive    # liked body archive（本体DL・少量）【Phase 7D】
archiver scheduler run-once --liked-retry      # liked retryable 再queue（backoff後）【Phase 7D】
archiver liked-videos progress                 # 進捗集計【Phase 7D】
archiver liked-videos progress --history       # progress 時系列【Phase 7E】
archiver queue status                          # キュー在庫（type/source_action別）【Phase 7D】
archiver scheduler runs --limit 20             # 実行履歴一覧【Phase 7E】
archiver scheduler runs show RUN_ID            # run 詳細 + jobs【Phase 7E】
archiver scheduler stats                       # 実行集計【Phase 7E】
archiver scheduler recommend-settings          # 安全寄り推奨値（自動変更なし）【Phase 7E】
archiver scheduler recommend-settings --env    # .env 貼り付け用 / --json【Phase 7F】
archiver scheduler runs cleanup --keep-last 50 --dry-run   # 既定 dry-run（ジョブ非削除）【Phase 7F】
archiver scheduler runs cleanup --keep-last 50 --apply     # 実削除【Phase 7F】
archiver liked-videos progress --history       # progress 時系列（グラフは UI）【Phase 7E/7F】
# API:
curl -s -XPOST localhost:8000/api/scheduler/run-once -H 'content-type: application/json' \
  -d '{"collections":true,"comments":true}'
```

`run-once` の結果は `collections_checked` / `collection_jobs_created` / `due_comment_videos_checked` / `comments_jobs_created` / `skipped_frozen` / `skipped_recent` / `submitted` / `job_ids` を返します。scheduler 投入ジョブの `job.meta` には `scheduled_by=scheduler_comments` / `due_reason`(`due`|`never_refreshed`) / `previous_next_comments_refresh_at` が入ります。

期限切れ動画の確認・手動スケジュール:

```bash
archiver comments due --limit 20                 # 期限切れ一覧（GET /api/comments/due）
archiver comments schedule VIDEO_ID              # policy で next を再計算
archiver comments schedule VIDEO_ID --now-due    # 即時 due 扱いに
archiver comments refresh-all --all --limit-videos 5 --now   # frozen 以外を全件（--due-only が既定）
```

### 429（レート制限）リトライ

`comments_refresh` が HTTP 429 を受けると、ジョブは `failed` だが `job.meta.rate_limited=true` / `retryable=true` を記録し、`video.comment_refresh_failures` を加算、`next_comments_refresh_at = now + COMMENTS_REFRESH_RETRY_BACKOFF_SECONDS` に後ろ倒しします（scheduler が後で再投入）。連続失敗が `COMMENTS_REFRESH_MAX_RETRY` 以上になると backoff を最低 1 日に延長。連続実行間隔は `COMMENTS_REFRESH_JOB_DELAY_SECONDS` で空けられます。

### live_chat_refresh（本体・コメントを再DLしない）

`live_chat_refresh` ジョブは `live_chat_refresh_only` プロファイル（`--skip-download --write-info-json --write-subs --sub-langs live_chat`）で **動画本体もコメントも取得せず**、yt-dlp が出力する `<id>.live_chat.json`（JSONL）のみを取得・解析します。テキスト / super chat / super sticker / メンバーシップの各 renderer を解析し、`live_chat_messages` に正規化保存（`message_id` で重複排除、新規/更新/消失/再発見の差分、`LIVE_CHAT_MAX_MESSAGES` で上限）。

```bash
archiver live-chat refresh "dQw4w9WgXcQ" --now       # video id か URL
archiver live-chat list VIDEO_ID [--superchats-only]
archiver live-chat stats VIDEO_ID
archiver live-chat refresh-all --limit-videos 25 --now   # has_live_chat/is_live かつ期限切れ
# API: POST /api/live-chat/refresh（{"target":...}、互換 {"video":...}、両方は400）、
#      POST /api/videos/{id}/live-chat/refresh、POST /api/live-chat/refresh-all、
#      GET  /api/videos/{id}/live-chat（既定 raw_json 非返却）・/stats
```

`videos` は `last_live_chat_refresh_at` / `next_live_chat_refresh_at` / `live_chat_state`（`available` / `not_available` / `unavailable` / `frozen`） / `has_live_chat` を保持します。**ライブチャットの無い通常動画はエラーにならず `not_available`** として記録され、`unavailable`/`frozen` は再取得対象から外れます（`LIVE_CHAT_REFRESH_INTERVAL_SECONDS` ごとに再取得）。取得した `.live_chat.json` は `metadata_snapshots`（`snapshot_type=live_chat_refresh`、`checksum`、相対 `path`）に記録。`job.meta` に `target_video_id` / `fetched_messages_count` / `inserted_count` / `updated_count` / `marked_missing_count` / `refound_count` / `snapshot_id` / `live_chat_state` / `capped` / `rate_limited` を記録。

### プライバシー（live chat）

- live chat も author 名・チャンネル ID・本文を含む**個人情報**です。`live_chat_messages.raw_json` と author 情報は API 既定で返しません（`include_raw=true` のみ raw を返却）。
- ログにメッセージ本文を大量出力しません（件数のみ）。

---

## Phase 5A: 管理用 Web UI

バックエンドの状態を**ブラウザから確認・操作**できる管理コンソールです（React + Vite + TypeScript）。**本格的な YouTube 風プレイヤー・認証・OAuth は未実装**で、まずは運用・確認用途に絞っています。

### 構成

- フロントエンドは `frontend/`（Vite + React + TS）。本番ビルド（`frontend/dist`）を **FastAPI が同一オリジンで配信**します（`app/main.py`）。SPA の deep link（例 `/jobs/5`）は history-API フォールバックで `index.html` を返します。
- API はすべて `/api/*`。UI は**相対パス**で叩くため、開発（Vite proxy）と Docker（同一オリジン）の両方でそのまま動きます。別オリジンの API を使う場合のみ `VITE_API_BASE` を設定。
- CORS は `CORS_ALLOW_ORIGINS`（既定 `*`。API は認証/cookie を使わないローカル管理ツール）。

### Docker での起動（UI 同梱）

`Dockerfile` は**マルチステージ**で、Node ステージが UI をビルド → Python イメージへ `frontend/dist` をコピーします。通常どおり起動するだけで UI も配信されます。

```bash
docker compose build
docker compose up -d
# ブラウザで開く:
open http://localhost:8000/            # 管理 UI（Dashboard）
# API ドキュメントは引き続き http://localhost:8000/docs
```

### 開発時の起動（ホットリロード）

バックエンド（`uvicorn` or `docker compose up web`）を 8000 で起動した状態で:

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173 （/api は 8000 へ proxy）
# 別オリジンの API を使う場合: VITE_PROXY_TARGET=http://host:8000 npm run dev
npm run build        # 本番ビルド -> dist/（FastAPI が配信）
npm run typecheck    # tsc --noEmit
npm test             # vitest
```

### 画面一覧

| パス | 画面 | 主な内容 / 操作 |
|---|---|---|
| `/` | Dashboard | health（DB/Redis/yt-dlp）・各種件数・job status 集計・最新ジョブ・scheduler 状態 / **doctor 実行**・**scheduler run-once** |
| `/jobs` | Jobs | 一覧（status/type フィルタ）・**自動更新（6秒）**・**retry**・詳細リンク |
| `/jobs/:id` | Job 詳細 | 基本情報・`job.meta` 整形・**command/stdout/stderr ログ tab（tail・secret マスク）**・retry・関連 video/collection |
| `/videos` | Videos | 一覧（検索 / comments_state / live_chat_state / body 有無）・状態バッジ・body 数 |
| `/videos/:id` | Video 詳細 | metadata・**簡易プレイヤー（body があれば再生、なければ「未保存」）**・media files・comments stats/list・live chat stats/list・snapshots・**comments/live chat refresh ボタン**・関連 jobs/collections |
| `/collections` | Collections | 一覧・**enable/disable**・**set-policy**・**refresh**・詳細リンク |
| `/collections/:id` | Collection 詳細 | 基本情報・items（`removed_at` 表示・video へ遷移）・**max_items 指定 refresh** |
| `/archive` | Add / Archive | **単体 URL 登録**・**expand**（playlist/channel・max_items）・**add-channel**（videos/shorts/streams・profile・max_items） |
| `/takeout` | Takeout | `TAKEOUT_IMPORT_ROOT` の ZIP 一覧・**preview**・**import-all（dry-run / limit）**。個人データは件数のみ表示 |
| `/settings` | Settings / Doctor | doctor 結果・profiles 一覧・scheduler 状態・**非 secret 設定値**（cookie/token/DB 認証情報は非表示・マスク） |

### セキュリティ

- **secret/cookie/token を UI・ログに出さない。** ジョブログは API 取得時に `services.logs.mask_secrets` で `--cookies <path>` / `Authorization` / `password|token|api_key=…` / 設定済み cookie ファイルパスを `***REDACTED***` にマスク（コマンドは生成時にも redact 済みの二重防御）。
- **設定画面**は cookie ファイルパスを表示せず `cookies_configured: yes/no` のみ。`DATABASE_URL` / `REDIS_URL` はパスワードをマスク。
- **Takeout / comments / live chat の個人情報**を不用意に全面表示しない（件数・サマリ中心。`raw_json`/author は既定で非返却）。
- 簡易プレイヤーの配信は **DB に登録された media file のみ**（`GET /api/videos/{id}/media/{media_file_id}`、`ARCHIVE_ROOT` 配下に強制・パス入力なし）。
- ログ表示は React により HTML エスケープ。既存 API の path traversal 対策は維持。
- **UI と API は分離**（API は `/api/*`、UI は静的配信）しており、将来の認証導入を阻害しません。現状は**認証なし**なので信頼できるネットワークで運用してください。

### 未実装（今後）

- ユーザー認証 / RBAC、YouTube Data API OAuth（Phase 6A 以降）。

---

## Phase 5B: 視聴 UI / プレイヤー・検索

Phase 5A の管理 UI を壊さずに、**保存済み動画の視聴体験**と**検索/ライブラリ**を追加しています。

### 動画再生（HTTP Range 対応）

`GET /api/videos/{id}/media/{media_file_id}` が **HTTP Range** に対応し（Starlette の `FileResponse`）、HTML `<video>` でのシーク再生ができます。

- 全件取得: `200` + `Accept-Ranges: bytes` + `Content-Type`（拡張子から判定：mp4/webm/mkv/m4a/opus…）+ `Content-Length`
- 範囲取得: `Range: bytes=start-end` → `206 Partial Content` + `Content-Range: bytes start-end/total`
- 配信は **DB 登録済み MediaFile のみ**（パスはユーザー入力ではなく DB 由来）。`ARCHIVE_ROOT` 配下に強制し、traversal は不可。
- 本体（video/audio）が無い動画（`metadata_only` のみ等）は Video 詳細で **「未保存」** と表示し、再生 UI は出しません。

```bash
# Range が効くことの確認（保存済み動画 + その video media file id）
curl -s -D - -o /dev/null -H "Range: bytes=0-1023" \
  "http://localhost:8000/api/videos/<VIDEO_ID>/media/<MEDIA_FILE_ID>"
# -> HTTP/1.1 206 Partial Content / Content-Range: bytes 0-1023/<total> / Accept-Ranges: bytes
```

### 保存済み動画 vs metadata_only

- `video_compressed_1080p` / `video_best_archive` 等の**動画保存プロファイル**だけが本体（video/audio）を保存します。Videos 一覧の **body** 列・Video 詳細のプレイヤーはこの**本体ファイル数**を見ます（info.json/サムネ等のメタファイルは body に数えません）。
- `metadata_only` / `comments_refresh` / `live_chat_refresh` は**本体を保存しません**（body=0、「未保存」）。

### YouTube 風 Video 詳細

メインプレイヤー＋タイトル＋チャンネル＋アップロード日/長さ＋**説明の折りたたみ**＋タブ（Comments / Live chat / Details）。

- **Comments**: author / text / like_count / published_at、top-level と reply の**親子表示**（取得済みの範囲で）、長文は折りたたみ、`raw_json` は既定非表示。
- **Live chat**: timestamp / author / message / `amount_text` / `message_type`、**super chat / membership を色分け**、`not_available` を明示、`raw_json`/author 詳細は既定非表示。
- **関連動画**（サイドバー）: 同 channel・同 collection（将来 watch history / liked / playlists を足せる構造）。
- Details: メタデータ・media files（再生リンク）・snapshots・関連 jobs/collections。
- コメント/ライブチャットの **refresh ボタン**でジョブ作成。

### 検索 / Videos 強化 / Library

- **検索** `GET /api/search?q=&types=&limit=` — 動画(title/channel/id) / コメント(text) / ライブチャット(text) / コレクション(title) を ILIKE 横断。`type` 別に結果を返し、**`raw_json` は返さない**（短い snippet のみ）。UI は `/search`。
- **Videos** `/videos` — 検索 + **channel フィルタ**（`/api/videos/channels`）+ comments_state/live_chat_state/body フィルタ + 並び替え（recently added / oldest / newest upload / title）+ **サムネ表示** + ページング。
- **Library** `/library`（`/api/library/summary`）— liked videos / watch history / search history / subscriptions / playlists の**将来分類**。liked videos は未同期（`available=false`）で、**Google Takeout と YouTube Data API の両方**を後続フェーズ（6A+）で検討します。

### Job UI（429 / partial_success）

ジョブ応答に**分類**（`classification`）を付与：`rate_limited`（meta or stderr の `HTTP Error 429`）/ `partial`（`partial_success`）/ `retryable` / `warnings`（impersonation 等の任意依存不足は**低重要度**）。Jobs 一覧に `429` / `partial` バッジ、Job 詳細に分かりやすい説明（「字幕取得の一時的なレート制限。少し待って Retry」）を表示。

### セキュリティ（5B）

- media 配信は **DB 登録済みファイルのみ**・`ARCHIVE_ROOT` 配下強制（Range でも範囲外/別パスは読めない）。
- comments / live chat / 検索 snippet は React で HTML エスケープ。`raw_json` は既定非返却。secret/cookie/token は UI・ログに出さない（5A の `mask_secrets` 継続）。

---

## Phase 6A: 高評価リスト / Takeout ライブラリ・DL 安定化

### なぜ Takeout を使うのか

高評価リスト（liked videos）・視聴履歴・検索履歴・登録チャンネル・再生リストは、**YouTube Data API だけでは取得が制限/不安定**な場合があり（quota・privacy スコープ・廃止項目）、**Google Takeout のエクスポートが最も確実**です。Phase 6A は **Takeout 中心**で実データ化し、API 同期（OAuth）は Phase 6B 以降に統合する設計です。

### liked videos のインポート

Takeout ZIP 内の「高く評価した動画」プレイリスト（`Liked videos.csv` / `高く評価した動画.csv` 等、CSV/JSON/HTML・言語差異に対応）を検出し、`liked_videos` に正規化保存します。

```bash
# preview（件数・サンプル）→ import
archiver takeout preview takeout.zip                 # likes_count / liked_samples
archiver takeout import-liked-videos takeout.zip --limit 100 [--dry-run]
archiver takeout import-all takeout.zip --limit-liked 100   # 他セクションと一括
# 一覧 / 統計 / メタデータ後追い取得（本体は保存しない）
archiver liked-videos list [--only-missing-metadata]
archiver liked-videos stats
archiver liked-videos enqueue-metadata --limit 20 --profile metadata_only [--missing-only|--all] [--now]
# Phase 7C: 一括アーカイブ（plan → metadata → body archive を少量ずつ）
archiver liked-videos plan-archive [--profile video_compressed_1080p] [--source ..] [--channel ..]
archiver liked-videos enqueue-archive --limit 1 --profile video_compressed_1080p --missing-body-only --dry-run
archiver liked-videos enqueue-archive --limit 1 --profile video_compressed_1080p --now   # 本体 DL（少量）
archiver liked-videos retryable [--reason rate_limited]
archiver liked-videos retry-failed --limit 20 [--reason rate_limited] [--now]
```

- **video stub 連携**: `youtube_video_id` がある liked entry は `videos` に stub を作成/統合（既存があれば紐付け）。`title`/`channel` は取得できた範囲で補完し、後から `metadata_only` で詳細を埋められます。
- **dedup**: `(source, youtube_video_id)`（id が無い HTML 由来は `(source, title, url)`）。再インポートは skip。
- **プライバシー**: `liked_videos.raw_json` と高評価履歴は**個人情報**。API は既定で `raw_json` を返しません（`include_raw=true` のみ）。

### Library 画面

`/library` は liked videos / watch history / search history / subscriptions / playlists の**実 count** を表示（liked は `/liked-videos` 専用画面へ）。`/liked-videos` では**メタデータ未取得**の動画を明示し、行ごと/一括で `metadata_only` ジョブを投入できます（**本体は保存しません** = body 数は増えません）。検索（`/api/search`）に `liked_video` タイプを追加。

### download 安定化（429 / Incomplete data received / partial_success）

実際の動画 DL は環境により YouTube 側の**スロットリング**（`Incomplete data received`）や **HTTP 429** で不安定になります。これらは**アプリ不具合ではなく既知の外部制限**として扱い、運用しやすいよう **stderr を分類**して `job.meta.classification` に保存し、UI で原因と `retryable` を表示します。

- 分類カテゴリ: `rate_limited`(429) / `incomplete_data` / `fragments_failed` / `subtitles_failed` / `impersonation`（impersonation・subtitles は**低重要度**）。
- **`partial_success`** は failed と区別して表示（例: 本体・info は取得できたが字幕だけ 429）。「後で字幕だけ再取得」は後続検討（Job は `retryable`）。
- リトライ/バックオフ設定: `YTDLP_RETRY_BACKOFF_SECONDS`（yt-dlp `--retry-sleep`）/ `DOWNLOAD_JOB_DELAY_SECONDS`（ジョブ間スリープ）/ `COMMENTS_REFRESH_RETRY_BACKOFF_SECONDS`（429 後の再スケジュール）。
- **後続フェーズの検討対象**: `cookies` / PO-token / `--remote-components`(ejs:github) / `curl_cffi` impersonation の整備で 429・incomplete data の低減（Phase 6B 以降）。

### セキュリティ（6A）

- liked videos / watch / search 履歴は個人情報として扱い、`raw_json` は既定非返却・UI 非表示。`metadata_only` enqueue は**本体を保存しない**挙動を維持。Takeout ZIP / cookies は Git 管理しない（`.gitignore` / `.dockerignore`）。

---

## Phase 6B: Hybrid Liked Videos Sync

### 高評価動画を取得する現実的な構成

| 用途 | 手段 | 件数 |
|---|---|---|
| **初回（全履歴）** | **Google Takeout「マイ アクティビティ」**（`Takeout/マイ アクティビティ/YouTube/マイアクティビティ.json`） | 過去すべて（実例: **11,066 件**） |
| 補完 | YouTube / YouTube Music Takeout（"Liked videos" プレイリスト CSV） | 通常 0〜直近のみ |
| **逐次更新** | **YouTube Data API（OAuth）** | **実用上 ~5000 件で頭打ち**（過去全件は保証しない） |

> **重要**: YouTube Data API 単体では過去全件を取得できません（実用上 ~5000 件）。それより古い高評価は **My Activity Takeout** から取り込みます。`API だけ / Takeout だけ / API+Takeout` のどれでも動作します。

### Takeout の取得方法と種別

- **My Activity Takeout**: [takeout.google.com](https://takeout.google.com) で「マイ アクティビティ」→ YouTube を選択（JSON 推奨）。これに `高く評価しました …` イベントが入ります。
- **YouTube Takeout**: 「YouTube と YouTube Music」を選択（watch/search/subscriptions/playlists）。liked は通常 0。
- **`archive_browser.html` だけの ZIP は「目次」**であり実データではありません（種別 `takeout_index`）。UI/CLI で明示されます。

種別自動判定（`youtube_takeout` / `my_activity_takeout` / `takeout_index` / `unknown_takeout`）:

```bash
archiver takeout discover            # /takeout_imports 配下を一覧分類
archiver takeout inspect PATH        # 1 ZIP の種別・検出パス
# API: GET /api/takeout/discover, GET /api/takeout/inspect?path=, GET /api/takeout/files（kind 付き）
```

### liked import（My Activity 対応）

`import-liked-videos` は ZIP 種別を自動判定し、My Activity JSON の `高く評価しました`/`Liked` を抽出（`低く評価`/`低評価`/`高評価を削除`/`unliked`/`removed like`/`を視聴` は除外。markers は `services/takeout.py` の `LIKE_ACTIVITY_MARKERS` / `NON_LIKE_ACTIVITY_MARKERS`）。`subtitles[0].name`→channel_title、`subtitles[0].url`→channel_id、`title` の `高く評価しました ` prefix を除去。

```bash
archiver takeout import-liked-videos MyActivityZIP --limit 10000 [--dry-run]
archiver liked-videos stats / list [--only-missing-metadata]
# 結果: scanned / imported / skipped_duplicate / failed / videos_created / source_kind / detected_path（job.meta にも保存）
```

- **source 区別**: `takeout_my_activity` / `takeout_youtube` / `youtube_data_api`。Library 画面で source 別 count を表示。
- **dedup は youtube_video_id でクロス source**（複数 export/source で重複する動画は 1 行）。
- 各 liked に **Video stub** を作成/統合し、`metadata_only` で後追い詳細取得（**本体は保存しない**）。

### Hybrid 初回 DB 構築

```bash
archiver library bootstrap \
  --youtube-takeout YouTubeTakeout.zip \
  --myactivity-takeout MyActivity.zip \
  --limit-liked 20000 [--use-api] [--dry-run]
# API: POST /api/library/bootstrap
```

### YouTube Data API（差分更新・OAuth）

**既定無効**。設定すると差分同期（newest-first で**既存 DB に到達したら停止**）が使えます。取得経路は A: `videos.list(myRating=like)` / B: `channels.list → relatedPlaylists.likes → playlistItems.list`（`YOUTUBE_API_LIKED_METHOD=videos|playlist|auto`）。1 ページ最大 50 件。

```bash
# 1) Google Cloud で OAuth クライアント（インストール済みアプリ）を作成し client_secret.json を /secrets か /config に置く
#    .env: YOUTUBE_API_ENABLED=true / YOUTUBE_OAUTH_CLIENT_SECRET_FILE=/secrets/client_secret.json
archiver youtube-api status            # enabled / configured 等（パス・token は非表示）
archiver youtube-api authorize         # ブラウザで認可 → token を保存（/config 既定）
archiver youtube-api sync-liked --limit 1000 --stop-on-existing [--dry-run]
# API: GET /api/youtube-api/status, POST /api/youtube-api/sync-liked（未設定でも 200 + ok=false + classification）
```

- source=`youtube_data_api`。Takeout 由来と **youtube_video_id で dedup**。API で取れた title/channel/publishedAt は Video stub に反映。API で取れない古い liked は **My Activity 由来を残す**。
- API quota/auth エラーも分類（`auth_required` / `quota_exceeded` / `forbidden` / `token_expired` / `rate_limited`）して UI/CLI に表示。
- **OAuth 未設定でも全機能が安全に起動**します（依存ライブラリ未導入でも分類エラーで graceful degrade）。

### 参考プロジェクト

`YouTube-Liked_Videos.zip`（旧・高評価収集 WebUI）は **参考資料**です（My Activity parser・markers・API 差分更新・viewer の検索/絞り込み/サムネ設計）。**移植はせず**、本プロジェクトに不要ファイル（`.git/` 等）を混ぜていません。

### セキュリティ（6B）

- 高評価履歴・`raw_json` は個人情報として既定非返却・UI 非表示。**OAuth secret/token は UI/API/log に出さない**（Settings は `youtube_api_configured: yes/no` のみ、パス非表示）。secret/token/Takeout ZIP は Git 管理しない。`metadata_only` の本体非保存を維持。

---

## Phase 7A: 実 DL 安定化 / retry・backoff / 字幕再取得

### 失敗の意味（429 / Incomplete data received / partial_success）

YouTube 側の事情で本体 DL・字幕・コメント取得は不安定になります。**アプリ不具合ではなく外部制限**として運用しやすいよう、全ジョブが `job.meta.classification` を**永続保存**し、UI/CLI/API で表示します。

| 区分 | 意味 | 扱い |
|---|---|---|
| `rate_limited` (HTTP 429) | レート制限 | retryable（バックオフ後に再試行）|
| `incomplete_data` | YouTube スロットリング（`Incomplete data received`）| retryable |
| `fragments_failed` | フラグメント DL 失敗 | retryable |
| `subtitles_failed` | 字幕取得失敗（多くは非致命）| retryable・**字幕だけ再取得可**|
| `comments_failed` / `live_chat_failed` | コメント/ライブチャット取得失敗 | retryable |
| `impersonation` | 任意 impersonation 依存の警告 | **低重要度**（retryable ではない）|
| `auth_required` / `quota_exceeded` / `forbidden` / `token_expired` | API/OAuth エラー | 設定要・quota は時間をおく |

- **`partial_success` は failed と明確に区別**（本体・info は取得できたが字幕だけ 429 等）。UI では黄色系バッジ。
- 「再試行可能な失敗だけ」を抽出できます（plain な `failed`＝動画削除等は retryable ではない）。

### retryable jobs / 手動 retry / 一括 retry

```bash
archiver jobs retryable [--reason rate_limited] [--type download] --limit 50
archiver jobs retry JOB_ID [--now] [--force]        # 回数上限超過は --force
archiver jobs retry-all [--reason incomplete_data] [--type download] --limit N [--now]
# API: GET /api/jobs/retryable, POST /api/jobs/{id}/retry[?force=true], POST /api/jobs/retry-all
```

- retry は **`retry_count` を加算**し、`DOWNLOAD_RETRY_MAX_ATTEMPTS` で上限（**無限 retry 防止**）。`next_retry_at` は retry/成功で解除。
- UI: Jobs 画面に **retryable filter / reason filter / Retry all**、Job 詳細に classification・Retry・**Retry subtitles only** ボタン。

### download retry / backoff

retryable な失敗は**指数バックオフ**で再試行時刻 `next_retry_at = now + BACKOFF * MULTIPLIER**attempt (+jitter)` を `jobs` 列に保存します（即時連続 retry しない）。`SCHEDULER_RETRY_ENABLED=true` で scheduler が `next_retry_at` を過ぎた retryable ジョブを自動再投入します（既定は手動 retry）。

```bash
DOWNLOAD_RETRY_MAX_ATTEMPTS=5
DOWNLOAD_RETRY_BACKOFF_SECONDS=600
DOWNLOAD_RETRY_BACKOFF_MULTIPLIER=2.0
DOWNLOAD_RETRY_JITTER_SECONDS=60
SCHEDULER_RETRY_ENABLED=false
SCHEDULER_RETRY_LIMIT_PER_RUN=10
```

### 字幕だけ再取得（subtitles_refresh）— 本体を再 DL しない

`metadata_only`/download で**字幕だけ失敗**したとき、本体を再 DL せず字幕のみ取り直します。

```bash
archiver subtitles failed --limit 50                 # 字幕失敗ジョブ一覧
archiver subtitles refresh VIDEO_OR_URL --now
archiver subtitles refresh-failed --limit N --now    # 失敗動画へ一括
# API: GET /api/subtitles/failed, POST /api/subtitles/refresh, POST /api/subtitles/refresh-failed
```

- profile `subtitles_refresh_only` = `--skip-download --write-subs --write-auto-subs --sub-langs <SUBTITLES_REFRESH_SUB_LANGS|DEFAULT_SUB_LANGS> --no-download-archive --no-playlist --remote-components ejs:github`（deno js runtime 維持、**本体フォーマット無し**）。字幕は**既存 video output dir** に保存し、**MediaFile(video/audio) は作りません**（body 数は増えない）。
- `job.meta`: `target_video_id` / `requested_sub_langs` / `subtitle_files_created` / `subtitle_files_updated` / `subtitles_failed` / `rate_limited`。

### metadata_only と video download の違い

- `metadata_only` / `subtitles_refresh_only` / `comments_refresh_only` / `live_chat_refresh_only`：**本体を保存しない**（Videos の body=0、Video 詳細は「未保存」）。
- `video_compressed_1080p` / `video_best_archive` 等：本体を保存（body≥1）。

### YouTube 取得安定化（cookies / browser cookies / PO-token / impersonation）

DL の 429・throttling を減らす運用オプション（**このフェーズでは設定の土台**。secret/token は UI/API/log に**出しません**。Settings は configured yes/no のみ）。

| 設定 | 説明 |
|---|---|
| `COOKIES_FILE` | cookies.txt のパス（`/secrets` か `/config`、Git 非管理）。`--cookies` |
| `COOKIES_FROM_BROWSER` | ブラウザから cookies（例 `chrome`）。`--cookies-from-browser`（cookies.txt 未設定時）|
| `YOUTUBE_PO_TOKEN` | PO token（**secret**）。`--extractor-args youtube:po_token=…`（command/log でマスク）|
| `curl_cffi`（同梱） | yt-dlp の **impersonation**。`impersonation` warning を減らせる**任意依存**。不安定なら無理に使わない |

- queue rate limiting: `DOWNLOAD_JOB_DELAY_SECONDS`（本体）・`METADATA_REFRESH_JOB_DELAY_SECONDS`・`SUBTITLES_REFRESH_JOB_DELAY_SECONDS`・`COMMENTS_REFRESH_JOB_DELAY_SECONDS` で種別ごとに連続 DL を間引けます。

### セキュリティ（7A）

- cookies / PO-token / token を **UI/API/log に出さない**（`redact_args` で `po_token=******`、`logs.mask_secrets` で読み出し時もマスク、Settings は configured yes/no）。`metadata_only`/`subtitles_refresh` の**本体非保存**を維持。retry は回数上限で**無限ループしない**。

### YouTube 取得安定化 doctor / diagnostics（Phase 7B）

「設定の有無」だけでなく「実際に効くか」を測る道具。**完全解決の保証ではなく、設定と傾向を確認するための診断**です（環境により 429 / Incomplete data received は残り得ます）。

#### 設定（すべて secret 扱い・値は UI/API/log に非表示）

| 設定 | 説明 |
|---|---|
| `COOKIES_FILE` | cookies.txt のパス。Docker では **`/config/cookies.txt`** 推奨（`/secrets` も可）。Git 非管理。`--cookies`（log では `--cookies ***REDACTED***`）|
| `COOKIES_FROM_BROWSER` / `YTDLP_COOKIES_FROM_BROWSER` | ブラウザ cookies（例 `chrome`）。両名を受理。**Docker 内はブラウザプロファイルが無く使いにくい**ため通常は cookies.txt 推奨。`--cookies-from-browser`（log マスク）|
| `YOUTUBE_PO_TOKEN` | PO token（**secret**）。`--extractor-args youtube:po_token=…`（マスク）|
| `YOUTUBE_VISITOR_DATA` | visitor data（**secret**、PO token とペア）。`youtube:visitor_data=…`（マスク）|
| `YTDLP_EXTRACTOR_ARGS` | 生の extractor-args 追記（非 secret チューニング）。例 `youtube:player_client=web` |
| `curl_cffi`（同梱） | yt-dlp の **impersonation**。`impersonation` warning を減らせる**任意依存**。効かない環境では warning 扱いで、既定では強制しない |

> cookies / cookies-from-browser / PO-token / visitor-data / extractor-args は **`profiles.build_ytdlp_args` の 1 箇所**で組み立て、`metadata_only` / `subtitles_refresh` / `comments_refresh` / `live_chat_refresh` / 本体 DL すべてに一貫適用。`--remote-components ejs:github` と deno は維持。

#### PO-token / visitor data の取得（概要）

- ブラウザの devtools / [yt-dlp の PO Token ガイド](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)等で取得した値を `YOUTUBE_PO_TOKEN` / `YOUTUBE_VISITOR_DATA` に設定するだけ（**完全自動取得は不要**）。値は**絶対に Git 管理しない**（`.env` はコミットしない）。

#### cookies.txt の配置（Docker）

1. ブラウザ拡張等で `cookies.txt`（Netscape 形式）を書き出す。
2. ホスト側に置き、コンテナの **`/config/cookies.txt`** にマウント（例 `-v $PWD/secrets/cookies.txt:/config/cookies.txt:ro`）。
3. `COOKIES_FILE=/config/cookies.txt` を設定。doctor で `file_exists` / `readable` を確認（**パス・中身は表示されません**）。

#### doctor youtube（静的・ネットワーク無し）

```bash
archiver doctor youtube
# GET /api/doctor/youtube
```

- yt-dlp version / deno / remote-components / curl_cffi installed・impersonate targets / cookies configured・file_exists・readable / browser cookies configured / PO-token configured / visitor data configured を **ok/warning/failed** で表示し、**recommended actions** を出す。secret 値・cookie パス・token は**一切表示しない**。

#### diagnostic ジョブ（実測・本体は保存しない）

```bash
# metadata_only + subtitles の実測（本体 DL 無し）
archiver youtube-diagnostics run --url https://youtu.be/<ID>
# 任意で小さな本体 DL も試す（一時 dir に DL→即削除、DB の media body は増えない）
archiver youtube-diagnostics run --url https://youtu.be/<ID> --video --timeout 180
# doctor からの即時テスト
archiver doctor youtube --test-url https://youtu.be/<ID>
```

- API: `POST /api/youtube-diagnostics/run` / `POST /api/doctor/youtube/run`（`job.type=youtube_diagnostic`）。
- 各ステップで **success/partial/failed・classification・duration・media_body_created** を記録し、**recommendations** を生成（`job.meta.diagnostic` / `job.meta.recommendations`）。video テストは**既定 OFF**、明示時のみ。**一時ディレクトリに DL して即削除**するため、video テストでも **DB の media body は 0**。

#### 推奨運用（429 / Incomplete data を減らす順序）

1. **`metadata_only`** で取得可否と classification を確認。
2. **`subtitles_refresh`** で字幕を補完（本体非 DL）。
3. 本体 DL は **delay / backoff 付きで少量ずつ**（`DOWNLOAD_JOB_DELAY_SECONDS` + `DOWNLOAD_RETRY_*`）。
4. 429 等は **`/api/jobs/retryable`（`archiver jobs retryable`）から時間を置いて再試行**。

### セキュリティ（7B）

- doctor / diagnostics / Settings は **configured yes/no と file_exists/readable のみ**。cookie パス・PO-token・visitor data・OAuth token は **UI/API/log/command に一切出さない**（`redact_args` + `logs.mask_secrets` で `po_token=******` / `visitor_data=******` / `--cookies ***REDACTED***`）。診断の本体 DL は**一時 dir→即削除**で `MediaFile` を作らない。cookies / OAuth secret/token / PO-token / Takeout ZIP は **Git 非管理**。

### Liked videos 一括アーカイブ（Phase 7C）

My Activity Takeout から取り込んだ **liked videos** を、429 / Incomplete data / subtitles_failed を前提に **少量ずつ安全に** metadata 取得・本体保存する運用です。**いきなり大量 DL せず、plan / dry-run → 少量 enqueue → classification 確認 → retryable 再試行** を基本とします。

#### metadata_only と body archive の違い（重要）

| 操作 | profile | 動画本体 | 用途 |
|---|---|---|---|
| **enqueue-metadata** | `metadata_only` | **保存しない**（info.json / description / thumbnail / subtitles のみ） | まず軽く取得可否を確認 |
| **enqueue-archive** | `video_compressed_1080p` 等 | **保存する（body DL）** | 本体を残す。**少量ずつ** |

> body archive は**動画本体をダウンロード**します。UI は確認ダイアログ＋赤い警告、CLI は黄色＋`[VIDEO BODY DOWNLOAD]`、API は `downloads_body=true` で明示します。

#### 状態の区別（body と metadata）

`/api/liked-videos` と一覧 UI は各 liked について `has_metadata` / `has_body` / `body_media_count` / `metadata_file_count` / `latest_archive_job_status` を返します（body = `video`/`audio` の `MediaFile`、metadata = info_json/description/thumbnail 等。**両者は明確に区別**）。

#### plan / dry-run（先に件数を見る）

```bash
archiver liked-videos plan-archive --profile video_compressed_1080p
# POST /api/liked-videos/archive-plan
```

候補数 / metadata 未取得 / body 未保存 / 既存 active job / retryable 件数 / **推奨 limit・delay・profile** を表示（**job は作らない**）。

#### enqueue（少量ずつ）

```bash
# 1) metadata（本体 DL なし）
archiver liked-videos enqueue-metadata --limit 3 --missing-only --now
# 2) 本体 archive（body DL！） dry-run で確認 → 少量実行
archiver liked-videos enqueue-archive --limit 1 --profile video_compressed_1080p --missing-body-only --dry-run
archiver liked-videos enqueue-archive --limit 1 --profile video_compressed_1080p --now
```

- API: `POST /api/liked-videos/enqueue-metadata`（後方互換）/ `enqueue-metadata-v2`（filters+dry-run）/ `enqueue-archive`。
- enqueue 条件: `--missing-only` / `--missing-body-only`、`--source`（takeout_my_activity / youtube_data_api / all）、`--channel`、`--title`。
- 結果: `selected_count` / `jobs_created` / `skipped_existing_job` / `skipped_already_has_metadata` / `skipped_already_has_body` / `job_ids`。
- 各 job は `job.meta` に `source_action=liked_archive` / `liked_video_id` / `liked_at` / `requested_profile` を付与（Job Detail から Liked videos へリンク）。

#### duplicate 防止 / retry

- 同じ video × profile の **queued/running** job があれば**重複 enqueue しない**（`skipped_existing_job`）。profile が違えば別物（metadata_only と video archive は両立）。
- 失敗は liked だけ抽出して再試行: `archiver liked-videos retryable` / `retry-failed --reason rate_limited`（`GET /api/liked-videos/retryable` / `POST /api/liked-videos/retry-failed`）。**retry 回数上限（`DOWNLOAD_RETRY_MAX_ATTEMPTS`）を守り、無限 retry しない**。

#### throttling（少量・間引き）

- `LIKED_ARCHIVE_DEFAULT_LIMIT`（既定 20）・`LIKED_ARCHIVE_MAX_ENQUEUE_PER_RUN`（安全上限 50）・`LIKED_ARCHIVE_DEFAULT_PROFILE`・`LIKED_ARCHIVE_JOB_DELAY_SECONDS`（liked archive job に追加 sleep）。
- **最初は 10〜30 件ずつ**。429 が出たら時間を置いて `retry-failed`。cookies / PO-token / `doctor youtube`（Phase 7B）と併用すると安定しやすい。
- scheduler 連携（`SCHEDULER_LIKED_ARCHIVE_ENABLED`）は**既定 OFF**（手動運用優先）。

#### 推奨運用

1. `plan-archive` で件数確認 → 2. `enqueue-metadata --limit 3` で取得可否確認（本体非保存）→ 3. `enqueue-archive --limit 1 --dry-run` → 少量 `--now` → 4. classification を Jobs / Job Detail で確認 → 5. 429 等は `retry-failed` で時間を置いて再試行。

### セキュリティ（7C）

- liked archive は **limit 必須/安全既定**で大量 DL を回避。raw_json（高評価履歴の個人データ）は既定非表示。body archive は body DL を明示。`metadata_only` の**本体非保存**を維持。secret/cookie/token/PO-token は引き続き UI/API/log に出さない。

### Liked archive scheduler + progress dashboard（Phase 7D）

Phase 7C の手動運用（plan / enqueue / retry）を、**少量ずつ安全に自動実行**できるようにした層です。scheduler は **既定すべて OFF**（誤って大量 DL しないため）で、明示的に有効化／run-once したときだけ動きます。

#### scheduler liked passes（すべて opt-in・既定 OFF）

| pass | 設定 | 内容 | 推奨 limit |
|---|---|---|---|
| metadata | `SCHEDULER_LIKED_METADATA_ENABLED` / `_LIMIT_PER_RUN` | metadata 未取得を `metadata_only` で取得（**本体非保存**） | 10〜50 |
| archive | `SCHEDULER_LIKED_ARCHIVE_ENABLED` / `_LIMIT_PER_RUN` / `_PROFILE` / `_SOURCE` / `_MISSING_BODY_ONLY` | body 未保存を archive（**本体 DL！**） | **1〜3** |
| retry | `SCHEDULER_LIKED_RETRY_ENABLED` / `_LIMIT_PER_RUN` | retryable liked job を再 queue（backoff 後のみ） | 1〜5 |

```bash
# 個別 run-once（SCHEDULER_*_ENABLED に関係なく即実行）
archiver scheduler run-once --liked-metadata     # 本体非保存
archiver scheduler run-once --liked-archive       # 本体 DL（少量）
archiver scheduler run-once --liked-retry
archiver scheduler run-once --all                 # collections+comments+liked 全部
# API
curl -XPOST /api/scheduler/run-once -d '{"liked_metadata":true}'   # liked のみ実行（collections/comments は走らない）
```

#### safety-first（大量 DL を避ける仕組み）

- 1 周期の enqueue 数は **`_LIMIT_PER_RUN` で上限**（body archive は既定 2）。
- **active 抑制**: liked-archive の **body ジョブ**が queued/running の間は archive pass を**スキップ**（`skipped_active_jobs`）。`SCHEDULER_LIKED_SUPPRESS_WHEN_ACTIVE=true`。metadata_only は対象外（軽いので継続可）。
- **dedup**: 同一 video×profile の queued/running があれば重複 enqueue しない（`skipped_duplicates`）。
- **backoff 尊重**: retry pass は `next_retry_at` が未来のジョブを**スキップ**し、`retry_count` 上限を超えたら再試行しない（`--force` は使わない）。
- body DL profile は**明示設定時のみ**（metadata_only が既定）。

#### run history（job.meta タグ）

scheduler が作るジョブは `job.meta` に `scheduled_by`（`scheduler_liked_metadata`/`_archive`/`_retry`）と `selected_by`（`missing_metadata`/`missing_body`/`retryable`）を保存。run-once の結果は `liked_metadata_selected/jobs_created` / `liked_archive_selected/jobs_created` / `liked_retry_selected/jobs_requeued` / `skipped_active_jobs` / `skipped_duplicates` / `job_ids` を返します。

#### progress dashboard

```bash
archiver liked-videos progress     # GET /api/liked-videos/progress
```

`total_liked` / `metadata_fetched`・`metadata_missing` / `body_saved`・`body_missing` / `active_archive_jobs` / `retryable_liked_jobs` / `failed_liked_jobs` / `partial_liked_jobs` / `by_source` / `by_channel`(top N) / `earliest|latest_liked_at` / `last_archive_job_at` / `last_successful_archive_at`。**raw_json（個人データ）は返しません**。UI（Liked Videos 画面上部）に進捗カード＋ source 内訳＋ run-once ボタン（metadata / archive=確認付き / retry）＋ queue 状態を表示。

#### queue health

```bash
archiver queue status              # GET /api/queue/status
```

queued/running、`by_type`、`by_source_action`、oldest queued、（取得できれば）worker 数。scheduler の active 抑制はこの「在庫」を見て判断します。

#### Jobs 画面

`source_action` フィルタ（liked_archive / scheduler_liked_metadata / scheduler_liked_archive / scheduler_liked_retry / comments_refresh / subtitles_refresh / takeout_import）を追加。progress の「View liked jobs →」から `/jobs?source_action=liked_archive` に絞り込み遷移できます。

#### 推奨運用

1. `liked-videos progress` で現状把握 → 2. `scheduler run-once --liked-metadata`（多め）で metadata を埋める → 3. `--liked-archive`（**1〜3 件**）で body を少量保存 → 4. 429 / Incomplete data は `--liked-retry`（backoff 後）で再試行 → 5. 常駐自動化したい場合のみ各 `SCHEDULER_LIKED_*_ENABLED=true` ＋ `SCHEDULER_INTERVAL_SECONDS` を長め（数時間）に。

### セキュリティ（7D）

- scheduler は**既定 OFF・小 limit・active 抑制・backoff 尊重**で大量 DL を避ける設計。progress/queue API は件数のみで raw_json/secret を返さない。`metadata_only` の**本体非保存**を維持し、body DL は UI/CLI/API で明示。

### Scheduler run history / progress 時系列 / adaptive throttle（Phase 7E）

Phase 7D の「現在状態」に加え、**scheduler 実行履歴**を `scheduler_runs` テーブルに保存し、進捗の**時系列**と**安全寄りの推奨値**を確認できるようにした層です。

#### run history（`scheduler_runs`）

`run_once` 1 回ごとに 1 行を記録：`run_id`(一意) / `run_type`(liked_metadata|liked_archive|liked_retry|comments|collections|all) / `status`(success|partial_success|failed) / selected・created・submitted / `skipped_active_jobs`・`skipped_duplicates`・`skipped_backoff` / `retryable_count`・`failed_count`・`partial_count`・`success_count` / `body_count_before`→`after` / `meta`(summary + progress/queue スナップショット)。記録失敗は**本体ジョブを壊さない**（fail-safe・ログのみ）。

```bash
archiver scheduler runs --limit 20         # GET /api/scheduler/runs
archiver scheduler runs show RUN_ID        # GET /api/scheduler/runs/{id} (+ /jobs)
archiver scheduler stats                   # GET /api/scheduler/stats
```

#### job ↔ run 連携

scheduler が作るジョブは `job.meta.scheduler_run_id`（＋ `scheduled_by`/`selected_by`）を保存。Job Detail に scheduler run リンク、`GET /api/jobs?scheduler_run_id=...`（UI: Jobs に絞り込みバナー）で run のジョブを一覧。

#### progress 時系列

run 完了時に liked progress スナップショット（total/metadata/body/retryable…）を run の `meta` に保存。`GET /api/liked-videos/progress/history`（`archiver liked-videos progress --history`、UI: Liked Videos の **History タブ**）で時系列を**表**表示（個人データ/raw_json は返さない）。

#### adaptive throttle（推奨のみ・自動変更しない）

```bash
archiver scheduler recommend-settings      # POST /api/scheduler/recommend-settings
```

直近の liked_archive 実 DL ジョブの **成功率 / throttle 率（429+incomplete）**・active body 数・retryable 数から、安全寄りの値を**提案だけ**します（**設定は自動変更しません**）：

| 観測 | 推奨 |
|---|---|
| throttle 率 ≥ 30%（429/incomplete 多い） | `SCHEDULER_LIKED_ARCHIVE_LIMIT_PER_RUN=1`・`LIKED_ARCHIVE_JOB_DELAY_SECONDS` を長く（≥300s） |
| 成功率 ≥ 80% かつ active body=0 | archive limit を +1（上限 5）まで微増可 |
| active body job あり | `SCHEDULER_LIKED_SUPPRESS_WHEN_ACTIVE` を維持 |
| retryable ≥ 5 | retry limit を小さく・`DOWNLOAD_RETRY_BACKOFF_SECONDS` を長く |

**429 が多い時の読み方**: throttle 率が高い＝YouTube 側の制限。limit を 1 に・delay を伸ばし、`--liked-retry` は backoff 経過後に少しずつ。成功率が戻るまで body archive を増やさないこと。

#### scheduler を安全に使う推奨運用

1. `scheduler stats` / `runs` で直近の傾向確認 → 2. `recommend-settings` で推奨値を確認（手動で `.env` に反映するか判断）→ 3. `--liked-metadata`（多め）→ `--liked-archive`（1〜3）→ `--liked-retry`（backoff 後）→ 4. `progress --history` で前進を確認。常駐は各 `SCHEDULER_LIKED_*_ENABLED=true` ＋長め interval。

### セキュリティ（7E）

- run history / progress history / stats / recommend は**件数・集計のみ**で raw_json/secret を返さない。adaptive throttle は**提案だけで設定を自動変更しない**。run 記録は fail-safe（失敗してもジョブ処理継続）。`metadata_only` の**本体非保存**・body DL の明示を維持。

### progress グラフ / run retention / recommendation export（Phase 7F）

Phase 7E の履歴を**見やすく可視化**し、**肥大化を防ぐ retention**と、推奨値を**安全に `.env` へ反映するための export**を追加した層です。

#### progress グラフ

Liked Videos の **History タブ**に、progress 時系列の**軽量 SVG 折れ線グラフ**（metadata_fetched / body_saved / retryable / total_liked、依存ライブラリなし）＋表を表示。`GET /api/liked-videos/progress/history?run_type=&from=&to=&downsample=daily&limit=` でフィルタ/間引き可能（`downsample=daily` は同日内の最新点のみ）。raw_json は返しません。

#### scheduler run history 強化

run history に **run_type / status フィルタ**、run 行クリックで**詳細ドロワー**（selected/created/submitted/skipped・body before→after・progress/queue スナップショット・関連ジョブ）。`GET /api/scheduler/runs?run_type=&status=&from=&to=&limit=`。各 run から `/jobs?scheduler_run_id=...` へ。

#### recommendation export（提案のみ・自動変更なし）

```bash
archiver scheduler recommend-settings --env    # .env 貼り付け用 KEY=VALUE
archiver scheduler recommend-settings --json   # JSON
# API: POST /api/scheduler/recommend-settings/export {"format":"env|json|human"}
```

UI（History → Recommended settings）に **Copy .env… / Copy JSON…** ボタンと現在値→推奨値の diff、`Copy to clipboard`。出力例：

```
SCHEDULER_LIKED_ARCHIVE_LIMIT_PER_RUN=1   # was 2
LIKED_ARCHIVE_JOB_DELAY_SECONDS=300   # was 0
```

> **設定ファイルは自動で書き換えません。** secret は含めません。内容を確認 → 手動で `.env` に反映 → `docker compose up -d` で再起動、が推奨フローです。

#### scheduler run retention / cleanup（ジョブは消さない）

```bash
archiver scheduler runs cleanup --keep-last 50 --dry-run            # 既定 dry-run
archiver scheduler runs cleanup --keep-last 50 --older-than-days 30 --apply
# API: POST /api/scheduler/runs/cleanup {"keep_last","older_than_days","dry_run"}
```

- 削除対象は **`scheduler_runs` 行のみ**。**ジョブ（`jobs`）は絶対に削除しません**。`job.meta.scheduler_run_id` は残り、run が消えても `/jobs?scheduler_run_id=...` でジョブは辿れます（UI は「run history deleted」相当＝run 詳細 404）。
- **両方 0（bound 無し）なら何も削除しない**安全既定。`--keep-last N`（最新 N 件保持）/ `--older-than-days D`（D 日より古いもの）。
- 自動 retention は `SCHEDULER_RUN_RETENTION_DAYS` / `SCHEDULER_RUN_KEEP_LAST`（**既定 0＝OFF**）。設定時のみ scheduler ループが各周期末に prune。
- **推奨運用**: まず `--dry-run` で削除件数を確認 → 必要なら `--apply`。export された env snippet は確認 → 手動で `.env` 反映 → compose 再起動。

#### optional aggregation

日次集計（`scheduler_run_daily_stats` 等）は将来拡張余地として設計のみ（**今回はテーブル追加なし**、retention 優先）。`downsample=daily` で当面の時系列圧縮は可能。

### セキュリティ（7F）

- recommendation export は**ファイルを自動変更せず**コピー用文字列のみ（secret 非包含）。cleanup は **scheduler_runs のみ**削除し**ジョブは保持**。progress graph / history は集計のみで raw_json/secret 非返却。`metadata_only` 本体非保存・body DL 明示を維持。

### Takeout 差分再取り込み + 省メモリ + import history（Phase 6C）

Takeout / My Activity の再取り込みを**差分（incremental）**で安全に行い、大容量でもメモリを使いすぎず、**import 履歴**を残せるようにした層です。

#### incremental import（差分）

全 import（watch / search / subscriptions / playlists / liked）は**既存 DB と重複する行を skip**し、新規のみ取り込みます。liked は既存 stub の空フィールドを再取り込みで**enrich（updated）**します。結果に `scanned` / `imported_count` / `skipped_duplicate_count` / `updated_count` / `failed_count` / `source_kind` / `detected_path` / `duration_seconds` / `session_id` を含み、**dry-run でも同じ集計**を返します（DB には書きません）。

#### 大容量 My Activity の省メモリ（streaming）

90k+ の watch / 11k+ の liked を含む巨大 JSON を、**ijson でストリーム解析**（top-level 配列を 1 件ずつ）し、全体をメモリに載せません。ijson 不在時は `json.loads` にフォールバック。2000 件ごとに進捗ログ（scanned/imported/skipped/updated）。

#### source registry（deep inspect）

```bash
archiver takeout inspect PATH --deep        # GET /api/takeout/inspect?path=...&deep=true
```

ZIP 内の検出結果を**構造化**して返します：`my_activity_youtube_json` / `youtube_watch_history_json|html` / `youtube_search_history_*` / `youtube_subscriptions_*` / `youtube_playlists` / `youtube_liked_videos_*` / `takeout_index`（`member` は ZIP 内パスのみ）。

#### import session history

```bash
archiver takeout sessions --limit 20        # GET /api/takeout/import-sessions
archiver takeout sessions show SESSION_ID   # GET /api/takeout/import-sessions/{id}
```

`run_once` 1 回ごとに `takeout_import_sessions` に 1 行（import-all は**1 件の combined session**）。保存は **ZIP の basename + 集計件数のみ**（**フルパス・raw_json・履歴行は保存しない**）。UI（Takeout 画面下部）に履歴テーブル、preview 時に registry バッジ、`Import liked / watch / search` ボタン + dry-run。

#### CLI / API（追加・拡張）

```bash
archiver takeout import-watch-history PATH --incremental --limit N --dry-run
archiver takeout import-search-history PATH --incremental --limit N --dry-run
archiver takeout import-all PATH --dry-run     # combined session
# API: POST /api/takeout/import-watch-history・import-search-history、GET import-sessions[/{id}]
```

#### My Activity と YouTube Takeout の違い（重要）

- **liked の全履歴は「マイ アクティビティ（My Activity）」Takeout** に入っています。**YouTube Takeout には通常 liked=0**（playlists の "Liked videos" は API 制限で 0 件のことが多い）。
- **index-only**（`archive_browser.html` のみ）の ZIP は取り込み対象なし（registry に `takeout_index` のみ）。

#### privacy / 注意

- 履歴・raw_json は個人情報として扱い、**既定 API/UI に出しません**（liked の raw_json は `include_raw=true` 明示時のみ）。import session には**フルパス・raw_json を保存しません**。
- Takeout ZIP は **Git 管理しない**。大容量再取り込みは `--limit` で少量ずつ・`--dry-run` で件数確認してから本実行を推奨。

### セキュリティ（6C）

- incremental import の dry-run は**DB 非書き込み**。import session は **basename + 件数のみ**（フルパス・raw_json・履歴行なし）。registry の `member` は ZIP 内パスのみ。ストリーム解析で大容量でも省メモリ。secret/cookie/token/PO-token は引き続き非表示。

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

# --- Phase 7B: YouTube 取得安定化 doctor / diagnostics ---
archiver doctor youtube                                  # 静的診断（configured yes/no・secret 非表示）
archiver doctor youtube --test-url https://youtu.be/<ID> # 静的 + 即時ライブテスト（本体非保存）
archiver doctor youtube --test-url <URL> --video --profile video_compressed_1080p
archiver youtube-diagnostics run --url <URL>             # metadata+subtitles 診断ジョブ
archiver youtube-diagnostics run --url <URL> --video --timeout 180 [--now]  # 任意で小本体DL（一時dir→即削除）

# --- Phase 2B: 再クロール / scheduler ---
archiver collections refresh COLLECTION_ID [--now] [--max-items N]   # 1件再クロール（removed検出あり）
archiver collections refresh-all [--now] [--max-items N]            # enabled全件（policy尊重）
archiver collections enable COLLECTION_ID
archiver collections disable COLLECTION_ID
archiver collections set-policy COLLECTION_ID new_only|refresh|manual
archiver scheduler run-once [--max-items N] [--collections] [--comments] [--all]  # 1パス（SCHEDULER_ENABLED無関係）
archiver scheduler run                             # ループ常駐（scheduler コンテナが使用）

# --- Phase 3A: Google Takeout import ---
archiver takeout list-files PATH                   # 検出ファイル一覧
archiver takeout preview PATH                      # 件数・サンプル（DB保存なし）
archiver takeout import PATH [--limit N] [--dry-run]   # 視聴履歴
archiver watch-history list [--limit N] [--offset N]
archiver watch-history stats

# --- Phase 3B: 残りの Takeout データ正規化 ---
archiver takeout import-subscriptions PATH [--limit N] [--dry-run]
archiver takeout import-playlists PATH [--limit-playlists N] [--limit-items N] [--dry-run]
archiver takeout import-all PATH [--limit-watch N --limit-search N --limit-subscriptions N --limit-playlists N --limit-items N] [--dry-run]
archiver takeout playlists PATH                    # 再生リスト一覧（title + 件数、保存なし）
# --- Phase 6C: 差分再取り込み / registry / import 履歴 ---
archiver takeout inspect PATH --deep               # source registry 付き
archiver takeout import-watch-history PATH [--limit N] [--incremental] [--dry-run]   # streaming
archiver takeout import-search-history PATH [--limit N] [--incremental] [--dry-run]
archiver takeout sessions [--limit N] [--kind liked_videos]   # import 履歴（件数のみ）
archiver takeout sessions show SESSION_ID
archiver search-history list [--limit N] / stats
archiver subscriptions list
archiver subscriptions enqueue --videos --shorts --streams --profile metadata_only --max-items 3 [--limit N] [--now]

# --- Phase 4A: コメント / メタデータ更新（本体は再DLしない） ---
archiver comments refresh VIDEO_OR_URL --now        # video id か URL
archiver comments refresh-video VIDEO_ID --now
archiver comments refresh-all [--due-only|--all] --limit-videos N --now   # 期限切れ / frozen以外を全件
archiver comments due [--limit N]                    # 期限切れ動画一覧
archiver comments schedule VIDEO_ID [--now-due]      # next_comments_refresh_at を再計算 / 即時due
archiver comments list VIDEO_ID [--limit N] [--active-only]
archiver comments stats VIDEO_ID
archiver comments snapshots VIDEO_ID                 # metadata snapshot 一覧

# --- Phase 4B: live chat 取得（本体・コメントは再DLしない） ---
archiver live-chat refresh VIDEO_OR_URL --now        # video id か URL
archiver live-chat refresh-all --limit-videos N --now   # has_live_chat/is_live かつ期限切れ
archiver live-chat list VIDEO_ID [--limit N] [--superchats-only]
archiver live-chat stats VIDEO_ID
```

> macOS でローカルに `archiver worker` を使うと fork 由来の問題が出る場合があります。ローカル確認では `--now` / `download run` のインライン実行を推奨します。Docker（Linux）では worker が正常動作します。

---

## Web API リファレンス

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/health` | 稼働状況（DB / Redis / yt-dlp バージョン） |
| GET | `/api/dashboard` | 管理 UI 集約（health / 件数 / job 集計 / scheduler / 最新ジョブ）【Phase 5A】 |
| GET | `/api/job-stats` | ジョブ件数集計（status 別 / type 別）【Phase 5A】 |
| GET | `/api/settings` | 非 secret 設定 + プロファイル（cookie/token 非表示・URL 認証情報マスク）【Phase 5A】 |
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
| GET | `/api/scheduler/status` | scheduler 設定と対象数（collections + comments） |
| POST | `/api/scheduler/run-once` | 手動で1パス実行（`{"collections","comments","liked_metadata","liked_archive","liked_retry","max_items"}`、liked サマリ含む）【Phase 2B/7D】 |
| GET | `/api/queue/status` | キュー在庫（queued/running・by_type・by_source_action・oldest・worker数）【Phase 7D】 |
| GET | `/api/scheduler/runs` | scheduler 実行履歴一覧（`?run_type=&limit=`）【Phase 7E】 |
| GET | `/api/scheduler/runs/{run_id}` | 1 run の詳細（meta に progress/queue スナップショット）【Phase 7E】 |
| GET | `/api/scheduler/runs/{run_id}/jobs` | その run が作成したジョブ一覧【Phase 7E】 |
| GET | `/api/scheduler/stats` | 直近 run の集計（status/type 別・skip 合計）【Phase 7E】 |
| POST | `/api/scheduler/recommend-settings` | 安全寄り推奨値（`{lookback}`、**自動変更なし**）【Phase 7E】 |
| POST | `/api/scheduler/recommend-settings/export` | 推奨値を `.env`/JSON/human で出力（`{format}`、ファイル非変更・secret 非包含）【Phase 7F】 |
| POST | `/api/scheduler/runs/cleanup` | 古い run を削除（`{keep_last,older_than_days,dry_run}`、**ジョブは消さない**）【Phase 7F】 |
| GET | `/api/liked-videos/progress/history` | progress 時系列（`?run_type=&from=&to=&downsample=daily&limit=`、raw_json 非返却）【Phase 7E/7F】 |
| GET | `/api/jobs?scheduler_run_id=` | scheduler run 単位でジョブ絞り込み【Phase 7E】 |
| POST | `/api/takeout/preview` | Takeout ZIP の preview（`{"path"}`、保存なし） |
| POST | `/api/takeout/import` | 視聴履歴 import（`{"path","limit","dry_run"}`） |
| POST | `/api/takeout/import-subscriptions` | 登録チャンネル import |
| POST | `/api/takeout/import-playlists` | 再生リスト import（`{"path","limit_playlists","limit_items","dry_run"}`） |
| POST | `/api/takeout/import-all` | 5種（watch/search/subs/playlists/liked）を順に import（各 limit 指定可）【6A 拡張】 |
| POST | `/api/takeout/import-liked-videos` | 高評価リスト import（My Activity / YouTube 自動判定、`source_kind`/`detected_path` 返却）【6A/6B】 |
| GET | `/api/takeout/discover` | ZIP 種別自動判定一覧（`?deep=`）【Phase 6B】 |
| GET | `/api/takeout/inspect` | 1 ZIP の構造判定（`?path=&deep=` で source registry 付き）【Phase 6B/6C】 |
| POST | `/api/takeout/import-watch-history` | 視聴履歴 import（差分・streaming・session 記録）【Phase 6C】 |
| POST | `/api/takeout/import-search-history` | 検索履歴 import（差分・streaming・session 記録）【Phase 6C】 |
| GET | `/api/takeout/import-sessions` | import 履歴一覧（件数のみ・パス/raw_json 非保存）【Phase 6C】 |
| GET | `/api/takeout/import-sessions/{session_id}` | import 履歴詳細【Phase 6C】 |
| POST | `/api/library/bootstrap` | Hybrid 初回構築（YouTube + My Activity + 任意 API）【Phase 6B】 |
| GET | `/api/youtube-api/status` | OAuth 状態（secret/token・パス非表示）【Phase 6B】 |
| POST | `/api/youtube-api/sync-liked` | API 差分同期（未設定でも 200 + `ok=false` + classification）【Phase 6B】 |
| GET | `/api/takeout/playlists/preview` | 再生リスト一覧 preview（`?path=&limit=`） |
| GET | `/api/watch-history` | 視聴履歴一覧（`?q=&limit=&offset=&include_raw=`） |
| GET | `/api/watch-history/stats` | 視聴履歴の統計（件数 / 期間 / top channels） |
| GET | `/api/search-history` | 検索履歴一覧（`?q=&limit=&offset=&include_raw=`） |
| GET | `/api/search-history/stats` | 検索履歴の統計（件数 / 期間 / top queries） |
| GET | `/api/subscriptions` | 登録チャンネル一覧 |
| POST | `/api/subscriptions/enqueue` | 登録チャンネルを expand job 化（`{"videos","shorts","streams","profile","max_items","limit"}`） |
| POST | `/api/comments/refresh` | コメント更新（公式 `{"target":"<id|url>"}`、互換 `{"video":...}`、両方は400） |
| POST | `/api/videos/{id}/comments/refresh` | 指定動画のコメント更新 |
| POST | `/api/comments/refresh-all` | 一括（`{"limit_videos","profile","due_only","all"}`、既定は due_only） |
| GET | `/api/comments/due` | 期限切れ（更新対象）動画一覧（`?limit=`） |
| GET | `/api/videos/{id}/comments` | コメント一覧（`?include_missing=&include_raw=&limit=&offset=`） |
| GET | `/api/videos/{id}/comments/stats` | コメント統計（total/active/missing/状態/次回更新） |
| GET | `/api/videos/{id}/snapshots` | metadata snapshot 一覧（`?snapshot_type=`） |
| POST | `/api/live-chat/refresh` | live chat 更新（公式 `{"target":"<id|url>"}`、互換 `{"video":...}`、両方は400） |
| POST | `/api/videos/{id}/live-chat/refresh` | 指定動画の live chat 更新 |
| POST | `/api/live-chat/refresh-all` | has_live_chat/is_live かつ期限切れを一括（`{"limit_videos","profile"}`） |
| GET | `/api/videos/{id}/live-chat` | live chat 一覧（`?include_missing=&superchats_only=&include_raw=&limit=&offset=`） |
| GET | `/api/videos/{id}/live-chat/stats` | live chat 統計（total/active/missing/superchats/members/状態） |
| GET | `/api/doctor` | 環境診断（書込可否 / ツール版 / DB / Redis） |
| GET | `/api/doctor/youtube` | YouTube 取得安定化の静的診断（configured yes/no・secret 非表示）【Phase 7B】 |
| POST | `/api/doctor/youtube/run` | 診断ジョブ作成（metadata+subtitles、video は任意）【Phase 7B】 |
| POST | `/api/youtube-diagnostics/run` | 取得安定化ベンチ（`{"url","profile","include_video_download","timeout"}` → `youtube_diagnostic`）【Phase 7B】 |
| GET | `/api/jobs` | ジョブ一覧（`?status=&type=&limit=&offset=`、`classification` 付き） |
| GET | `/api/jobs/retryable` | 再試行可能な失敗ジョブ（`?reason=&type=&limit=`）【Phase 7A】 |
| POST | `/api/jobs/retry-all` | 再試行可能ジョブを一括 re-queue（`{"reason","type","limit"}`）【Phase 7A】 |
| GET | `/api/subtitles/failed` | 字幕取得に失敗したジョブ一覧【Phase 7A】 |
| POST | `/api/subtitles/refresh` | 字幕のみ再取得（`{"target"}`、本体非DL）【Phase 7A】 |
| POST | `/api/subtitles/refresh-failed` | 字幕失敗動画へ一括字幕再取得（`?limit=`）【Phase 7A】 |
| GET | `/api/jobs/{id}` | ジョブ詳細（status/error/ログパス/出力先/動画/profile/classification/retry） |
| GET | `/api/jobs/{id}/logs` | command/stdout/stderr をまとめて取得（`?tail=N`） |
| GET | `/api/jobs/{id}/logs/{stdout\|stderr\|command}` | 単一ログを生テキストで取得（`?tail=N`） |
| GET | `/api/jobs/{id}/log` | （後方互換）末尾のみの JSON |
| POST | `/api/jobs/{id}/retry` | 失敗/キャンセル/部分成功ジョブの再実行（`?force=true` で回数上限を無視）【7A 拡張】 |
| POST | `/api/jobs/{id}/cancel` | ジョブのキャンセル |
| POST | `/api/profiles/{name}/build-command` | dry-run（`{"url"}`）。cookie/secret はマスク |
| GET | `/api/videos` | 保存済み動画一覧（`?q=&comments_state=&live_chat_state=&has_media=&limit=&offset=`、body 数付き）【Phase 5A 拡張】 |
| GET | `/api/videos/{id}` | 動画詳細（メディアファイル/字幕数/コメント数 + comments/live_chat 状態） |
| GET | `/api/videos/{id}/jobs` | その動画の関連ジョブ【Phase 5A】 |
| GET | `/api/videos/{id}/collections` | その動画が属する collection【Phase 5A】 |
| GET | `/api/videos/{id}/media/{media_file_id}` | media body 配信（**HTTP Range 対応**・DB 登録ファイルのみ・`ARCHIVE_ROOT` 配下強制）【Phase 5A/5B】 |
| GET | `/api/videos/{id}/thumbnail` | サムネ配信（guarded）【Phase 5B】 |
| GET | `/api/videos/{id}/related` | 関連動画（同 channel + 同 collection）【Phase 5B】 |
| GET | `/api/videos/channels` | 動画を持つ channel 一覧（件数付き、フィルタ用）【Phase 5B】 |
| GET | `/api/search` | 横断検索（`?q=&types=video,comment,live_chat,collection,liked_video&limit=`、raw 非返却）【5B/6A】 |
| GET | `/api/library/summary` | ライブラリ分類サマリ（liked は実 count）【5B/6A】 |
| GET | `/api/liked-videos` | 高評価リスト一覧（`?q=&source=&only_missing_metadata=&only_missing_body=&include_raw=`、`has_body`/`has_metadata`/`latest_archive_*` 付き）【Phase 6A/7C】 |
| GET | `/api/liked-videos/stats` | 高評価リスト統計【Phase 6A】 |
| POST | `/api/liked-videos/enqueue-metadata` | 高評価動画に `metadata_only` ジョブを一括投入（本体保存なし・後方互換）【Phase 6A】 |
| POST | `/api/liked-videos/archive-plan` | 一括アーカイブの plan/dry-run（件数・推奨 limit/delay、job 作成なし）【Phase 7C】 |
| POST | `/api/liked-videos/enqueue-metadata-v2` | metadata 一括（filters+dry-run、本体保存なし）【Phase 7C】 |
| POST | `/api/liked-videos/enqueue-archive` | **本体 archive** 一括（body DL！ `downloads_body=true`、dry-run 可）【Phase 7C】 |
| GET | `/api/liked-videos/retryable` | liked 由来の retryable ジョブ一覧(`?reason=&limit=`)【Phase 7C】 |
| POST | `/api/liked-videos/retry-failed` | liked 由来 retryable を再 queue(`{reason,limit}`、回数上限尊重)【Phase 7C】 |
| GET | `/api/liked-videos/progress` | 進捗集計(metadata/body 保存・retryable・by_source/channel、raw_json 非返却)【Phase 7D】 |
| GET | `/api/jobs?source_action=` | `source_action`/`scheduled_by` でジョブ絞り込み(liked_archive 等)【Phase 7D】 |
| GET | `/api/takeout/files` | `TAKEOUT_IMPORT_ROOT` 配下の ZIP 一覧（root 外は不可）【Phase 5A】 |

`/api/jobs`・`/api/jobs/{id}` は **`classification`**（429/partial/retryable/warnings）を含みます【Phase 5B】。Videos 一覧は `?channel_id=&sort=` を追加。
UI は `/`（管理コンソール + 視聴）、OpenAPI は `/docs`（Swagger UI）/ `/redoc`。

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
  - `0004` `watch_history_events` に `(source, youtube_video_id, watched_at)` のユニーク制約（Takeout 視聴履歴の重複防止。既存重複は安全に dedup）
  - `0005` `search_history_events` テーブル追加（`(source, query, searched_at)` ユニーク）
  - `0006` `videos` に `last_comments_refresh_at` / `next_comments_refresh_at` / `comments_state`（adaptive コメント更新）
  - `0007` `videos` に `comment_refresh_failures` / `last_live_chat_refresh_at` / `next_live_chat_refresh_at`(index) / `live_chat_state` / `has_live_chat`、`live_chat_messages` に `time_text` / `amount_text` / `message_type` / `published_at` / `fetched_at` / `is_deleted_or_missing`（Phase 4B。NOT NULL 列は `server_default` 付きで既存行も安全に移行）
  - `0008` `liked_videos` テーブル追加（`source`/`youtube_video_id`/`title`/`channel_title`/`url`/`liked_at`/`video_id`(FK)/`raw_json`/`created_at`、`(source, youtube_video_id)` ユニーク、各種 index）（Phase 6A・SQLite/PostgreSQL 両対応）
  - Phase 6B は**マイグレーション追加なし**（既存 `liked_videos.source` を `takeout_my_activity`/`takeout_youtube`/`youtube_data_api` で運用、channel_id は `raw_json` + Video stub へ反映）
  - `0009` `jobs` に `retry_count`(server_default 0) / `retry_of_job_id` / `next_retry_at`(index)（Phase 7A・retry/backoff。SQLite 互換のため自己参照 FK はプレーン列で追加）
  - Phase 7B / 7C / 7D は**マイグレーション追加なし**（7B 診断結果は `job.meta.diagnostic`、7C/7D liked archive は既存 `Job`/`MediaFile`/`LikedVideo` を再利用し `job.meta.source_action`/`scheduled_by`/`selected_by` でタグ付け）
  - `a1b2c3d4e5f6` `scheduler_runs` テーブル新規（Phase 7E・実行履歴 + progress/queue スナップショット。整数カウント列は `server_default '0'` で PostgreSQL/SQLite 両対応）
  - Phase 7F は**マイグレーション追加なし**（retention は `scheduler_runs` の delete のみ・ジョブ非削除。日次集計テーブルは将来拡張）
  - `b2c3d4e5f6a7` `takeout_import_sessions` テーブル新規（Phase 6C・import 履歴。basename + 集計件数のみ保存。整数列 `server_default '0'` で PostgreSQL/SQLite 両対応）
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
