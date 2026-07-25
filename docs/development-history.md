# Development history (archived)

> Detailed, phase-by-phase development log. Moved out of the top-level `README.md`
> in Phase 11B so the README can serve first-time users; for product usage see
> [`../README.md`](../README.md). The original title/intro is kept below.

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
archiver liked-videos failures   # Phase 7H: 失敗を理由別に集計(private/deleted/unavailable/network/rate_limited/unknown)
# Phase 7I: cookie/PO-token 対応 + metadata 安定全件取得
archiver liked-videos metadata-run --limit 100 [--dry-run]   # rate-limit ゲート付き段階 metadata 取得
archiver liked-videos metadata-run --all --confirm           # 全 missing（429 比率高で自動停止, exit 2）
archiver liked-videos retry-metadata --retryable | --reason rate_limited   # retryable のみ再投入(permanent除外)
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

### 大容量 import benchmark + import job 化 + progress（Phase 6D）

実 My Activity（liked ~11k / watch ~90k）を**安全に・進捗を見ながら**取り込めるようにした層です。**dry-run / limit / background job が安全な既定**です。

#### benchmark（throughput / peak memory）

```bash
archiver takeout benchmark PATH --kind liked_videos|watch_history|search_history|all [--limit N] [--dry-run]
# POST /api/takeout/benchmark {"path","kind","limit","dry_run"}
```

`scanned` / `imported` / `skipped_duplicate` / `updated` / `failed` / `duration_seconds` / **`entries_per_second`** / **`peak_memory_mb`**（tracemalloc）/ **`parser_backend`**(ijson/json) / `source_kind` を返します（**dry-run 既定**・**個人情報本文は返さない**）。ijson ストリームのため巨大ファイルでも peak memory は小さく保たれます。

#### import の background job 化

```bash
archiver takeout import-liked-videos PATH --limit N --job [--dry-run] [--now]
archiver takeout import-watch-history PATH --limit N --job
archiver takeout import-search-history PATH --limit N --job
# API: POST /api/takeout/import-liked-videos-job / import-watch-history-job / import-search-history-job
```

`job.type=takeout_import`（既存と非衝突。同期 import は RQ に出さないため worker は job 化分のみ処理）。`job.meta` に `import_kind`/`path_basename`/`source_kind`/`limit`/`dry_run`/`session_id`/`scanned/imported/skipped/updated/failed`/`duration_seconds`/`entries_per_second`（**ホスト絶対パスは保存しない**）。worker は `ProgressTracker` で進捗を import session に書き、partial import は残ります。

#### progress / cancel

```bash
archiver takeout sessions progress SESSION_ID     # GET /api/takeout/import-sessions/{id}/progress
archiver takeout sessions cancel SESSION_ID        # POST /api/takeout/import-sessions/{id}/cancel
```

実行中は `status=running` + `current_phase` + 件数 + `entries_per_second` を返却（DB 更新は throttle：N 件 or 数秒間隔）。cancel は `cancel_requested` を立て、**parser loop が checkpoint で停止**（`status=cancelled`、**部分 import 済みデータは残る**）。完了済み session の cancel は 409。

#### session ↔ job 連携

`takeout_import_sessions` に `job_id`/`rq_job_id`/`parser_backend`/`entries_per_second`/`peak_memory_mb`/`cancel_requested`/`current_phase`/`last_update_at` を追加（migration 0012）。session show / 一覧 / UI に job link、Job Detail から session を辿れます。**6C で null だった watch/search の `source_kind`** は archive_kind（my_activity_takeout/youtube_takeout/takeout_index）で補完。

#### 大容量 import の推奨手順

1. `benchmark --kind ... --dry-run` で eps / peak memory を確認 → 2. `--dry-run --limit 1000` で件数確認 → 3. `--job --limit N` で background 実行（progress を `sessions progress` で監視）→ 4. 問題なければ limit を上げて本 import。**Docker のディスク空き容量に注意**（11k/90k の raw_json は DB を肥大化させ得る）。

### セキュリティ（6D）

- benchmark / job / progress は**件数・集計・eps/peak_mem のみ**で raw_json/secret/絶対パスを返さない。job.meta は basename 中心。dry-run（既定）は **DB 非書き込み**。大容量は dry-run/limit/job を安全既定に。`metadata_only` 本体非保存・body DL 明示を維持。

### 実 11k/90k 本番規模 import + no-raw-json + DB stats + retention（Phase 6E）

11k liked / 90k watch クラスの本番 import を **DB を肥大化させず安全に** 回すための運用機能群。

#### no-raw-json import モード（DB サイズ削減）

```bash
# CLI: 各 import に --no-raw-json（raw 活動 blob を保存しない）
archiver takeout import-liked-videos  PATH --no-raw-json [--job --limit N]
archiver takeout import-watch-history PATH --no-raw-json
archiver takeout import-search-history PATH --no-raw-json
archiver takeout import-all PATH --no-raw-json
# API: import / import-*-job リクエストに "store_raw_json": false
```

- **正規化フィールド（video_id / title / channel / timestamp / query）は常に保持**します。落とすのは `raw_json` 活動 blob のみ。
- 既定は後方互換のため `store_raw_json=true`（UI は OFF を推奨表示）。
- session.meta / job.meta に `store_raw_json` / `raw_json_stored_count` / `raw_json_skipped_count` を記録。
- 11k/90k 規模では raw_json が DB 肥大化の主因なので、**本番では `--no-raw-json` 推奨**。

#### DB stats（DB サイズ / raw_json 増加の可視化）

```bash
archiver storage db-stats        # GET /api/storage/db-stats
```

- table 件数・概算サイズ（PostgreSQL `pg_total_relation_size` / SQLite `PRAGMA page_count×page_size`）、`raw_json_stored`（**JSON-null literal は除外して実 blob のみ計上**）、videos / liked / watch / search / session 件数。
- raw_json / 本文は一切読まず**集計のみ**返却。

#### benchmark-large（liked+watch 一括フルスキャン dry-run）

```bash
archiver takeout benchmark-large PATH [--include-search]
# POST /api/takeout/benchmark-large {"path","include_search"}
```

- liked + watch（任意で search）を **dry-run フルスキャン**し、kind 別に scanned / eps / peak_memory_mb / parser_backend / `estimated_full_import_time_seconds` / `recommended_batch_size` を返却（**DB 非書き込み・raw_json/本文非返却**）。

#### import session の retention / cleanup（session 行のみ削除）

```bash
archiver takeout sessions cleanup --keep-last N [--older-than-days D] --dry-run   # 既定 dry-run（プレビュー）
archiver takeout sessions cleanup --keep-last N --apply                            # 実削除
# POST /api/takeout/import-sessions/cleanup {"keep_last","older_than_days","dry_run"}
```

- **削除対象は `takeout_import_sessions` 行のみ**。**job も、import 済み liked/watch/search データも絶対に削除しません**（結果に `jobs_preserved` を明示）。
- **bounds 未指定（keep_last/older_than_days とも 0）では何も削除しない**安全設計。**実行中（running）session は削除対象外**。dry-run 既定。
- config: `TAKEOUT_IMPORT_SESSION_RETENTION_DAYS` / `TAKEOUT_IMPORT_SESSION_KEEP_LAST`（既定 0 = 無効）。

#### --safe-large プリセット（最も安全な既定で大容量 import）

```bash
archiver takeout import-watch-history PATH --safe-large            # benchmark のみ実行（--limit 無し）
archiver takeout import-watch-history PATH --safe-large --limit 1000 --apply   # job + no-raw-json + 実書き込み
archiver takeout import-liked-videos  PATH --safe-large --limit 100  --apply
```

- `--safe-large` は **job=ON / no-raw-json=ON / dry-run（`--apply` 無しの間）** を既定化。`--limit` 無しなら **benchmark のみ**走り、eps/peak_mem を表示して「`--limit N`（＋`--apply`）で再実行」を案内。UI には "Safe large import" ボタン + 確認ダイアログ。

#### 実 11k/90k 本番手順

1. `storage db-stats` で現状の DB サイズを確認 → 2. `benchmark-large PATH` で liked+watch の eps / peak_mem / 推定フル import 時間を把握 → 3. `import-watch-history PATH --safe-large --limit 1000 --apply`（no-raw-json job）で小さく検証 → 4. progress を `sessions progress` / UI で監視 → 5. 問題なければ limit を上げて本 import → 6. 完了後 `storage db-stats` で増加量を確認、古い session を `sessions cleanup --keep-last N --apply` で剪定。**Docker のディスク空き容量に注意**。

### セキュリティ / プライバシー（6E）

- db-stats / benchmark-large は**集計・件数・サイズのみ**で raw_json / 本文 / secret / 絶対パスを返さない。
- no-raw-json は raw 活動 blob を落とすが**正規化フィールドは保持**。`raw_json_stored` は JSON-null literal を除外し実 blob 数のみ計上。
- cleanup は **session 行のみ削除**（job / import 済みデータは不可侵）。bounds 未指定で no-op、running は保護、dry-run 既定。
- Takeout ZIP は Git 非管理。`metadata_only` 本体非保存・body DL 明示・`--remote-components ejs:github` + deno・ZIP path traversal 対策を維持。

### 実運用安全化・自動cleanup・本番全件import手順化（Phase 6F）

実運用で大容量 import を安全に回すための「事故防止」機能群。**stale worker（web と worker が別ビルド）** を import 前に検出し、preflight → import-large → verify-import の手順をコード化する。

#### ⚠ stale worker 対策（最重要）

**コード変更後は web だけでなく worker も必ず再ビルドすること**（6E 検証で worker が古いイメージのまま no-raw-json を無視した事故を踏まえた対策）。

```bash
docker compose build web worker migrate   # 必要なら --no-cache。全イメージを揃える
docker compose up -d
archiver system preflight                  # ← import 前に必ず実行（stale なら FAIL で止まる）
```

- 各プロセスは `build_id`（`APP_BUILD_ID` env、未設定なら `app/` ソースの content hash）を持つ。同一ソースから揃ってビルドすれば web と worker の `build_id` は一致。**古い worker は不一致 → preflight が FAIL**。
- worker は起動時+定期に短 TTL の heartbeat（build_id 付き）を Redis に publish。preflight/health はそれを読んで「worker 生存・build 一致・takeout_import 処理可能」を判定。

```bash
archiver system build-info     # GET /api/system/build-info（app_version/build_id/schema_head）
archiver system preflight      # DB/Redis/alembic head/web=worker build/worker稼働/パス可否（fail で exit 1）
# GET /api/system/health/full … db/redis + workers + worker_build_match + schema_head_match
```

#### preflight-large（大容量 import 前チェック）

```bash
archiver takeout preflight-large PATH [--kind liked_videos|watch_history|all]
# POST /api/takeout/preflight-large {"path","kind","sample_limit"}
```

- ZIP 存在（basename のみ表示）・parser=ijson・kind 別サンプル benchmark（eps/peak）・現在の DB 件数・raw_json 方針・推奨コマンドを表示。**dry-run のみ（書き込みなし）**。

#### import-large（本番全件 import runner）

```bash
archiver takeout import-large PATH --kind watch_history                       # 既定 = dry-run + no-raw-json + job
archiver takeout import-large PATH --kind watch_history --limit 1000 --apply   # 実行（段階的に limit を上げる）
archiver takeout import-large PATH --kind all --apply                          # liked+watch
```

- **安全既定: dry-run（`--apply` 必須）/ no-raw-json（`--raw-json` で ON）/ background job（`--no-job` で同期）**。
- **実行前に自動で preflight**（system + large）。`--apply` の job 実行は **system preflight が FAIL（例: stale worker）なら import せず中止**。`--skip-preflight` で回避可（非推奨）。
- 出力: kind / session_id / job_id / dry_run / store_raw_json / 推奨 progress・db-stats コマンド。

#### verify-import（import 後検査）

```bash
archiver takeout verify-import SESSION_ID
archiver takeout verify-import --latest [--kind watch_history]
# GET /api/takeout/import-sessions/{id}/verify
```

- session 結果（status/件数/eps/peak）・store_raw_json/raw_json カウント・**DB stats + raw_json 実 blob 数**・job status / worker error・**secret/cookie/絶対パスの漏洩 grep**（session/job メタのみ対象）。UI は各 session 行に “verify” ボタン。

#### import session 自動 cleanup（session 行のみ）

```bash
archiver takeout sessions cleanup-auto --dry-run    # プレビュー
archiver takeout sessions cleanup-auto --apply      # 即時実行（enabled/interval を無視して force）
archiver takeout sessions cleanup-status            # 設定 + 直近 cleanup 結果
# GET /api/takeout/import-sessions/cleanup-status
```

- config: `TAKEOUT_IMPORT_SESSION_CLEANUP_ENABLED` / `TAKEOUT_IMPORT_SESSION_CLEANUP_INTERVAL_HOURS`（+ 既存 `RETENTION_DAYS`/`KEEP_LAST`）。enabled かつ bound>0 のとき **scheduler が INTERVAL_HOURS ごとに自動 prune**（status は config 配下のファイルに記録）。
- **削除対象は import session 行のみ。job も import 済みデータも削除しない。running session は保護。**

#### 本番 watch 全件 import 推奨手順

1. **backup**（DB バックアップ）→ 2. `docker compose build web worker migrate` → 3. `docker compose up -d`（migrate 実行）→ 4. `archiver system preflight`（PASS を確認）→ 5. `archiver takeout preflight-large myactivity.zip --kind watch_history` → 6. `archiver takeout benchmark-large myactivity.zip` → 7. `archiver takeout import-large myactivity.zip --kind watch_history --limit 1000 --apply` → 8. `archiver takeout verify-import --latest` → 9. `archiver storage db-stats` → 10. 問題なければ `--limit 10000` → 11. 最後に limit 無しで全件。
- **raw_json を ON にする場合**: DB が肥大化（11k/90k で顕著）。`--raw-json` を付けたら verify-import / db-stats で増加量を必ず確認。
- **rollback / retry**: import-large の job は冪等（再 import は video_id / (source,title,url) で dedup）。失敗時は verify-import で「どこまで進んだか」を確認し、同じ kind を再実行すれば未取込分のみ追加される。session cleanup は安全（データ非削除）なのでいつでも実行可。

### セキュリティ / プライバシー（6F）

- build-info / preflight / health/full / verify-import は **build_id・件数・集計・check 結果のみ**。raw_json 本文 / cookie / token / PO-token / visitor_data / Mac 絶対パスは返さない（worker heartbeat も build_id 等のみ）。
- import-large は **dry-run / preflight / no-raw-json / job を安全既定**にし、stale worker 時は `--apply` を中止。
- auto cleanup は **session 行のみ**（job / import 済みデータ不可侵・running 保護・bound 未設定で no-op）。

### 本番全件importの段階実行・再開性・運用レポート（Phase 6G）

実 liked 約11k / watch 約90k を **段階的に**（limit付き→全件）安全に取り込み、各段で verify + db-stats を挟み、運用レポート化する。6F の preflight / import-large / verify / db-stats を1コマンドの導線に統合。

#### staged import runner

```bash
archiver takeout import-staged PATH --kind watch_history              # 既定 = dry-run（plan + benchmark、書き込みなし）
archiver takeout import-staged PATH --kind watch_history --apply      # 段階実行（1000→10000→50000、各段で verify+db-stats）
archiver takeout import-staged PATH --kind watch_history --apply --allow-full   # 最後の全件段を許可
archiver takeout import-staged PATH --kind liked_videos --apply --max-stage 2   # 最初の2段だけ
```

- **段階 limit**: liked `100→1000→5000→full` / watch `1000→10000→50000→full`（累積。dedup で各段は差分のみ追加）。
- **安全既定**: **dry-run（`--apply` 必須）/ no-raw-json（`--raw-json` で ON）/ background job（`--no-job` で同期）**。最初に system preflight + preflight-large を自動実行し、`--apply` の job は **system preflight 失敗（stale worker 等）で中止**。
- **full 段は `--allow-full` 必須**（無いと skip して案内）。`--max-stage N` で N 段で停止。
- 各段で保存: session_id / job_id / scanned / imported / skipped / updated / failed / eps / peak_memory_mb / raw_json stored・skipped / **db size before・after** / status / verify_ok。

#### 再開 / 再実行の安全性（resume / rerun）

- import-staged は実行前に**同じ kind / ファイル名の直近 session 履歴**を表示（`prior_sessions`）。
- **再実行は冪等**: 重複は dedup（liked=youtube_video_id、watch=(source,video_id,watched_at)）で skip され、**破壊的変更にならない**。
- **自動削除・自動巻き戻しはしない**（中断後は同じコマンドを再実行すれば未取込分のみ追加）。

#### operation report

```bash
archiver takeout import-report --latest
archiver takeout import-report SESSION_ID
archiver takeout import-report --kind watch_history --recent 10
# GET /api/takeout/import-report/latest ・ /api/takeout/import-report/{session_id}
```

- 内容: import 結果 + job 状態 + verify 結果 + db-stats + raw_json 保存有無 + **leak check** + **推奨次アクション**（成功→次段 / 失敗→worker_error 確認後に再実行[dedup安全] / raw_json ON→db-stats 確認 / leak→ALERT）。
- **raw_json 本文 / 履歴本文 / 絶対パス / cookie・token 類は返さない。**

#### UI: Production import wizard

Takeout 画面に「**Production import wizard**」を追加。手順 1.System preflight → 2.Preflight large → 3.Dry-run benchmark → 4.Staged import（stage limit ボタン、**full は確認ダイアログ**）→ 5.Verify → 6.DB stats → 7.Report。各 step に OK/WARN/FAIL を表示、**no-raw-json は既定 ON**（raw-json 保存は「上級者向け・DB 肥大」と明示）、直近 report を表示。

#### 本番 watch 全件 import 推奨手順（6G）

1. `docker compose build web worker migrate`（**web/worker を必ず揃える**）→ 2. `docker compose up -d` → 3. `archiver system preflight`（PASS）→ 4. `archiver takeout import-staged myactivity.zip --kind watch_history`（dry-run plan 確認）→ 5. `archiver takeout import-staged myactivity.zip --kind watch_history --apply --max-stage 1`（1000 を投入し verify/db-stats を確認）→ 6. 問題なければ `--max-stage 2`（10000）→ `--max-stage 3`（50000）→ 7. **ユーザー確認の上** `--apply --allow-full`（全件）→ 8. `archiver takeout import-report --latest`。
- **no-raw-json 推奨理由**: raw_json 本文は個人情報かつ DB 肥大化の主因（11k/90k で顕著）。正規化フィールド（video_id/title/channel/timestamp）は常に保持されるため、既定 OFF で運用に支障なし。
- **DB 容量注意**: 全件取込前に `storage db-stats` で空き容量を確認。raw_json ON は容量を数倍に。
- **cleanup との関係**: import 履歴 session は `sessions cleanup` / `cleanup-auto` で剪定可（**job・取込データは消えない**）。レポート用に直近数件は残すと良い。

### セキュリティ / プライバシー（6G）

- import-staged / import-report は **件数・集計・check・推奨アクションのみ**。raw_json 本文 / 履歴本文 / secret / cookie / token / 絶対パスを返さない。
- 段階実行は **no-raw-json / preflight / verify を安全既定**。full 段は `--allow-full`（CLI）/ 確認ダイアログ（UI）でユーザー確認必須。re-run は dedup 安全で非破壊。

### 本番全件import完了・運用固定（Phase 6H）

実 `myactivity.zip`（約139MB）の liked / watch を **no-raw-json で全件 import 完了**。実機結果を記録し、日常運用手順を確定。

#### 実本番 import 結果（PostgreSQL）

| 指標 | before | after（全件） |
|---|---|---|
| liked_videos | 1,000 | **11,066** |
| watch_history_events | 10,000 | **92,303** |
| videos（stub 含む） | 1,000 | 11,066 |
| raw_json_stored_total（実 blob） | 0 | **0** |
| DB total size | 12.87 MB | **46.23 MB**（watch table 27.18MB） |

- verify-import: 各段 / 最終とも `status=success`・`leak_check=clean`・`raw_json_real_blobs_total=0`。watch 全件 stage は scanned=92303 imported=27303（+既存65000）eps≈2215 peak≈108MB。
- **no-raw-json 運用維持**: raw_json 実 blob は全工程で 0。正規化フィールド（video_id/title/channel/timestamp）は保持。
- **段階実行**: liked `100→1000→5000→full`、watch `1000→10000→50000→full`。各段 dedup で差分のみ追加（既存分は skip）。full 段は `--allow-full` で実行。
- secret/cookie/token/PO-token/visitor_data/`/Users`/`/takeout_imports` の漏洩 **なし**（API/CLI/log 全確認）。

#### 実運用で踏んだ不具合と対処（6H で修正済み）

- **長すぎる title による import 失敗**: 実 watch 履歴に **1024 文字超の title** があり、PostgreSQL の `VARCHAR(1024)` 制約で INSERT バッチが落ち、watch 全件 stage が途中失敗（SQLite テストでは未検出＝長さ非強制）。→ importer で **title(1024)/channel_title(512)/query(512) を column 上限に clip**（migration なし）。再ビルド後に再実行 → dedup で未取込分のみ追加され全件完了（**失敗時の自動削除・巻き戻しはなし**、再実行で安全に前進）。
- **rolling recreate 直後の preflight 一時 STALE**: `docker compose up -d` 直後は旧 worker の heartbeat が TTL(90s) 切れ前に残り、preflight が一時的に STALE 表示。約90秒で自動解消（実害なし）。確実にしたい場合は `docker compose up -d --force-recreate worker` 後に数十秒待ってから preflight。

#### 日常運用手順（確定版）

```bash
# 1. build（コード変更後は web/worker/migrate を必ず揃える）
docker compose build web worker migrate && docker compose up -d
# 2. preflight（PASS を確認。STALE なら ~90s 待つか worker 再作成）
docker compose exec web archiver system preflight
# 3. dry-run（plan + benchmark、書き込みなし）
docker compose exec web archiver takeout import-staged myactivity.zip --kind watch_history
# 4. 段階 apply（1000→10000→50000、各段 verify+db-stats 自動）
docker compose exec web archiver takeout import-staged myactivity.zip --kind watch_history --apply --max-stage 1
#    問題なければ --max-stage 2 → 3 → 最後にユーザー確認の上 --allow-full
docker compose exec web archiver takeout import-staged myactivity.zip --kind watch_history --apply --allow-full
# 5. verify
docker compose exec web archiver takeout verify-import --latest
# 6. report
docker compose exec web archiver takeout import-report --latest
# 7. db-stats（容量・raw_json_stored_total=0 を確認）
docker compose exec web archiver storage db-stats
# 8. cleanup（session 行が増えたら。job/取込データは消えない）
docker compose exec web archiver takeout sessions cleanup-auto --dry-run   # 確認後 --apply
```

liked も同手順（`--kind liked_videos`）。UI は Takeout 画面の **Production import wizard** で同じ流れを対話実行できる（full は確認ダイアログ・no-raw-json 既定 ON）。

### 取り込み済み liked/watch を起点にした本番アーカイブ運用（Phase 7H）

import 済みの liked=11,066 / watch=92,303 を起点に、YouTube の **metadata 取得 → body/archive 保存** を段階的・安全に運用する。新機能は最小限（失敗理由の分類強化のみ）で、既存の liked-videos / jobs / scheduler / archive 機能を使う。

#### 失敗理由の分類（Phase 7H で追加）

metadata/archive ジョブの失敗を分類して可視化:

| reason | 意味 | retryable | 扱い |
|---|---|---|---|
| `private` | 非公開動画 | × | 記録（**削除しない**） |
| `deleted` | 削除/uploader 削除 | × | 記録 |
| `unavailable` | 視聴不可（地域/メンバー限定/削除） | × | 記録 |
| `network` | ネットワーク/サーバ(5xx)エラー | ○ | 後で再試行 |
| `rate_limited` | HTTP 429（YouTube スロットリング） | ○ | 後で再試行 |
| `unknown` | 未分類の失敗 | × | 記録（要調査） |

- Jobs API は各ジョブに `classification`（`primary_reason` / `permanent` / `retryable` / `reasons`）を付与。`GET /api/jobs?reason=private` で絞り込み可。
- 集計: `archiver liked-videos failures` / `GET /api/liked-videos/failure-breakdown` が **理由別カウント**を返す（件数のみ・本文/URL/パス非返却）。UI の Liked archive progress に "failures by reason" を表示（permanent は赤、retryable は橙）。
- **private/deleted/unavailable は再試行しない**（永続失敗）。**失敗動画は削除せず理由付きで記録**。

#### 「metadata 取得済み」の定義（重要）

Takeout import は **title だけの Video stub** を作る（実 metadata ではない）。Phase 7H では「metadata 取得済み」を **実 metadata ファイル（info_json 等）が存在すること**と定義（title stub は未取得扱い）。よって取り込み直後は `metadata_fetched=0`、`--missing-only`（既定）が **全 stub を対象に metadata を取得**し、取得済み（info_json あり）は skip する。

#### 推奨運用手順

```bash
docker compose exec web archiver system preflight            # PASS 確認
docker compose exec web archiver storage db-stats            # before
docker compose exec web archiver liked-videos plan-archive --limit 5         # 候補/未取得数の確認
docker compose exec web archiver liked-videos enqueue-metadata --limit 100 --dry-run
docker compose exec web archiver liked-videos enqueue-metadata --limit 100   # metadata_only（本体DLしない）
# 完了後: jobs / metadata_fetched / 失敗理由を確認
docker compose exec web archiver liked-videos failures
docker compose exec web archiver liked-videos plan-archive --limit 5
docker compose exec web archiver storage db-stats            # after
# body archive は小規模から（本体DL・容量注意）
docker compose exec web archiver liked-videos enqueue-archive --limit 10 --dry-run
docker compose exec web archiver liked-videos enqueue-archive --limit 10     # → 100 → full はユーザー確認後
```

- **段階**: metadata 100 → 1000 → 全件、その後 archive 10 → 100 → full（full はユーザー確認後）。1回の enqueue は `LIKED_ARCHIVE_MAX_ENQUEUE_PER_RUN`（既定 **50**）で hard cap。
- **deleted/private 動画**: 自動的に該当 reason で分類・記録され、再試行されない（liked 行は残る）。
- **rate limit 注意**: cookie/PO-token 未設定だと metadata/body とも 429（rate_limited）になりやすい。安定運用には `COOKIES_FILE` / `YOUTUBE_PO_TOKEN`（README のセキュリティ節）を設定。429 は retryable なので `liked-videos retry-failed` / scheduler retry pass で後追い。
- **storage 容量注意**: metadata(info_json) は軽量だが、**body archive は1本あたり数十〜数百MB**。full archive 前に `storage db-stats` とディスク空きを確認。
- **scheduler**: liked passes は **既定 OFF**。有効化する場合も metadata `SCHEDULER_LIKED_METADATA_LIMIT_PER_RUN`(既定10) / archive `..._ARCHIVE_LIMIT_PER_RUN`(既定2) + hard cap 50 + `SUPPRESS_WHEN_ACTIVE`(既定 true) で**一気に大量 job を作らない**。UI の "Run … pass once" でも手動 1 回実行可。

#### 実機検証結果（実 myactivity.zip / 11,066 liked）

- metadata `--limit 100`（cap 50）→ 50 job: **success 8 / partial 41 / failed 1**。**metadata_fetched 0→49**（info_json 保存）。失敗分類: `rate_limited 44`(retryable) / `unavailable 1`(permanent)。
- archive `--limit 3`（本体DL）→ cookie 無しのため全て 429 で `partial_success`（retryable・**body 0 件保存・liked データは不変**）。
- db-stats: 46.23→46.47MB、**raw_json_stored_total は終始 0**。secret/raw_json/cookie/token/`/Users`/`/takeout_imports`/`/archive` の API/UI/log 露出 **なし**。

### セキュリティ / プライバシー（7H）

- failure-breakdown / progress / stats / jobs は **件数・分類・集計のみ**。raw_json 本文・履歴本文・動画 URL・cookie/token/PO-token・絶対パスを返さない。
- 失敗動画（private/deleted/unavailable）は **理由付きで記録、削除しない**。body 保存は archive root 配下のみ（API/UI/log に絶対パス非表示）。
- 大量 import より **metadata→小規模 archive** を優先。full archive はユーザー確認後。

### cookie/PO-token 対応 + metadata 全件取得の安定運用（Phase 7I）

cookie/PO-token を設定して 429（rate_limited）を減らし、liked=11,066 の metadata を **段階的・安全に全件取得**する。新機能は最小限（rate-limit ゲート付き metadata-run + 機密ステータス表示）。

#### cookie / PO-token 設定（機密 — Git/UI/API/log に実値を出さない）

`.env`（**ユーザーが自分で編集**。AI/Git は実値を書かない）:

| env | 用途 |
|---|---|
| `COOKIES_FILE` | cookies.txt のパス（マウント秘密）。`/secrets/cookies.txt` 等 |
| `COOKIES_FROM_BROWSER` | ローカルブラウザから cookie 取得（`chrome` 等） |
| `YOUTUBE_PO_TOKEN` | PO token（429 緩和）。secret |
| `YOUTUBE_VISITOR_DATA` | PO token とペア。secret |

- 設定状況は **boolean/masked のみ**で確認: `archiver system preflight`（`cookies_file` / `po_token` / `secret_value_exposed=false` チェック）、`GET /api/system/secrets-status`、UI の Liked archive progress の "fetch auth" バッジ。**実値・絶対パスは一切表示しない**（preflight/secrets-status はパスも返さず、cookie ファイルの readable/last_modified だけ）。
- **read-only マウント対応**: yt-dlp は終了時に cookie jar を `--cookies` パスへ書き戻すため、`COOKIES_FILE` を `:ro` マウント（`./secrets:/secrets:ro` 等）に置くと `[Errno 30] Read-only file system` で失敗する。実行時に **元 cookie を writable な一時ファイルへコピーして yt-dlp に渡し、終了後に削除**する（元ファイルは read-only のまま不変）。`command.txt` でも cookie パスは `--cookies '******'` とマスクされ、一時パスもログに出ない。**`COOKIES_FILE` は read-only マウント推奨**（書き戻しが起きても安全）。

#### metadata 全件取得（段階・rate-limit ゲート）

```bash
archiver liked-videos metadata-run --limit 100            # dry-run は --dry-run
archiver liked-videos metadata-run --limit 1000
archiver liked-videos metadata-run --all --confirm        # 全 missing（--confirm 必須）
```

- 動作: capped バッチ（`LIKED_METADATA_MAX_ENQUEUE_PER_RUN`、既定 200）で **missing-metadata** を enqueue → worker 完了待ち → そのバッチの **rate_limited 比率**を測定 → target 到達/missing 枯渇/比率が STOP 閾値以上のいずれかで停止。**metadata_only（本体DLしない）**。
- **rate-limit safety**: `LIKED_METADATA_WARN_ON_RATE_LIMIT_RATIO`(0.5) で WARN、`LIKED_METADATA_STOP_ON_RATE_LIMIT_RATIO`(0.8) で **full/staged run を停止**（CLI は exit 2 を返すのでスクリプトが full を止められる）。比率が高い＝cookie/PO-token を設定すべきサイン。
- 各 run: preflight（worker 必須）→ enqueue → wait → failure-breakdown / metadata_fetched / db-stats / **推奨次アクション**を出力。

#### retryable / permanent と再試行（permanent は選定から除外）

- **retryable**: `rate_limited` / `network` / `impersonation` / `unknown`（一時的・要調査）→ 選定対象に残し、後で再試行。
- **permanent**: `private` / `deleted` / `unavailable` → **metadata 選定から既定除外・再試行しない・削除しない・理由付きで保持**。

permanent な動画は info_json が永遠に作られない＝「missing metadata」のままなので、対策しないと **metadata-run のたびに同じ private 動画を選び直してしまう**（実機で private が 41 unique なのに 801 attempts ＝ 約20回ずつ再試行されていた）。Phase 7J 以降、`metadata-run` / `enqueue-metadata` / `plan-archive` は **各動画の最新 metadata 試行が permanent なら選定から除外**する（`metadata_only` ジョブの最新分類で判定。body archive の失敗とは混同しない）。

```bash
archiver liked-videos progress     # eligible missing / permanent unique(kept) を表示
archiver liked-videos failures     # reason 別に attempts と unique_videos を分けて表示
archiver liked-videos retry-metadata --retryable            # 全 retryable を再投入（permanent 除外）
archiver liked-videos retry-metadata --reason rate_limited  # 429 のみ
# どうしても permanent も再試行したい場合のみ（非推奨・明示必須）:
archiver liked-videos metadata-run --limit 100 --include-permanent
archiver liked-videos enqueue-metadata --all --include-permanent
```

- 既定では permanent を除外（`--include-permanent` / `--retry-permanent` を明示した時のみ対象）。permanent は `retryable=false` なので `retry-metadata` の対象にも自動で入らない。
- `metadata-run` 出力に `selected` / `skipped_permanent` / `eligible_missing` / `permanent_kept` を表示。`progress` は `eligible_metadata_missing` / `skipped_permanent_metadata` / `permanent_unique_videos` を、`failure-breakdown` は `attempts_by_reason` と `unique_videos_by_reason`（distinct 動画）を返す。UI の Liked archive progress も "Eligible missing" / "Permanent (kept)" カード + reason 別 `unique/attempts` を表示（「再試行せず保持・選定から除外」と明記）。**動画行は削除しない。**

#### 429 が多い場合の対処

1. `system preflight` / secrets バッジで cookie/PO-token 未設定を確認 → `.env` に `COOKIES_FILE`（または `COOKIES_FROM_BROWSER`）+ `YOUTUBE_PO_TOKEN`/`YOUTUBE_VISITOR_DATA` を設定 → `docker compose up -d`（worker 再起動）。
2. `LIKED_METADATA_JOB_DELAY_SECONDS` を上げてリクエスト間隔を空ける。
3. STOP した run は `retry-metadata --retryable` で後追い。**body archive は metadata 完了後に小規模から**（cookie 設定済みでも 429 になりやすい）。

#### 実機検証結果（cookie 未設定状態）

- `system preflight`: `cookies_file=WARN`（未設定）/ `po_token=WARN` / `secret_value_exposed=ok(false)`。secret 値・絶対パスの露出なし。
- `metadata-run --limit 100`（cap 50/batch）: cookie 無しのため **rate_limited が大半 → ratio が STOP 閾値超 → run 停止**（429 を分類して安全に止まる挙動を確認）。`metadata_fetched` は info_json 保存分だけ増加。
- `retry-metadata`: retryable のみ再投入、private/deleted/unavailable は除外。

### セキュリティ / プライバシー（7I）

- cookie/PO-token/visitor_data の **実値・絶対パスは API/UI/log/preflight/secrets-status のどこにも出さない**（configured/readable booleans + masked timestamp のみ）。`.env` はユーザーが編集、Git 非管理。
- metadata-run は **本体DLしない・worker 必須・rate-limit STOP ゲート付き**。permanent 失敗は再試行も削除もしない。

### metadata 段階取得の実機運用結果（Phase 7K）

cookie 設定済み + `LIKED_METADATA_JOB_DELAY_SECONDS=1.5` で `metadata-run` を **300 → 500 → 1000 → 2000** と段階拡大。300〜1000-run は rate-limit ゲート（WARN 0.5 / STOP 0.8）下で **STOP 無しで完走**、2000-run は**深い領域で 429 が増え、batch 38 が 0.82 に達して STOP ゲートが発動**（＝設計どおり安全停止、1 バッチ手前で停止）。コード変更なし＝既存ループ + 既存集計の運用のみ。migration head は `c3d4e5f6a7b8` のまま。

#### 実測（cookie + delay=1.5s、本体は一切DLしない）

| run | attempted | success(info_json完備) | rate_limited(部分取得) | permanent検出 | ratio | stopped | metadata_fetched |
|---|---|---|---|---|---|---|---|
| `--limit 300` | 300 | 244 | 41 | — | **0.137** | none | 1033 → 1318 (+285) |
| `--limit 500` | 500 | 392 | 92 | — | **0.184** | none | 1318 → 1802 (+484) |
| `--limit 1000` | 1000 | 881 | 85 | 34（private18 / unavailable10 / deleted6） | **0.085** | none | 1802 → 2768 (+966) |
| `--limit 2000` | 1950（batch38 で STOP） | 1070 | 791 | 89（private/deleted/unavailable） | **0.406**（overall） | **stop（batch38=0.82）** | 2768 → 4629 (+1861) |

- 累計（2000-run 後）: `metadata_fetched=4629`（**info_json 完備 3050 / description のみ 1579**）/ `eligible_missing=6230` / `permanent_unique=207`（保持・選定除外）/ DB 51.0 → 64.26 MB。整合: `11066 = 4629 + 6230 + 207`。**全 run で raw_json stored=0・body saved=0・active jobs=0**（本体未取得）を維持。

#### `success` と `metadata_fetched` の違い（定義 — 数値の食い違いは定義差）

- **`success`（バッチ値）** = ジョブが status=success でクリーン終了＝`info_json` を完備。
- **`metadata_fetched`（progress 見出し）** = `info_json/description/thumbnail/link/live_chat` の**いずれか1つ以上**を持つ liked 動画数。429 で途中停止して `.description` だけ書けた **partial_success（reason=rate_limited）も「取得済み」に数える**。
- ゆえに `metadata_fetched` 増分 = `success` + `rate_limited 部分取得`。実測一致: 300→244+41=285 / 500→392+92=484 / 1000→881+85=966 / 2000→1070+791=1861。累計内訳は **info_json 完備 3050 + description のみ 1579 = 4629**。

#### `LIKED_METADATA_JOB_DELAY_SECONDS=1.5` の効果（429 抑制）

metadata ジョブ専用ディレイ（本体アーカイブの `LIKED_ARCHIVE_JOB_DELAY_SECONDS` とは別系統）を 0 → 1.5s にして rate_limited 比率が大きく低下:

| 条件 | rate_limited ratio |
|---|---|
| cookie 無し | ~0.92（STOP 閾値超 → 即停止） |
| cookie 有り・delay 0 | ~0.36 |
| **cookie 有り・delay 1.5s** | **0.137 / 0.184 / 0.085 / 0.406**（300 / 500 / 1000 / 2000-run・overall） |

2000-run は overall 0.406 だが**バッチ単位では深い領域で上昇**: 序盤〜中盤は 0.1〜0.3、batch 13 以降は 0.4〜0.66 を頻発し、batch 38 で 0.82（STOP）。delay だけでは深い領域の 429 を抑えきれず、**PO-token が次の手**であることを示す。

#### permanent-skip の効果

permanent（private/deleted/unavailable）は info_json が永遠に作られず「missing」のまま再選択され続けるため、選定から除外（Phase 7J）。実機では run を重ねるごとに `skipped_permanent` が **53 → 68 → 84 → 118 → 207** と増加（新規 permanent を検出した瞬間に除外）。除外しなければ同一動画を約20回ずつ再試行していた（cumulative: private 893 attempts / **133 unique**）。**permanent 動画は削除せず、理由付きで保持。**

#### PO-token 推奨と次段（full は要ユーザー確認）

- `secrets-status`: `cookies_configured=true` / **`po_token_configured=false`（未設定）**。
- **2000-run で full への自動 GO 条件は未達**: overall ratio は 0.406（<0.5）だが、深い領域（batch 13 以降）で 429 が増え、**batch 38 が 0.82 で STOP ゲート発動**（success=8 / rate_limited=41）。`ratio<0.5 かつ STOP 無し` を満たさないため、**残り 6,230 件の full metadata へは進まない**（gate が設計どおり安全停止）。
- **次段の推奨（ユーザー判断待ち）**: ① `.env` に `YOUTUBE_PO_TOKEN`/`YOUTUBE_VISITOR_DATA` を設定（深い領域の 429 をさらに低減）→ `docker compose up -d` で worker 再起動 → 小さめの `metadata-run` で ratio を再確認、② または現状のまま `retry-metadata --reason rate_limited` で rate_limited（cumulative unique 1,588）を間隔を空けて後追い。いずれの場合も **full metadata 全件（`--all --confirm`）・body archive は必ずユーザー確認後**に実施する（本 Phase では未実行）。

### rate-limit 安定化 + info_json 完備率の可視化（Phase 7L）

2000-run の batch STOP を受け、**full には進まず** 429 をさらに下げ、完備率を正しく測れるようにした（migration 追加なし、head は `c3d4e5f6a7b8` のまま）。

#### `metadata_fetched` は broad count（完備率は info_json で測る）

- **`metadata_fetched`（= `metadata_any_count`）**: `info_json/description/thumbnail/link/live_chat` の**いずれか1つ以上**を持つ動画数（broad）。429 で `.description` だけ書けた partial も含む。
- **`info_json_complete_count`**: `info_json` を持つ動画数（**完備＝full-metadata 判断はこの値を使う**）。
- **`description_only_count`**: description はあるが info_json が無い動画。
- **`retryable_partial_count`**: 上記のうち最新 metadata ジョブが retryable（rate_limited 等）で、`retry-metadata` で info_json へ格上げ可能な動画。
- `progress`（CLI / `GET /api/liked-videos/progress`）と UI の Liked archive progress に上記4値を表示（UI は "info_json complete" / "desc-only (retryable)" カード）。

#### metadata-run の level 表記を是正（overall OK でも batch STOP を明示）

- 旧: overall ratio が 0.5 未満だと batch STOP が起きても `level=OK stopped=...` と紛らわしかった。
- 新: 最終 `level` は **overall と全 batch の最悪値**（さらに rate-limit STOP なら強制 `STOP`）。出力に `overall ratio [level]` と `worst batch [level]` を併記し、batch STOP 時は **⚠ 警告行**を出す。`level==stop` で CLI は exit 2（スクリプトが full を止められる）。

#### 429 抑制チューニング + yt-dlp 更新

- `LIKED_METADATA_JOB_DELAY_SECONDS` を **1.5 → 3.0**、`LIKED_METADATA_JOB_DELAY_JITTER_SECONDS=1.0`（各 metadata ジョブの遅延に 0〜1s のランダム揺らぎを足し、完全な周期性を崩す）を追加。delay 計算は `compute_liked_job_delay()`（純関数・テスト済み）に切り出し。
- yt-dlp を **2026.03.17 → 2026.06.9**（`requirements.txt` / `pyproject.toml` の下限を更新）。90 日超の "yt-dlp is older than 90 days" 警告と抽出器ドリフトを解消。**web/worker 両イメージを再ビルド**し、`system preflight` の `worker_build_match` で一致を確認（worker が古いと旧 yt-dlp/旧コードのままになる）。

#### detached 実行のログ可視化（途中経過が見える）

- `metadata_run()` に `on_batch` コールバックを追加し、CLI が**各 batch 完了ごとに flush 付きで出力**（旧実装は run 完了まで何も出なかった）。イメージは `PYTHONUNBUFFERED=1`（Dockerfile 既定）。
- 長時間 run は **detached + マウント済み `/logs`** に出すと、ホスト側タスクが落ちても batch 別 ratio が残る:
  ```bash
  docker compose exec -d web sh -c \
    'archiver liked-videos metadata-run --limit 300 > /logs/mr300.log 2>&1'
  # 進捗は /logs/mr300.log を tail（batch 行が随時 append される）／ DB の info_json_complete でも確認
  ```

#### 小規模再テスト（300）と判定ルール

yt-dlp 更新 + delay 3.0s+jitter 後、まず `metadata-run --limit 300` を実行し、次の段階を決める:

| 300-run overall ratio | 次アクション |
|---|---|
| `< 0.3` | 1000 へ拡大してよい |
| `0.3 〜 <0.5` | 300/500 で継続（拡大は慎重に） |
| `>= 0.5` | **拡大しない**。PO-token / visitor_data を設定して再測定 |
| `>= 0.8`（または batch STOP） | STOP。PO-token 必須 |

- retryable partial（description-only）の格上げは、**まず `retry-metadata --retryable --limit 100〜200`** で `info_json_complete_count` が増えるか確認してから（全件を一気に流さない）。
- **本 Phase でも body archive・full metadata（`--all --confirm`）・`--include-permanent` は未実行**。permanent は保持（削除しない）。cookie/PO-token/visitor_data の実値・パスは log/UI/API/README に出さない（worker ログの cookie も `--cookies '******'` でマスク）。

#### 300-run 実測（yt-dlp 2026.06.9 + delay 3.0s + jitter 1.0、本体DLなし）

| batch | success | rate_limited | ratio | level |
|---|---|---|---|---|
| 0 | 41 | 5 | 0.10 | ok |
| 1 | 31 | 17 | 0.34 | ok |
| 2 | 42 | 7 | 0.14 | ok |
| 3 | 43 | 1 | 0.02 | ok |
| 4 | 35 | 15 | 0.30 | ok |
| 5 | 43 | 2 | 0.04 | ok |

- 全体: attempted=300 success=235 rate_limited=47 **overall ratio=0.157 [ok] / worst batch=0.34 [ok] / stopped=None**（**どの batch も WARN 未満**＝2000-run の深部 0.5〜0.82 から大幅改善）。
- 完備率: **info_json complete 3050 → 3285（+235＝clean success）** / broad metadata 4629 → 4911（+282） / description-only 1579 → 1626（+47）。eligible 6230 → 5930 / permanent 207 → 225（+18、保持）/ DB 64.26 → 65.03 MB / raw_json=0 / body=0 / active=0 / 秘匿リーク無し。
- **判定（decision tree）**: overall 0.157 **< 0.3 → 1000 へ拡大可**。ただし full metadata（`--all --confirm`）・body archive は引き続きユーザー確認後のみ。深部で worst batch が再び 0.5 以上 / STOP に達する場合は PO-token / visitor_data の設定を推奨。

#### 1000-run 実測（判定に従い拡大、yt-dlp 2026.06.9 + delay 3.0s + jitter）

300-run が 0.157 < 0.3 だったため 1000 へ拡大。**20 batch すべて ≤0.24（WARN すら無し）で完走**。

- 全体: attempted=1000 success≈821 rate_limited=80 **overall ratio=0.08 [ok] / worst batch=0.24 [ok] / stopped=None**。
- 完備率: **info_json complete 3285 → 4106（+821）** / broad 4911 → 5812（+901） / description-only 1626 → 1706。eligible 5930 → 4930 / permanent 225 → 324（保持）/ DB 65.03 → 67.77 MB / raw_json=0 / body=0 / active=0 / 秘匿リーク無し。
- **2000-run（delay 1.5, 旧 yt-dlp）との対比**: overall 0.406→**0.08**、worst batch 0.82(STOP)→**0.24(STOPなし)**。delay 3.0s+jitter + yt-dlp 更新で深部の 429 が大幅に低減。

**累計（7L 300+1000 後）**: broad **5812/11066（52.5%）** / **info_json complete 4106/11066（37%）** / eligible_missing 4930 / permanent 324。`11066 = 5812 + 4930 + 324`。**full metadata（`--all --confirm`）・body archive は未実行**（ユーザー確認後のみ）。

### staged metadata completion（Phase 7M）

残り eligible（4,930）を full 一括ではなく **1000件単位**で段階的に消化。主指標は broad ではなく **`info_json_complete_count`**。各 run の前に preflight（worker build 一致 / cookies OK / secret 非露出 / yt-dlp 2026.06.9 / delay 3.0+jitter）と active=0 を確認し、1 run ごとに結果を報告してから次へ進む（無断連続実行しない）。

| run | attempted | success | rate_limited | overall ratio | worst batch | stopped | info_json complete | broad | eligible 残 |
|---|---|---|---|---|---|---|---|---|---|
| #1 | 1000 | 869 | 38 | **0.038 [ok]** | **0.16 [ok]** | None | 4106 → **4975**（+869） | 5812 → 6719 | 4930 → **3930** |
| #2 | 1000 | 892 | 36 | **0.036 [ok]** | **0.22 [ok]** | None | 4975 → **5867**（+892） | 6719 → 7647 | 3930 → **2930** |
| #3 | 1000 | 843 | 68 | **0.068 [ok]** | **0.16 [ok]** | None | 5867 → **6710**（+843） | 7647 → 8558 | 2930 → **1930** |
| #4 | 1000 | 879 | 56 | **0.056 [ok]** | **0.28 [ok]** | None | 6710 → **7589**（+879） | 8558 → 9493 | 1930 → **930** |
| final | 930 | 735 | 122 | **0.131 [ok]** | **0.36 [ok]** | None | 7589 → **8324**（+735） | 9493 → 10350 | 930 → **0** |

- #1 後: description_only=1744 / retryable_partial=1744 / permanent_unique 324 → **417**（保持）/ DB 67.77 → 70.58 MB / raw_json=0 / body=0 / active=0 / 秘匿リーク無し。`11066 = 6719 + 3930 + 417`。
- #2 後: description_only=1780 / retryable_partial=1780 / permanent_unique 417 → **489**（保持）/ DB 70.58 → 73.3 MB / raw_json=0 / body=0 / active=0 / 秘匿リーク無し。`11066 = 7647 + 2930 + 489`。
- #3 後: description_only=1848 / retryable_partial=1848 / permanent_unique 489 → **578**（保持）/ DB 73.3 → 76.26 MB / raw_json=0 / body=0 / active=0 / 秘匿リーク無し。`11066 = 8558 + 1930 + 578`。
- #4 後: description_only=1904 / retryable_partial=1904 / permanent_unique 578 → **643**（保持）/ DB 76.26 → 79.48 MB / raw_json=0 / body=0 / active=0 / 秘匿リーク無し。`11066 = 9493 + 930 + 643`。
- final 後: description_only=2026 / retryable_partial=2026 / permanent_unique 643 → **716**（保持）/ DB 79.48 → 82.52 MB / raw_json=0 / body=0 / active=0 / 秘匿リーク無し。`11066 = 10350 + 0 + 716`。

#### Phase 7M 完了サマリ（eligible_metadata_missing = 0）

5 runs（1000×4 + 930）で **eligible metadata を全消化**。最終状態（11,066 liked）:

- **info_json complete = 8,324（75.2%）**、broad metadata = 10,350（93.5%）、description-only（retryable partial）= 2,026、permanent（private/deleted/unavailable、保持・未削除）= 716、**eligible_metadata_missing = 0**。
- 全 run STOP 無し（worst batch 最大 0.36）。yt-dlp 2026.06.9 + delay 3.0s+jitter で PO-token 無しでも安定。
- **body archive・full metadata（`--all --confirm`）・`--include-permanent` は未実行**。raw_json stored=0 / 秘匿リーク無しを全 run 維持。
- 残課題（任意・未着手）: description-only 2,026 件は `retry-metadata --retryable` で info_json へ格上げ可能（まず `--limit 100〜200` で効果確認）。

### description-only → info_json 格上げ（Phase 7N）

429 で `.description` だけ取得できた **description-only partial** を `retry-metadata --retryable`（permanent は対象外）で再取得し、**info_json complete へ格上げ**する。主指標は `info_json_complete_count` の増加。`metadata-run` と違い retry-metadata はバッチ/STOP ゲートを持たない（即 enqueue）ため、ratio / worst batch は完了後にジョブ実績から算出した。

#### retry 実測（yt-dlp 2026.06.9 + delay 3.0s + jitter、本体DLなし）

retry-metadata はバッチ/STOP ゲートを持たない（即 enqueue）。ratio / worst batch は **開始マーカー以降の metadata_only retry ジョブ**をDB集計（worst は finish 順 50 件毎の pseudo-batch）。

| run | queued | success | rate_limited | overall ratio | worst pseudo-batch(50) | info_json complete | description_only |
|---|---|---|---|---|---|---|---|
| retry 200 | 200 | 161 | 39 | **0.195** | **0.30** | 8324 → **8485**（+161） | 2026 → **1865**（−161） |
| retry 500 | 470 | 397 | 72 | **0.153** | **0.46** | 8485 → **8882**（+397） | 1865 → **1468**（−397） |
| retry final | 424 | 361 | 62 | **0.146** | **0.42** | 8882 → **9243**（+361） | 1468 → **1107**（−361） |

- **success がそのまま info_json 格上げ**（各 run の info_json 増分 = success ＝ 完備済み動画の再取得ゼロ）。broad metadata は 10,350 で不変（元から description＝broad のため、動くのは info_json complete のみ）。
- final 後: retryable_partial 1468 → **1107** / permanent **716**（保持・不変）/ DB 83.52 → 84.55 MB / raw_json=0 / body=0 / active=0 / 秘匿リーク無し。failures（attempts vs unique・累計）: private 1176/416、rate_limited 1119/**1116**、deleted 242/197、impersonation 206/206、unavailable 192/103、unknown 48/48。
- **retry の到達範囲**: `retryable_liked` は直近 1000 件の failed/partial を走査し、retry 回数 cap 未到達のもののみ対象。available は 491 → 470 → 424 → **380**（final 後）と緩やかに減るだけ（再失敗が cap 未到達のまま窓内に再浮上するため枯渇しない）。残り description-only 1,107 の cap 到達分は retry-metadata では格上げ不可。

#### Phase 7N サマリ（retry 3 passes・info_json 主指標）

- 3 passes（200 + 500 + 424）で **919 件の description-only を info_json complete へ格上げ**。**info_json complete 8,324 → 9,243（全11,066 中 83.5%）**、description-only 2,026 → 1,107。broad は 10,350 不変・permanent 716 保持・**eligible 0**。
- 全 pass で overall ratio < 0.2、worst pseudo-batch < 0.5（0.30 → 0.46 → 0.42。残るほど 429 しやすい難物）。yt-dlp 2026.06.9 + delay 3.0s+jitter で PO-token 無しでも安定。raw_json=0 / body=0 / 秘匿リーク無しを全 pass 維持。
- **残課題（任意・別フェーズ）**: ① 残り description-only 1,107 の更なる格上げは retry-cap 引き上げ + PO-token 設定が必要（retry の available は枯渇しないが diminishing returns・429 上昇）。② **body archive は未実行**（小規模・要ユーザー確認）。**full `--all --confirm` / `--include-permanent` は未実行、permanent 716 は保持・削除なし。**

### body archive 小規模テスト（Phase 8A）

本体動画DLの初回小規模テスト（`enqueue-archive --limit 5`、profile `video_compressed_1080p`）。あわせて body archive にも安全策を追加（コード変更）:
- **permanent 除外**: `enqueue-archive` / `plan-archive` も private/deleted/unavailable を既定除外（`--exclude-permanent` 既定 / `--include-permanent` で上書き・非推奨）。plan に `eligible missing body` / `permanent excluded` を表示。
- **info_json 完備を優先選定**: 完備（info_json あり）動画を先に archive（`prioritize_info_json`）。
- backend 457 tests / frontend 55 tests green、migration 追加なし（head `c3d4e5f6a7b8`）。

#### limit 5 実測（cookie 設定済み、本体DLあり）

| 指標 | 値 |
|---|---|
| attempted / success / partial / failed | **5 / 5 / 0 / 0** |
| body_saved | **0 → 5** |
| media_files（video） | 0 → **5** |
| 保存先 | `/archive/youtube/videos/<channel>/<id>/…mp4` ×5（**実DLサイズ計 約249 MB**） |
| DB size | 84.55 → **93.55 MB**（増分の大半は `comments` テーブル＝1080p profile が `--write-comments`。**本体動画は disk 保存・DB 非肥大**） |
| raw_json stored | **total 0**（維持） |
| failure 分類 | 新規 body 失敗なし（rate_limited / private / deleted / unavailable いずれも 0） |
| UI 再生 | media stream API が **HTTP 206 + Content-Range + Accept-Ranges**、`video/mp4`、MP4 `ftyp` 確認（シーク可） |
| active jobs | **0** |
| 秘匿リーク | **NONE OK**（progress / failure / secrets / video detail API に path/token/raw_json 無し。worker ログの cookie も `--cookies '******'`） |

- 観測: 一部動画で 1 回目の format 抽出が `Requested format is not available` となり、profile の **fallback format チェーンで再試行 → 5/5 成功**（保存・再生・media_files 整合に問題なし。format 1 回目の最適化は別途）。
- 判定: limit 5 成功・rate_limited 0 → **次は `--limit 10` を提案**（実行前に報告・停止）。

#### limit 10 実測（同 profile、本体DLあり）

| 指標 | 値 |
|---|---|
| attempted / success / partial / failed | **10 / 10 / 0 / 0** |
| body_saved | **5 → 15** |
| media_files（video） | 5 → **15** |
| 実DLサイズ（今回10件） | **約 1.03 GB**（disk 累計 15 本 ≈ 1.28 GB） |
| DB size | 93.55 → **107.07 MB**（+13.5 MB） |
| **comments table 増分** | 9.47 → **23.0 MB（+13.6 MB ≈ 1.36 MB/本）** — DB 増加はほぼ全てコメント。**本体動画は disk** |
| raw_json stored | **total 0** |
| 失敗分類 / format fallback | 新規失敗 0（rate_limited/private/deleted/unavailable 0）/ format fallback **1/10**（稀） |
| UI 再生 | Range **HTTP 206 + Content-Range + video/mp4 + ftyp** 確認（別動画でも seek 可） |
| active jobs / 秘匿リーク | **0** / **NONE OK** |
| 既存 body 除外 | `skipped_has_body=5`（前回5本を再DLしない）/ permanent excluded 716 / 選定10件すべて info_json 完備 |

- **スケール上の注意（要判断）**: DB 増加分はほぼ全て `comments` テーブル（≈1.36 MB/本）。全 11,066 本を本体アーカイブすると comments だけで **概算 ~15 GB** DB 肥大の可能性。大規模化前に **コメント保存を抑えた body profile**（`--write-comments` 無効 / `YTDLP_MAX_COMMENTS` 制限）を検討推奨。本体動画自体は disk 保存で DB を肥大させない。
- 判定: limit 10 成功・rate_limited 0・Range OK → **次は `--limit 30` が候補**（実行前に報告・停止）。ただし上記コメント肥大を踏まえ、**comments-light profile への切替**も選択肢。**full body archive・`--all --confirm`・`--include-permanent` は未実行、permanent 716 は保持・削除なし。**

### comments-light body profile + limit 30（Phase 8B）

8A で判明した「DB 肥大の主因＝コメント保存」を解消するため、**コメントを保存しない body profile を追加**（既存 profile は互換のため残置）。backend **462** / frontend **55** tests green、migration 追加なし（head `c3d4e5f6a7b8`、built-in profile は seed で upsert）。

- 新 profile **`video_compressed_1080p_light`**: `video_compressed_1080p` と同一（<=1080p mp4 + info_json/description/thumbnail/subtitles）だが **`--write-comments` を無効**。`enqueue-archive --profile video_compressed_1080p_light --limit N` で選択（plan/dry-run も profile 名を表示）。permanent 除外・info_json 優先・既存 body 除外は維持。

#### limit 30 実測（`video_compressed_1080p_light`、本体DLあり）

| 指標 | 値 |
|---|---|
| attempted / success / partial / failed | **30 / 30 / 0 / 0** |
| body_saved | **15 → 45** |
| media_files（video） | 15 → **45** |
| 実DLサイズ（今回30件） | **約 20.2 GB**（disk 累計 45 本 ≈ 21.5 GB。長尺中心で 1 本あたり大） |
| DB size | 107.07 → **107.12 MB（+0.05 MB のみ）** |
| **comments table 増分** | **23.0 → 23.0 MB（差分 0 バイト）** ← コメント保存無効が効いた |
| raw_json stored | **total 0** |
| 失敗分類 / format fallback | 新規失敗 0（rate_limited/private/deleted/unavailable 0）/ format fallback **1/30**（稀） |
| UI 再生 | Range **HTTP 206 + Content-Range + video/mp4 + ftyp**（seek 可） |
| active jobs / 秘匿リーク | **0** / **NONE OK** |
| 選定 | profile `video_compressed_1080p_light` / `skipped_has_body=15`（前回分除外）/ permanent excluded 716 / info_json 完備優先 |

- **comments-light の効果**: 30本DLで DB は **+0.05 MB のみ**（標準 profile は 10本で +13.5 MB）。本体動画は disk 保存で DB を肥大させない。全件 body archive 時の DB 肥大懸念を解消。
- **disk 容量の注意**: 本体は disk 増（今回30本で約20.2 GB。長尺が多いと 1 本数百MB〜）。大規模化では disk（NAS）容量の見積りが必要。
- 判定: limit 30 成功・rate_limited 0・Range OK・**comments 増分 0** → **次は `--limit 100` が候補**（実行前に報告・停止）。**full body archive・`--all --confirm`・`--include-permanent` は未実行、permanent 716 は保持・削除なし。**

#### limit 100 実測（`video_compressed_1080p_light`、本体DLあり）

per-run body 上限を 50 → **1000** に引き上げ（`LIKED_ARCHIVE_MAX_ENQUEUE_PER_RUN`、非secret。`--limit` が実制御）。disk 事前確認: `/archive` 空き **1.3 TB**（gate OK）。

| 指標 | 値 |
|---|---|
| attempted / success / partial / failed | **100 / 98 / 2 / 0** |
| body_saved | **45 → 143** |
| media_files（video） | 45 → **143** |
| 実DLサイズ（今回98本） | 約 **10.2 GB**（disk 累計 143 本 ≈ 31.7 GB） |
| DB size | 107.12 → **107.26 MB（+0.14 MB のみ）** |
| **comments table 増分** | **23617536 → 23617536 bytes（差分 0）** |
| raw_json stored | **total 0** |
| 失敗分類 | 新規 failed 0。partial 2（1=rate_limited、1=理由なし/サブ要素欠落）。video 本体は 98 本保存 |
| format fallback | **0** |
| 重複 video media_files | **0** |
| UI 再生 | Range **HTTP 206 + video/mp4 + Content-Range + ftyp**（730 MB 長尺でも seek 可） |
| active jobs / 秘匿リーク | **0** / **NONE OK** |

**run 中に host sleep → stack 停止が発生**（約11h）。復旧時: **Redis queue は永続**しており worker が drain 再開、**orphaned running job 12056 を検出 → 再 dispatch**。`--download-archive history.txt` により**重複DL・重複 media_files は発生せず**（12056 は再実行で status=success・video file 1 本のみ、DB 全体で重複 0）。

- 判定: limit 100 は **98/100 成功・partial 2（rate_limited 1）・comments 増分 0・重複 0・Range OK** → 次は `--limit 300` が候補。**ただし limit 300 前に Redis queue 永続化（AOF/RDB 明示）または orphan job 自動修復手順の導入を検討**（今回の host sleep 復旧は手動再 dispatch だった）。**full body archive・`--all --confirm`・`--include-permanent` は未実行、permanent 716 は保持・削除なし。**

### body archive runtime robustness（Phase 8C）

limit-100 中に host sleep でワーカーが停止し、DB 上 `running` のまま RQ から消えた orphan job が発生した（手動復旧）。大規模化前に **queue 永続化 + orphan 検出/修復**を実装（migration 追加なし、head `c3d4e5f6a7b8`）。

- **Redis queue 永続化**: `docker-compose` の redis を `--appendonly yes --appendfsync everysec` + named volume `redisdata:/data` に変更。**AOF によりコンテナ/スタック再起動・host sleep 後も RQ の queued/started ジョブが復元**される（従来はボリューム無しで消失リスク）。postgres(`pgdata`) と同様に data は volume 保持。
- **orphan 検出/修復 CLI**: `archiver jobs reconcile-orphans --dry-run`（既定）/ `--apply`（`--older-than-minutes 30`）。DB で `running`/`queued` だが **RQ の queue / StartedJobRegistry 等に存在しない**（＝ワーカー死亡で放置された）ジョブのみを orphan と判定。**RQ に居るジョブ・閾値より新しいジョブは絶対に触らない。Redis 不通時は何もしない**（安全側）。
  - `--apply`: orphan を安全に `queued` へ戻して RQ 再 enqueue。ただし **既に body(video)保存済みなら再 DL せず `success` に整合**（`--download-archive history.txt` も再 DL を抑止）。出力: `scanned / orphan_found / requeued / skipped_already_has_body / skipped_recent / skipped_rq_present / errors`。
- **duplicate 検査 CLI**: `archiver storage media-duplicates`（`video` メディアが 2 本以上ある動画を検出、あれば exit 2）。**再 dispatch で重複が生じていないことを検証**。
- **preflight 連携**: `system preflight` に `orphan_jobs` チェックを追加（orphan があれば **WARN**、自動修復はしない）。**再起動後の推奨手順**: ① `system preflight` → ② `jobs list` / `jobs reconcile-orphans --dry-run` → ③ 必要時のみ `--apply` → ④ `storage media-duplicates` で重複0を確認。
- 秘匿値・host パスは出力しない（既存方針を維持）。**Phase 8C では body archive は実行しない**。

### comments-light body archive limit 300（Phase 8D）

Phase 8C の堅牢化（AOF 永続 + orphan reconcile）下で `video_compressed_1080p_light` の 300 件本番テスト。disk 事前確認 **1.3 TB 空き**（gate OK）、実行前 orphan=0 / duplicate=0 / preflight PASS。

| 指標 | before → after |
|---|---|
| attempted / success / partial / failed | **300 / 298 / 2 / 0** |
| body_saved | **143 → 441** |
| media_files（video） | **143 → 441** |
| 実DLサイズ（今回298本） | 約 **53.9 GB**（mp4 累計 441 本 ≈ 82 GB） |
| disk 使用 | 328G → **381G**（空き 1.2 TB 維持） |
| DB size | 107.26 → **107.65 MB（+0.39 MB のみ）** |
| **comments table 増分** | **23617536 → 23617536 bytes（差分 0）** |
| raw_json stored | **total 0** |
| 失敗分類 | 新規 failed 0。partial 2（いずれも rate_limited）。private/deleted/unavailable 0 |
| format fallback | **1/300**（稀） |
| duplicate video media / orphan | **0 / 0**（完了後 dry-run も scanned=0） |
| UI 再生 | Range **HTTP 206 + video/mp4 + Content-Range + ftyp**（seek 可） |
| active jobs / 秘匿リーク | **0** / **NONE OK**（worker ログ cookie も `--cookies '******'`） |

- 本 run は **host sleep 発生せず**完走。298/300 成功（99.3%）、comments 増分 0、重複・orphan 0。
- 判定: 高成功率・rate_limited 低・comments 0・duplicate 0・orphan 0・Range OK → 次の候補は **limit 500 または limit 1000**（残り eligible body ≈ 9,900、disk は両方とも収容可）。ただし実行前に報告・停止。**full body archive・`--all --confirm`・`--include-permanent` は未実行、permanent 716 は保持・削除なし。**

### comments-light body archive limit 500（Phase 8E）

実行前 orphan=0 / duplicate=0 / preflight PASS / disk 1.3 TB 空き。`video_compressed_1080p_light` で 500 件。

| 指標 | before → after |
|---|---|
| attempted / success / partial / failed | **500 / 499 / 1 / 0** |
| body_saved | **441 → 940** |
| media_files（video） | **441 → 940** |
| 実DLサイズ（今回499本） | 約 **101 GB**（mp4 累計 940 本 ≈ 176 GB） |
| disk 使用 | 383G → **482G**（空き **1.1 TB** 維持） |
| DB size | 107.65 → **108.38 MB（+0.73 MB のみ）** |
| **comments table 増分** | **23617536 → 23617536 bytes（差分 0）** |
| raw_json stored | **total 0** |
| 失敗分類 | 新規 failed 0。partial 1（**private**、rate_limited ではない）。format fallback **1/500** |
| duplicate / orphan | **0 / 0**（完了後 dry-run scanned=0） |
| UI 再生 / 秘匿リーク | Range **206 video/mp4 ftyp** / **NONE OK** |

- **run 中に host sleep が複数回発生**したが、**Redis AOF 永続キューにより worker が自動で drain 再開**し、stuck orphan は生じず手動介入不要で完走（8B の手動復旧と対照的）。
- **reconcile 修正（8E で判明・適用）**: body-archive ジョブは `Job.rq_job_id` を保存しないため、旧実装（rq_job_id 照合）は**全 body job を誤って orphan 判定 → `--apply` で二重 enqueue する危険**があった。**RQ ジョブの args に埋まった DB job_id で照合**する方式へ修正（`reconcile.py`、regression test 追加、backend 470 tests green）。live 監視では常に手動 age チェックを用い、危険な `--apply` は未使用だったため実害なし。
- 判定: 499/500 成功（99.8%）・comments 0・duplicate 0・orphan 0・Range OK → 次候補 **limit 1000**（disk 概算 +200 GB、空き 1.1 TB で収容可）。実行前に報告・停止。**full body archive・`--all --confirm`・`--include-permanent` は未実行、permanent 716 は保持・削除なし。**

### comments-light body archive limit 1000（Phase 8F）

修正版 reconcile（args 照合）+ `_enqueue` の rq_job_id 保存を commit（`0b37533`）・デプロイ後に実行。**enqueue 直後に fixed reconcile を live 検証**: 999 active job が rq_job_id 保存済み・reconcile dry-run で `orphan_found=0 / skipped_rq_present=999`（旧実装なら 999 全部を誤検出していた）。

| 指標 | before → after |
|---|---|
| attempted / success / partial / failed | **1000 / 990 / 10 / 0** |
| body_saved | **940 → 1930** |
| media_files（video） | **940 → 1930** |
| 実DLサイズ（990本） | 約 **247 GB**（mp4 累計 1930 本 ≈ 406 GB） |
| disk 使用 | 481G → **718G**（空き **848 GB**） |
| DB size | 108.38 → **109.72 MB（+1.34 MB のみ）** |
| **comments table 増分** | **23617536 → 23617536 bytes（差分 0）** |
| raw_json stored | **total 0** |
| 失敗分類 | 新規 failed 0。partial 10（**private 1 / rate_limited 9**）。format fallback **1/1000** |
| duplicate / orphan | **0 / 0**（完了後 dry-run scanned=0） |
| UI Range / 秘匿リーク | **206 video/mp4 ftyp** / **NONE OK** |

- **run 中に host sleep が複数回発生（合計 ~18h）**したが、**Redis AOF 永続キューで worker が自然に drain 再開**し、毎回 running ジョブは fresh・stuck orphan 0・**手動 `--apply` 不要で完走**。fixed reconcile の誤検出も無し。
- **disk 注意（拡大の律速）**: 990本で **+237 GB（~250 MB/本、想定 ~200GB より大）**。**残り eligible body 8,420 件は概算 ~2.1 TB 必要**で、現空き 848 GB では全件は収まらない。次の `--limit 1000`（~250 GB → 空き ~600 GB）は可だが、その先の全件アーカイブには**保存先の増設（NAS/外付け）または低解像度 profile が必要**。
- 判定: 990/1000 成功（99%）・comments 0・duplicate 0・orphan 0・Range OK → 次候補は **limit 1000 継続**（disk 空き ≥500 GB を維持）。**limit 2000 は空きが ~350 GB に低下するため非推奨**。**full body archive・`--all --confirm`・`--include-permanent` は未実行、permanent 716 は保持・削除なし。**

---

## Phase 9A: 本番向け body archive 運用制御（production archive operation controls）

Phase 8F までで保存機能は検証済み（limit 5〜1000 を段階実行、latest limit 1000 = 990 success / 10 partial / 0 failed、body_saved 1930、Range 206、comments delta 0、raw_json 0、dup 0、orphan 0）。**開発環境での全件・大規模 DL は今後行いません**。本番で「必要な範囲だけを安全に回す」ための制御を追加しました。

### 1. 既定 body profile を本番向けに
- `BODY_ARCHIVE_DEFAULT_PROFILE`（既定 `video_compressed_1080p_light`）を追加。plan/enqueue/CLI/API/scheduler の body pass はこれを既定に使用。
- 旧 `video_compressed_1080p` は互換のため保持。**ただし `--write-comments` を含み DB `comments` テーブルが肥大するため、大規模運用では非推奨**（README/.env.example に明記）。

### 2. disk capacity guard
- `ARCHIVE_MIN_FREE_GB`（既定 500 GiB）を追加。enqueue 前に保存先の空きを確認し、**実行後に min-free を下回る（または既に下回っている）body 実行を拒否**（CLI は exit 2）。
- `plan-archive` / dry-run が **disk total/used/free・selected 件数・推定 DL サイズ・実行後推定 free・min-free 判定（blocked）** を表示。
- override は明示 flag `--allow-low-disk`（API は `allow_low_disk`）のみ。**空きが読めない時のみ guard 非適用**（誤検知で全拒否しない安全側）。

### 3. size estimator
- 保存済み `video` media_files の `filesize` から **avg / median / p90** を算出、**推定値は p90（保守的）**。データ不足時（既定 10 本未満）は固定値 `ARCHIVE_SIZE_ESTIMATE_FALLBACK_MB`（既定 300 MiB）。plan に「推定値・実サイズは動画尺で変動」と表示。

### 4. batch planning
- `plan-archive` が **requested limit / cap per run（`LIKED_ARCHIVE_MAX_ENQUEUE_PER_RUN`）/ disk-safe limit / recommended limit（+ 律速要因）/ remaining eligible body / selected 件数** を表示。

### 5. operations status（CLI + API）
- `archiver liked-videos ops-status` / `GET /api/liked-videos/operations`：**body_saved・remaining eligible body・active/queued/running・orphan dry-run・duplicate 件数・disk free・既定 body profile・comments テーブルサイズ・raw_json stored total** を一括表示（progress / failures / storage db-stats を統合。カウント/数値のみ、raw_json・パス・cookie は出力しない）。

### 6. UI
- Liked Videos 画面に **Body archive operations パネル**（参照専用）と、plan プレビューの **batch/disk 表示**、enqueue モーダルの `--allow-low-disk` トグル・disk block 表示を追加。**一括DLボタンは置かず**、必ず plan/dry-run を経由。

### 7. 本番運用手順（runbook）
1. `archiver system preflight`（worker build 一致 / cookies / **archive_disk_free** を確認）
2. disk check：`archiver liked-videos ops-status`（free ≥ min-free か）
3. `archiver liked-videos plan-archive --limit N`（disk-safe limit / blocked を確認）
4. small smoke test：`enqueue-archive --limit 1〜3`（本番投入時のみ）
5. staged batch：`enqueue-archive --limit N`（min-free を割らない範囲）
6. host sleep/restart 復旧：Redis AOF でキュー存続 → worker 自然再開。running が >30分 stuck の時だけ次へ
7. `archiver jobs reconcile-orphans --dry-run`（→ 必要時 `--apply`、保存済み body は再DLせず success 化）
8. `archiver storage media-duplicates`（重複 0 を確認）

**方針**: 開発環境では残り全件を DL しない。Phase 8F で保存機能は確認済みのため、以後の機能確認に全件テストは不要。本番投入時も **小規模 smoke test（limit 1〜3）で十分**。full body archive・`metadata-run --all --confirm`・`--include-permanent` は実行しない。permanent failure は保持（削除しない）。cookie / PO-token / visitor data / raw_json / host path はログ/API/UI に出さない。

---

## Phase 9B: 本番移行の準備（production deployment readiness）

開発環境から本番へ移す前の運用準備。**実動画DLは行わず**、既存の body_saved 1930 本でチェック系を検証。

### 1. production readiness check — `archiver system production-check` / `GET /api/system/production-check`
preflight + disk guard + Redis AOF + orphan/duplicate + raw_json + 既定profile + queue/env/dev設定 を **PASS/WARN/FAIL** で一括判定。FAIL があれば CLI は exit 1。**秘匿値・host path は非出力**。主なチェック:

| チェック | PASS | WARN | FAIL |
|---|---|---|---|
| schema_head / worker_build_match / cookies_file / secret_value_exposed | preflight OK | — | preflight FAIL |
| archive_disk_free | free ≥ min-free | 読取不可 | **free < min-free** |
| redis_aof_persistence | `appendonly=yes` | 読取不可 | **`appendonly=no`** |
| orphan_jobs | 0 | RQ 読取不可 | **orphan > 0** |
| duplicate_video_media | 0 | — | **> 0** |
| raw_json_stored | **total 0** | — | > 0 |
| default_body_profile | comments-light | **comments 有** | — |
| active_jobs_idle / queue_health / required_env / dev_only_settings | idle・worker有・server DB・危険設定なし | 各該当時 | worker 0 |

### 2. environment separation — `.env.production.example`
本番テンプレート（必須/推奨/任意を明記）。secrets/cookies は**値を書かず**置き場所のみ説明（cookies は `./secrets/` に置き `/secrets` に read-only マウント、`COOKIES_FILE=/secrets/cookies.txt`）。**dev-only/危険設定**（`ARCHIVE_MIN_FREE_GB=0`・`--allow-low-disk`常用・`LOG_LEVEL=DEBUG`・`CORS_ALLOW_ORIGINS=*`・comments profile大規模・raw_json保存）は非推奨として明記し、production-check が WARN/FAIL で検出。既存 `.env.example` は開発フル参照として維持。

### 3. backup / restore runbook
**Backup（cutover 前）**:
1. **Postgres**: `docker compose exec -T postgres pg_dump -U archiver archiver | gzip > backup/db_$(date +%F).sql.gz`
2. **Redis AOF**: `redisdata` ボリュームを停止中にコピー（`docker run --rm -v youtube_archiver_redisdata:/data -v "$PWD/backup:/b" alpine tar czf /b/redis_$(date +%F).tgz -C /data .`）
3. **archive ディレクトリ**: `ARCHIVE_HOST_PATH` を rsync（`rsync -a --info=progress2 <archive>/ <dest>/`）
4. **cookies/secrets**: `./secrets` と `/config` を**オフラインで**安全にバックアップ（git・共有ストレージに置かない）

**Restore 後の確認**（この順で実行）:
1. `docker compose up -d`（migrate 自動適用）
2. `archiver system preflight`（worker build 一致）
3. `archiver system production-check`（PASS または WARN 内容が妥当）
4. `archiver jobs reconcile-orphans --dry-run`
5. `archiver storage media-duplicates`（0）
6. `archiver system archive-check`（missing 0）
7. UI で任意動画の **Range 206** 再生確認

### 4. archive root migration check — `archiver system archive-check` / `GET /api/system/archive-check`
保存先を NAS/外付けへ切替える前後で、DB の video `media_files` が実ファイルに解決するか検証：**DB件数 / checked / files present / missing / duplicate / disk free** を表示。欠損は **youtube id のみ**で報告（**path/host path は非出力**）。missing>0 または duplicate>0 で exit 2。

### 5. smoke test procedure（本番移行後のみ・READMEに記載）
1. `archiver system production-check`（PASS）
2. `archiver liked-videos plan-archive --limit 3`（disk-safe / blocked 確認）
3. `archiver liked-videos enqueue-archive --limit 3 --profile video_compressed_1080p_light`
4. 確認: **Range 206**・comments delta **0**・raw_json **0**・duplicate **0**・orphan **0**
5. これ以上の大規模DLは production-check + disk guard を通した上で**手動判断**（全件DLはしない）

### 6. UI
Body archive operations パネル（参照専用）に **production readiness（PASS/WARN/FAIL + counts + 非PASSチェック + backup リマインダ）** を追加。危険な実行ボタンは無し、secret/path 非出力。

**禁止（継続）**: 開発環境で残り全件DL / full body archive / `metadata-run --all --confirm` / `--include-permanent` / permanent 削除 / secrets・cookies・raw_json・host path の出力・git混入。

---

## Phase 9C: 本番アクセス制御とセキュリティ強化（access control）

管理 UI/API を外部公開しても、未認証ユーザーが archive 操作・設定参照・ジョブ操作を行えないようにする。**既定は `APP_ENV=development` / `AUTH_MODE=disabled`** なので開発運用は無変更。認証は `AUTH_MODE` を設定した時だけ有効化。

### 認証モード（`AUTH_MODE`）
- **disabled**（開発のみ）: 認証なし。production-check は `APP_ENV=production` で **FAIL**。
- **local**: アプリ内管理者ログイン。パスワードは **scrypt**（`hashlib.scrypt`、stdlib）でハッシュ化。**scrypt は memory-hard KDF で、Argon2id とは別方式**です（Argon2id の代替として、外部依存を増やさず適切な cost/salt を用いて採用: N=16384・r=8・p=1、ランダム 16B salt、dklen=32、定数時間比較）。加えて **HMAC 署名 session cookie**（HttpOnly / Secure / SameSite=strict / 有効期限）、ログイン失敗は一律 generic メッセージ、per-IP rate limit あり。
- **trusted_proxy**: Cloudflare Access 等の**認証済みリバースプロキシ**の identity header を利用。**`TRUSTED_PROXY_CIDRS` に一致する proxy 送信元からのみ** header を信用し、**直接クライアントは同名 header を偽装しても拒否**。`ALLOWED_ADMIN_EMAILS` の許可リストのみ通過（未設定は fail-closed）。

### 保護範囲（backend 強制・pure-ASGI middleware）
- `/api/*`（liked-videos、production-check/archive-check、jobs、enqueue/retry/import/scheduler、settings/secrets-status、videos 詳細、**media stream(Range)** を含む全 54 mutating + 参照）は認証必須。
- 公開は `/health`・`/api/health`・`/api/auth/*`・SPA static のみ。`/docs`・`/openapi.json` は認証時保護。
- **media Range**: 認証済みは 206 を維持、未認証は 401（ファイルサイズ/パスを漏らさない）。Cloudflare 等経由の Range 再生も同一 origin cookie で維持。
- UI でボタンを隠すだけでなく **middleware で必ず拒否**。

### CSRF / CORS / cookie / proxy
- **CSRF**: cookie-session の mutating（POST/PUT/PATCH/DELETE）に **double-submit**（`X-CSRF-Token` ヘッダ = 読み取り可能な CSRF cookie）+ **Origin/Referer** 検査。GET は状態変更なし。trusted_proxy の header 認証は cookie 非依存なので CSRF 対象外。
- **CORS**: `APP_ENV=production` + wildcard は production-check **FAIL**。local 認証（credentials）では wildcard を使わず明示 origin + 明示ヘッダ。
- **cookie**: production では **Secure 必須**（false は FAIL）。SameSite=strict、Path=/、明確な有効期限、logout で無効化。
- **proxy/HTTPS**: `X-Forwarded-*` は **trusted proxy からのみ**信用（`TRUST_FORWARDED_HEADERS`）。直接 HTTP 公開は production-check で警告。

### production-check 追加項目（PASS/WARN/FAIL・値/パス非出力）
`app_env` / `auth_mode`（prod+disabled→FAIL）/ `session_secret_readable` / `local_password_hash_readable`（prod+local+不足→FAIL）/ `secure_cookie`（prod+insecure→FAIL）/ `cors_policy`（prod+wildcard→FAIL）/ `trusted_proxy_config`（CIDR/allowlist 不足→FAIL）/ `mutating_api_protection`（prod+disabled→FAIL）。dev+disabled は PASS。

### 本番セットアップ手順（runbook）
1. secrets 生成（値は git/README/log/API/UI に出さない・`./secrets` に置き `/secrets` に read-only マウント）:
   ```
   archiver auth gen-session-secret > ./secrets/session_secret
   archiver auth hash-password      > ./secrets/admin_password_hash   # 対話でパスワード入力。ハッシュのみ出力
   chmod 600 ./secrets/*
   ```
2. `.env`（`.env.production.example` 参照）: `APP_ENV=production`、`AUTH_MODE=local`（or `trusted_proxy`）、`SESSION_COOKIE_SECURE=true`、`CORS_ALLOW_ORIGINS=https://<your-domain>`、secret file パス。
3. **port 8000 を直接インターネットに公開しない**。リバースプロキシ（Cloudflare Tunnel / nginx）配下に置き、`8000` は **localhost バインドまたは firewall で保護**。
4. `archiver system production-check`（PASS を確認）→ `archiver auth status`（boolean 確認）。
5. **Cloudflare Access モード**: `AUTH_MODE=trusted_proxy`、`TRUSTED_PROXY_AUTH_HEADER=CF-Access-Authenticated-User-Email`、`TRUSTED_PROXY_CIDRS=<Cloudflare ranges>`、`ALLOWED_ADMIN_EMAILS=<you>`。
6. **ログイン障害復旧**: パスワード忘れ → `auth hash-password` で再生成し `admin_password_hash` を差し替え、web を再起動。全ロックアウト時は一時的に `AUTH_MODE=disabled`（localhost 限定）で復旧作業。
7. **secret rotation**: `session_secret` 差し替え（既存 session 全失効=再ログイン）/ `admin_password_hash` 差し替え。**backup には session/password secret を含める（ただし git には入れない）**。

### UI
未認証は login 画面（local）またはプロキシ認証案内（trusted_proxy）。401 で login へ戻る。認証済みは identity（最小）+ logout。**production readiness / operations は認証後のみ表示**、危険操作は plan/dry-run 前提を維持、secret/path/raw_json 非表示。

**禁止（継続）**: 実動画DL / full body archive / `--all --confirm` / `--include-permanent` / permanent 削除 / volume 削除 / plaintext password・session secret を git/README/log/API/UI に出す / cookie・PO-token・raw_json・host path の露出。

---

## Phase 9D: 本番 ingress とリリース強化（ingress and release hardening）

port 8000 を直接公開せず、Cloudflare Tunnel / reverse proxy 配下で安全に運用する構成を標準化。

### 1. production compose template — `docker-compose.production.example.yml`（追跡・secret/host path 無し）
```
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d
```
- **web は `127.0.0.1:${WEB_HOST_PORT:-8000}:8000`（loopback のみ）**。Tunnel を同一 Docker network に置く場合は web の `ports:` を丸ごと削除（host publish 無し）。
- **postgres/redis は host publish しない**（内部ネットワーク限定）。worker/scheduler も非公開。restart=unless-stopped、web に healthcheck。volume（pgdata/redisdata/archive）は base 継承・維持。**`down -v` 禁止**。

### 2. compose 静的検査 — `archiver system compose-check --file cfg.json [--production]`
アプリはコンテナ内から host port bind を判定できないため、`docker compose config --format json > cfg.json` を静的検査: **web が 0.0.0.0 で publish / postgres・redis が publish** → WARN（`--production` で FAIL）。

### 3. ingress security（Cloudflare Tunnel / reverse proxy runbook）
- public DNS → Cloudflare（HTTPS 終端・Access 認証）→ Tunnel → **web の loopback**。direct port は firewall/localhost bind で遮断。
- **`AUTH_MODE=trusted_proxy`**: `TRUSTED_PROXY_AUTH_HEADER=CF-Access-Authenticated-User-Email`、`TRUSTED_PROXY_CIDRS=<Cloudflare ranges>`、`ALLOWED_ADMIN_EMAILS=<you>`。identity header は **trusted CIDR の socket peer からのみ信用**、direct client の同名 header は拒否、**`X-Forwarded-For` 左端を無条件に信用しない**（socket peer で判定）。Cloudflare 以外の任意 proxy は信頼しない。
- Range 再生は同一 origin cookie で維持。WebSocket は不要。proxy 側で **body-size 制限・timeout** を設定。

### 4. Redis-backed login rate limit
IP 単位・window+max・**Redis TTL・複数 worker 共有**・web 再起動後も短時間維持。キーは **HMAC 匿名化**（生 IP/ password/ email を含めない）。応答は generic + **Retry-After**。**Redis 不通時: production は fail-closed / development は in-memory fallback**（`LOGIN_RATE_LIMIT_BACKEND=auto|redis|memory`）。IP/secret は通常 log に出さない。

### 5. session hardening
署名 session に iat/exp/**jti nonce**。**future timestamp 拒否・malformed 拒否・定数時間署名比較**。rotation: **`SESSION_SECRET_PREVIOUS_FILE` を verification-only で一時受理**（新規は現行 secret で署名）。
- **重要（正確な記載）**: server-side session store は導入していないため、**logout は browser cookie 削除であり、盗まれた token を即時失効はできません**。`session_secret` を rotate すれば**全 session を失効**できます。`SESSION_MAX_AGE_SECONDS` は短め（既定 8h）に保ってください。

### 6. security headers
認証・全応答に **CSP**（SPA/media Range/API を壊さない最小: `script-src 'self'`, `style-src 'self' 'unsafe-inline'`, `media-src 'self'`, `frame-ancestors 'none'`; inline script は許可しない）、**X-Content-Type-Options: nosniff / Referrer-Policy / X-Frame-Options: DENY / Permissions-Policy**。`/api/auth`・`/api/system`・`/api/settings` に **Cache-Control: no-store**。**HSTS は production のみ**（dev/HTTP では付与しない）。

### 7. allowed hosts / origin boundary
- **`ALLOWED_HOSTS`**: 不正 Host header は 400 拒否（空=任意, dev）。**`CSRF_TRUSTED_ORIGINS` は CORS origins と別**・wildcard 禁止。
- production-check: **prod で ALLOWED_HOSTS 無 → FAIL / prod+local で CSRF trusted origins 無 → FAIL / wildcard → FAIL**。

### 8. release-check — `archiver system release-check`（FAIL 時 exit 1・secret/path 非出力）
production-check + **archive presence（全 media_files が実在）+ migration status（code head==DB head）+ backup freshness marker + build version** を統合。auth/CORS/CSRF/allowed hosts/secure cookie/Redis AOF/rate-limit backend/disk/worker build も含む。

### 9. deployment / rollback / backup scripts（非破壊・`set -euo pipefail`・secret 非 echo・`down -v` 禁止）
- `scripts/backup.sh`: Postgres `pg_dump` + Redis `BGREWRITEAOF` + backup marker touch（archive/redisdata は out-of-band で別途）。
- `scripts/deploy.sh`（`DRY_RUN=1` 可）: backup → build → migrate → recreate → health wait → preflight → release-check。
- `scripts/rollback.sh <tag>`: web/worker/scheduler を旧 image tag へ。**DB schema は自動 rollback しない**（migration 済みなら pg_dump から復元してから旧コードを動かす）。archive/volume は不変。

### 10. secret 生成 / rotation（値を git/README/log/API/UI に出さない）
```
archiver auth gen-session-secret > ./secrets/session_secret          # rotate = 全session失効
archiver auth hash-password      > ./secrets/admin_password_hash      # stdin/対話・plaintext は history に残さない
chmod 600 ./secrets/*
```
`auth hash-password` は対話入力（plaintext は shell history に残らない）。stdout に出す場合のリスクを理解し可能ならファイルへ直接。temporary secret file は検証後に削除。**session secret rotation で既存 session は失効**、password 更新は hash 差し替え + web 再起動。

### 11. UI
production readiness パネルに **auth mode / app_env / secure cookie / allowed hosts / CSRF origins / rate-limit backend（PASS/WARN/FAIL）** と **release-check（オンデマンド実行・summary）** を追加。危険操作ボタンは無し、**secret 値・proxy CIDR 全文・email allowlist 全文・host path は非表示**。

**禁止（継続）**: 実動画DL / full body archive / `--all --confirm` / `--include-permanent` / permanent 削除 / volume 削除・`down -v` / plaintext password・session secret を git/README/log/API/UI に出す / cookie・PO-token・raw_json・host path・生 IP の露出。

---

## Phase 9E: 監査証跡と運用可観測性（audit trail and observability）

管理操作・認証イベント・運用障害を後から追跡でき、異常を早期検知できるようにする。**secret/password/cookie/token/生 IP/生 email/host path/raw_json は監査・log・metrics に出さない**。

### 1. append-only 監査モデル + 改ざん検出
`audit_events` テーブル（migration `d4e5f6a7b8c9`。**downgrade は監査証跡を破棄するので本番では不可**）: occurred_at / event_type / category / severity / outcome / actor_kind / **actor_id_hash / client_id_hash（HMAC 擬似ID、生 email/IP は保存しない）** / request_id / correlation_id / resource_type / resource_id / action / reason_code / **metadata_json（allowlist + 値redaction）** / previous_hash / event_hash。ORM の update/delete を event listener で拒否（**append-only**）。
- **hash chain**: `event_hash = HMAC(AUDIT_HMAC_KEY, previous_hash + 正規化内容)`（key 無時は SHA-256 の unsigned chain）。`archiver audit verify`（改ざん時 `valid=false / first_invalid_event_id` のみ表示・機密は出さない、exit 1）。retention 削除時は `audit_checkpoints` に境界を記録し chain 検証を継続。
- **注意（正確な記載）**: hash chain は **DB 管理者による完全な改ざんを絶対防止するものではなく**、改ざんの**検出を補助**する仕組みです。

### 1.1 署名ライフサイクル（Phase 9E.1）
署名方式の切替（unsigned→signed）・key rotation・restore を **正規の運用変更**として扱い、**改ざんと区別**できるようにする（migration `e5f6a7b8c9d0`）。
- **event 署名 metadata**: 各 event に `chain_version` / `signature_scheme`（`sha256_unsigned` | `hmac_sha256`）/ `signing_key_id`（**短い ID のみ・key 値/path は保存しない**）。v2 canonical はこれらを含む（既存 event は legacy=chain_version1/unsigned/legacy として保持、**再署名しない**）。
- **署名境界 checkpoint**: `audit_checkpoints.checkpoint_type` = `retention` / `signing_enabled` / `key_rotated` / `restore_boundary`。previous/next event id・hash・previous/next **signing_key_id**・`checkpoint_hash`（改ざん検出対象）を保持。**checkpoint 無しの scheme/key 変化は invalid**。
- **key 構成**: `AUDIT_HMAC_KEY_FILE` + `AUDIT_HMAC_KEY_ID`（current・署名用）、`AUDIT_HMAC_PREVIOUS_KEY_FILES/IDS`（**verification-only**）。id/file 件数不一致・重複 id は FAIL。current key のみで新規署名、previous では署名しない。
- **pseudonymization key の分離**: `AUDIT_PSEUDONYM_KEY_FILE`（署名 key と別）。**署名 key を rotate しても actor/client 擬似 ID が変化しない**。生 email/IP は引き続き保存禁止。production で未設定は WARN。
- **verify semantics**: legacy unsigned segment / signing_enabled / current signed / key_rotated / previous-key signed / retention / event 順序 / previous_hash / event_hash / scheme / key ID / **missing verification key** / **unexpected regime change** を判定。出力: `valid` / `valid_with_warnings` / `checked_count` / `segment_count` / `checkpoint_count` / `current_signing_key_id` / `unsigned_event_count` / `first_invalid_event_id` / `failure_reason_code`（**機密/key/path 非出力**）。
- **CLI**（boundary/rotation は **dry-run 既定・`--apply` で適用**、key は secret file 経由・stdout に値/path を出さない）: `archiver audit signing-status` / `establish-signing-boundary [--type signing_enabled|restore_boundary] [--apply]` / `rotate-key [--apply]` / `verify`。
- **env policy**: development は unsigned 許容（WARN）・`AUDIT_ALLOW_LEGACY_UNSIGNED_PREFIX=true` 可。production は current key 無=FAIL・key id 無=FAIL・checkpoint 無しの scheme 変更=FAIL・missing verification key=FAIL・unsigned prefix は既定 FAIL（policy 許可時のみ WARN）。
- **retention 整合**: cleanup は **署名/restore boundary の参照 event を跨いで削除しない**（跨ぐ場合は prune を止め verify 可能性を維持）。security category は通常より長期保持。
- **production/release-check**: `audit_signature_scheme` / `audit_current_key_configured` / `audit_current_key_id_configured` / `audit_key_config` / `audit_previous_keys_available` / `audit_pseudonym_key` / `audit_chain_valid` / `audit_signing_boundary_valid` / `audit_unexpected_regime_changes` / `audit_missing_verification_keys` / `audit_unsigned_event_count`。判定順序: **chain 検証 → check 確定 → `*_check_run` event を append**（append は best-effort・本体判定を壊さない）。
- **restore/mixed の扱い**: DB restore や dev の署名方式変更で mixed chain になった場合、`establish-signing-boundary --type restore_boundary --apply` で**現 head を attested baseline として明示境界**を作り（既存 event は削除/再署名せず）、以後を current key で署名 → verify は legacy prefix + 明示 checkpoint + signed suffix を `valid` / `valid_with_warnings` として検証。

### 2. 監査対象
認証: `login_success/login_failure/login_rate_limited/logout/csrf_rejected/host_rejected/trusted_proxy_rejected/unauthorized`。危険操作: `archive_plan_requested/archive_enqueue_created/archive_enqueue_blocked/production_check_run/release_check_run/archive_check_run/deploy_started/deploy_succeeded/deploy_failed/backup_started/backup_completed/audit_retention_cleanup` 等。**通常の一覧/progress poll・operation panel の定期 poll は監査対象外**（DB 肥大回避）。

### 3. CLI / API（認証必須・admin・削除/編集なし）
- CLI: `archiver audit list|show <id>|verify|stats|export [--since]|cleanup|log-op --event ...`。
- API: `GET /api/audit/events`（pagination + event_type/category/severity/outcome/time/request_id filter）・`/events/{id}`・`/stats`・`/verify`・`/export`（JSONL・`AUDIT_MAX_EXPORT_EVENTS` cap・同じ redaction）。**update/delete API なし**。

### 4. request / correlation ID + structured logging
middleware が **request_id を生成**（妥当な `X-Request-ID` のみ採用、制御文字/長すぎは再生成）し **`X-Request-ID` を応答**。`X-Correlation-ID` も採用可。**structured JSON logging**（`STRUCTURED_LOGGING`）+ 共通 **redaction**（password/token/cookie/secret/hash/email/IP/host path をマスク、`redact_text`、tests で検証）。

### 5. metrics + health 分離
- **`GET /api/system/metrics`**（Prometheus text・**認証必須**・counts/gauges のみ・identity/video/channel/full URL/path label なし）: jobs_active/queued/running・worker_heartbeat_age・orphan/duplicate・archive_disk_free_bytes・body_saved/remaining・redis_available・audit_events_total・audit_chain_valid・auth_login_success/failure/rate_limited/csrf_rejected（24h）等。
- **`/health/live`**（公開・process のみ、内部情報なし）と **`/health/ready`**（DB/Redis/disk、未認証には詳細理由を出さず 503）。ready FAIL でも live は 200。

### 6. production-check / release-check 統合（値/パス非出力）
`audit_enabled`・`audit_hmac_key`（prod で無→**FAIL**）・`audit_chain_valid`（invalid→**FAIL**）・`audit_retention`・`metrics_protected`（prod で無認証→FAIL）・`structured_logging`・`readiness_endpoint`・**`https_readiness`**（HSTS ヘッダ有無ではなく **`PUBLIC_BASE_URL` が https か**で判定、prod で http→FAIL）・`password_hash_algorithm`（scrypt は algorithm/cost/salt/digest を保持し将来 rehash 検出可）。

### 7. alert 条件（通知基盤は追加せず条件を定義）
| 条件 | severity | 推奨対応 |
|---|---|---|
| archive disk free < min-free | critical | batch 縮小 / 保存先増設（disk guard 参照）|
| worker heartbeat stale / redis unavailable | critical | worker/redis 復旧、reconcile-orphans |
| orphan > 0 / duplicate > 0 / raw_json > 0 | warning/critical | reconcile-orphans / media-duplicates / import 見直し |
| backup stale / production-check・release-check FAIL | warning | backup 実行 / 該当 FAIL 修正 |
| auth failure・rate-limit・CSRF rejection spike | warning | 監査 `audit list --category auth`、必要なら遮断 |
| **audit chain invalid** | critical | 改ざん疑い → 証拠保全（下記 runbook）|
| job failure率上昇 / queue が減らない | warning | jobs 調査、worker/queue 確認 |

### 8. incident response runbook（証拠保全: volume/logs を削除しない・`down -v` 禁止・secret を ticket/chat に貼らない）
- **credential leak 疑い**: `session_secret` rotate（`SESSION_SECRET_PREVIOUS_FILE` で無停止）→ 全 session 失効。`admin_password_hash` 再生成 + web 再起動。監査で `login_success` を確認。
- **trusted proxy 誤設定**: `TRUSTED_PROXY_CIDRS`/`ALLOWED_ADMIN_EMAILS` 確認、direct port 遮断。
- **unauthorized login 多発**: `audit list --category auth --severity warning`、rate limit/ firewall。
- **audit chain invalid**: `audit verify` で `first_invalid_event_id` 確認 → **DB/volume/logs を保全**（削除禁止）→ backup と突合。
- **Redis loss / queue stuck / orphan / duplicate / disk low / archive file missing / DB restore / rollback**: 各々 preflight・reconcile-orphans・media-duplicates・archive-check・pg_dump 復元・`scripts/rollback.sh`（Phase 9D）。**archive/volume は触らない**。

### 9. deploy/backup script 統合
`scripts/deploy.sh`・`backup.sh` は `archiver audit log-op --event deploy_started|deploy_succeeded|deploy_failed|backup_started|backup_completed|backup_failed`（container 内 CLI・**best-effort、audit 失敗で本体を壊さない**、専用 token を増やさない）。

### 10. UI
Liked Videos 画面に **read-only Audit & observability パネル**（audit chain status・severity 別 counts・recent events・**request/correlation ID 検索**・重大度 filter）。**編集/削除ボタンなし**、識別子は擬似ハッシュのみ・**secret/CIDR全文/email全文/host path 非表示**。

**禁止（継続）**: 実動画DL / full body archive / `--all --confirm` / `--include-permanent` / permanent 削除 / volume 削除・`down -v` / 監査イベントの通常 API 削除・編集 / plaintext identity・IP・password・session secret・cookie・token・host path・raw_json を audit/log/metrics/API/UI に出す。

---

## Phase 9F: 本番バックアップ整合性と災害復旧受入（backup integrity / DR acceptance）

現環境に一切影響を与えずに、「**バックアップから本当に復旧できること**」を検証可能・再現可能にする。**migration head は `e5f6a7b8c9d0` のまま**（新規 migration なし）。

### 1. restore_boundary の break-glass 強化
`archiver audit establish-signing-boundary --type restore_boundary` は **--reason-code 必須**（service 層でも拒否）+ apply には **`--confirm-restore` 必須**。plan / 監査イベントに **pre-boundary verify 結果**（`pre_boundary_chain_valid` / `failure_reason_code`）を証拠として埋め込み、適用イベントは `severity=warning`。**CLI 専用**（`/api/audit` は read-only のまま、boundary 作成 API なし — tests で担保）。**通常の chain 破損の隠蔽に使わない**（証拠保全が先）。

### 2. backup manifest + 検証 — `archiver backup ...` / `scripts/verify-backup.sh`
- `scripts/backup.sh` が pg_dump 後に **`<dump>.manifest.json`**（`sha256` / `size_bytes` / `schema_head` / `created_at`・**basename のみ**）を生成し、**summary** を `BACKUP_MANIFEST_SUMMARY_FILE`（/config 配下）へ書く。pre-9F image では警告してスキップ（初回 9F deploy を塞がない）。
- `archiver backup verify-manifest --manifest F [--write-marker]` — sha256/size を再計算して照合（改ざん/欠損/サイズ違いを `sha256_mismatch` / `artifact_missing` / `size_mismatch` で区別、mismatch は exit 1）。`--write-marker` で `BACKUP_VERIFIED_MARKER_FILE` を touch。ホスト側は **`./scripts/verify-backup.sh`**（最新 manifest を自動選択・audit log-op `backup_verified|backup_verify_failed`）。
- manifest の `artifact` は **パス区切り拒否**（traversal 対策）。出力は basename/counts のみ。

#### 2.1 backup-set manifest v2（Phase 9F.1）
manifest は DB dump 単体でなく **復旧に必要な backup set 全体**を一意に識別する（`manifest_version=2`。v1 も後方互換で verify 可・`legacy_manifest_v1` warning）。フィールド: `backup_id`（`bk-<UTC>-<rand>`）/ `created_at` / `completed` / `app_version` / `build_id` / `schema_head` / dump の `artifact`+`size_bytes`+`sha256` / **`active_jobs_at_backup`** / **`audit_head_event_id`+`audit_head_event_hash`**（監査 chain 先頭）/ **`archive_manifest`**（sibling archive manifest の `artifact`+`sha256`+件数で結合）/ **`redis_recovery_mode=empty_redis_then_reconcile`** / `encrypted` / **`integrity`**（manifest 自身の canonical hash。`BACKUP_MANIFEST_HMAC_KEY_FILE` があれば `hmac_sha256`、無ければ `sha256`。**key 値/path は出力しない**）。
- `verify-manifest` は dump sha256/size に加え **manifest integrity・completed・archive manifest 結合・監査 head（DB 到達時）** を検証し、`manifest_integrity_mismatch` / `integrity_key_missing` / `incomplete_backup` / `archive_manifest_missing|mismatch` / `audit_head_mismatch` を区別（mismatch は exit 1）。`active_jobs_at_backup>0` は WARN。
- `scripts/backup.sh` は **archive manifest → backup manifest（`--archive-manifest` で結合）** の順で生成し、DB dump と archive manifest の**組合せ不一致**を後で検出できるようにする。archive 実体は dump に含めない（サイズ snapshot のみ）。
- **禁止（manifest に出さない）**: host path / secret path / password / cookie / token / 生 identity / 生 IP・email / raw_json（tests で担保）。

### 3. archive manifest — `archiver backup archive-manifest / verify-archive`
DB の video media_files を **相対パス + サイズ（+ 任意で先頭 N 件の sha256、`--hash-limit`）** でスナップショット化し、`verify-archive` が現状と照合（missing / size_mismatch / hash_mismatch を **public youtube id のみ**で報告、mismatch は exit 1）。82GB 級の全 hash は高コストのため **既定はサイズのみ**。bitrot / 誤削除 / ランサム検知の運用ツール。

### 4. release-check / backup-readiness 統合（WARN 系）
`release-check` に **`backup_manifest`**（summary の存在/整合）・**`backup_verified`**（`BACKUP_VERIFY_MAX_AGE_HOURS` 以内か）・**`restore_rehearsal`**（`RESTORE_REHEARSAL_MAX_AGE_DAYS` 以内か）を追加(未設定/未実施/期限切れは **WARN** — deploy は塞がない)。`GET /api/system/backup-readiness`（認証必須・read-only）+ Liked Videos 画面の **Backup & recovery readiness パネル**（basename/counts/経過時間のみ・操作ボタンなし）。

### 5. migration rehearsal — `./scripts/migration-rehearsal.sh`（実行済み・PASS）
**一時 postgres コンテナ**（`ya-migrehearsal-<ts>-<rand>`・127.0.0.1 の ephemeral port・匿名 volume・compose 不使用）に対して:
- **fresh**: 空 DB → `alembic upgrade head` → **`alembic check`**（model と migration の drift 検出）→ audit テーブル存在 / `audit_checkpoints.up_to_event_id`・`reason` nullable / unsigned→signed boundary → **署名 chain verify**。
- **upgrade**: `alembic upgrade d4e5f6a7b8c9`（pre-9E.1）→ **代表 legacy 監査 chain を raw SQL 投入**（v1 canonical / sha256_unsigned）→ `upgrade head` → **legacy event 全件保持（hash 不変・再署名/削除なし）**・server_default（chain_version=1 / sha256_unsigned / `legacy`）→ boundary → verify（legacy prefix は warning）。
- 手動 ALTER なし・**`alembic downgrade` は実行しない**（監査 migration の本番 downgrade は監査証跡を破壊するため**引き続き禁止**）。機械可読レポートを `backups/rehearsals/migration-{fresh,upgrade}-<ts>.json` に出力。
- この rehearsal が検出した **model↔migration drift 3 件を修正**（`AuditCheckpoint.occurred_at`/`reason` の nullable を migration に一致、`jobs.retry_of_job_id` の FK を migration 通り削除 — migration `6279ed580c1a` は意図的に FK なし）。

### 6. isolated restore rehearsal — `./scripts/restore-rehearsal.sh`（実行済み・PASS）
最新（or 指定）の pg_dump を **専用一時 Compose project** に復元し、DR 受入を機械可読レポート化する。
- **隔離**: 専用テンプレート `docker-compose.restore-rehearsal.yml`（tracked・secret/実パスなし）。`-p ya-rehearsal-<ts>-<rand>` を**全 compose 呼び出しに明示**、volume 名は **`rehearsal_pgdata` / `rehearsal_redisdata`**（本番と別名 = 二重分離）、bind は **mktemp の `REHEARSAL_ROOT` 配下のみ**、web は `127.0.0.1:<ephemeral>` のみ公開、**scheduler サービスなし**（何も enqueue できない）。一時 secrets（session/admin hash/署名キー k1→k2/pseudonym key）を生成し実 `./secrets` は不使用。
- **現 project への操作は read-only のみ**（`exec -T postgres pg_dump/psql` と事後の `docker volume inspect` / `ps`）。`down -v` は **teardown() 内・一時 project 限定**（名前 regex を再検証してから）。teardown 後に **現 project の volume 存置・コンテナ稼働を検証**しレポートに記録。
- **受入項目（41 項目）**: manifest 検証 → DB restore → schema head → migrate no-op → live/ready → **audit verify（restore 直後の legacy chain）→ restore_boundary break-glass 実演**（reason/confirm ゲート → apply → verify）→ auth（誤 PW 401 / login / 未認証 401）→ CSRF（token なし 403 / token+Origin で通過）→ protected metrics → preflight / production-check（**FAIL 0**）/ release-check → **fixture 方式 archive-check**（上位 K 件にダミーファイル生成・`--limit K` で 0 missing、full check は「archive 未接続で missing = 全体-K」を **expected fail** として記録）→ **media Range 206**（fixture のみ・実 DL なし）→ duplicate / orphan dry-run → **key rotation**（k1→k2 + previous 検証）→ **previous 欠損 = missing_verification_key で FAIL / 復元で PASS / current 誤値で FAIL / 復元で PASS**→ **pseudonym 分離**（署名 rotation で不変・pseudonym key 交換で変化・chain は有効のまま）→ **jobs 件数不変**（download 0 件）。
- 結果は `backups/rehearsals/restore-acceptance-<ts>.json`（項目ごとに `status` + **`expected_failure`** を区別、想定外 fail=0 のときだけ **成功 marker** `RESTORE_REHEARSAL_MARKER_FILE`（既定 `./data/config/last_restore_rehearsal`）を touch → release-check / readiness パネルに反映）。レポートは host path を含むと生成拒否。

### 7. 鍵復旧マトリクス（rehearsal + pytest で検証済み）
| シナリオ | 結果 |
|---|---|
| current key のみ | PASS |
| current + 必要な previous key | PASS |
| 必要な previous key 欠損 | **missing_verification_key で FAIL**（検知が正しい挙動） |
| previous key 復元後 | PASS |
| current key の値が誤り | **event_hash_mismatch で FAIL** |
| restore_boundary | dry-run 既定 + `--reason-code` + `--apply --confirm-restore` 必須 |

**pseudonym key は署名キーと独立**: 署名 rotation で擬似 ID は不変。pseudonym key を失うと**過去との相関だけが切れる**（chain 検証は影響なし）— 復旧優先度の判断材料として明記。

### 8. Redis AOF / reconcile 復旧ポリシー（Phase 9F で明文化）
**DB restore 時に Redis AOF は復元しない**（キュー状態は DB と必ずズレるため）。空の Redis で起動 → `archiver jobs reconcile-orphans`（dry-run → 必要なら `--apply`）で DB 側の queued/running を再整合するのが正。rehearsal はこのポリシー通り（空 Redis + orphan dry-run）を受入項目に含む。AOF volume のバックアップは「ホスト障害からの**再起動**」用であり、「過去時点への**復元**」には使わない。

### 8.1 deploy worker convergence（Phase 9F.2）
`deploy.sh` が web/worker を新 image へ recreate した直後、**停止した旧 worker の RQ heartbeat 登録が Redis に TTL（`WORKER_HEARTBEAT_TTL_SECONDS=90`）まで残る**。preflight の `worker_build_match` は stale 登録も build 比較するため、実コンテナが正常でも一時的に FAIL していた。9F.2 では recreate 後・preflight 前に **収束待ち**を挟む:
- `archiver system worker-convergence [--json] [--wait --timeout N --poll S]` — 現 web build に**現行 build の active worker が 1 以上**あり、**build 不一致の worker（stale/fresh 問わず）が 0**なら ready（exit 0）。未収束 exit 1 / Redis 不通 exit 2。出力は**件数のみ**（worker id / host / redis url / secret / path は出さない）。
- `deploy.sh` は固定 sleep ではなく**この CLI を poll**（`WORKER_CONVERGENCE_TIMEOUT_SECONDS=150` / `WORKER_CONVERGENCE_POLL_SECONDS=5`）。stale 登録は TTL 失効まで**待つ**が、**現行 worker 不在**や**恒常的な build mismatch（fresh な旧 build worker が残り続ける）**は timeout で正しく失敗（`deploy_failed` は**timeout 時のみ**記録）。収束後に preflight / release-check を**一度だけ**実行するので、手動 preflight 再実行は不要。

### 9. 運用（runbook 追記）
```bash
./scripts/backup.sh                      # dump + manifest + summary + marker
./scripts/verify-backup.sh               # 最新 manifest を照合 + verified marker
./scripts/migration-rehearsal.sh         # fresh + pre-9E.1 upgrade（一時 postgres）
./scripts/restore-rehearsal.sh           # 隔離 restore + DR 受入（一時 project）
./scripts/deploy.sh                      # 非破壊 deploy（収束待ち込み・volume 保持）
archiver system worker-convergence --json  # 収束状態（件数のみ・deploy が poll）
archiver backup status                   # マニフェスト/照合/リハーサルの経過時間
archiver backup archive-manifest --out /backups/archive-manifest.json [--hash-limit N]
archiver backup verify-archive --manifest /backups/archive-manifest.json
```
- `backups/`（dump / manifest / rehearsal レポート）は **`.gitignore` 済み** — commit しない。
- rehearsal レポートに host path / secret は入らない（builder が拒否）。
- 定期推奨: backup=毎deploy前+週次 / verify=週次 / restore rehearsal=四半期（`RESTORE_REHEARSAL_MAX_AGE_DAYS=90` の WARN と連動）。

**禁止（継続）**: 実動画DL / full body archive / `metadata-run --all --confirm` / `--include-permanent` / permanent 削除 / **現 project の volume 削除・`docker compose down -v`** / 監査イベントの書換・削除・再署名 / `.env`・secret・cookie・dump・レポートの commit / password・hash・session secret・token・生 identity・host path・raw_json の出力。rehearsal の `down -v` は **`ya-rehearsal-*` 一時 project のみ**（静的ガードテストで担保）。

---

## Phase 10A: リリース候補とサプライチェーン強化（release candidate & supply chain）

本番へ投入する **source / frontend / container image / migration / 設定テンプレート**を一意に識別し、同一 commit から**再構築・照合**できるようにする。SBOM・脆弱性検査・image identity を release manifest に束ねる。**migration head は `e5f6a7b8c9d0` のまま**。

### 1. version identity — `archiver system version` / `GET /api/system/version`
`app_version`（`APP_VERSION`、既定は package `__version__`、release で `vX.Y.Z`）/ `git_commit`（`APP_GIT_COMMIT`）/ **`git_tree_clean`**（`APP_GIT_TREE_CLEAN`、無ければ git から算出）/ `build_id` / `build_timestamp` / `schema_head` / **`frontend_build_id`**（`frontend/dist` の content hash）/ `image_digest`（`APP_IMAGE_DIGEST`）。**host path / repo path / username / secret / 生 env は出さない**。**production + dirty build は release-check FAIL**。

### 2. dependency reproducibility
- **Python（Phase 10A.1: 真の hash-pinned lock）**: `requirements.lock` を **唯一の runtime 依存 lock** とし、**direct＋transitive を全て `==` 固定＋各行に `--hash=sha256:`** を付す。生成は **`scripts/gen-python-lock.py`**（既知良好 release image の `pip freeze` を入力に PyPI から hash を取得。**再解決・upgrade しない**）。Dockerfile は **`pip install --require-hashes -r requirements.lock`**（lock 改ざん/欠落/hash 不一致で build FAIL）→ project 本体のみ `pip install --no-deps -e .`。**`requirements.txt` は human-edit の direct 入力**として残し lock とは明確に区別。dev/test 依存（pytest/httpx）は `pyproject` の `optional-dependencies`。**pyproject direct ⊆ lock / 全行 pin+hash / transitive 固定 / Dockerfile が非 lock を本番 install に使わない**を test で検証。lock 検証: `pip install --require-hashes` で既知良好 image と **package 差分 0** を確認済み。
- **Frontend**: `package-lock.json` を唯一の基準に、Dockerfile を **`npm ci`（厳格）**（lockfile 不整合で build FAIL）。**lockfile は自動更新しない**（`npm install`/`update` を build から排除）。
- lock 更新手順（README）: `docker compose exec web pip freeze > /tmp/f.txt` → `scripts/gen-python-lock.py /tmp/f.txt requirements.lock`（依存 upgrade は別 PR で明示的に）。`requirements.lock`/`package-lock.json` は **git 追跡**（release artifact ではない）。

### 3. base image pinning（Phase 10A.1: release で digest 必須）
Dockerfile の base image を **ARG 化**（`BASE_PYTHON_IMAGE` / `BASE_NODE_IMAGE`、stage 内で再宣言し LABEL の `base.name` に反映）。`scripts/build-release.sh` は **build 開始時に base image の RepoDigest を解決し `python:3.12-slim@sha256:…` を build arg として渡す**（FROM が digest を使用）。**digest 解決不能なら `RELEASE_REQUIRE_DIGEST=1`（既定）で release を FAIL**。manifest に **requested tag / resolved digest ref / actual image id** を記録し、verify は digest-pin 済みかを照合。**digest はコードに推測で焼き込まない**（実行時に docker から解決）。apt `--no-install-recommends`+cache 削除、pip `PIP_NO_CACHE_DIR=1`。**非root 化は監査の上 root 維持**（host bind mount 所有権のため。将来 UID mapping と再検討する旨を Dockerfile 明記）。OCI labels 付与、private repo URL 非焼込。

### 4. release manifest — `archiver release create-manifest / verify-manifest / status`
versioned + canonical integrity（SHA-256、`RELEASE_MANIFEST_HMAC_KEY_FILE` があれば HMAC。**key 値/path は出さない**）。項目: `release_id` / `app_version` / `git_commit` / `git_tree_clean` / `build_id` / `build_timestamp` / `schema_head` / `backend_test_count` / `frontend_test_count` / `frontend_build_id` / **python/frontend lock sha256 / Dockerfile sha256 / compose template sha256 / migration dir sha256** / per-service **image name/id/digest/build_id** / base image refs / **SBOM 識別子** / **脆弱性 scan 識別子** / `backup_manifest_version` / **migration rehearsal**（fresh/upgrade 結果 + report hash + 実行時刻） / release-check summary / `completed` / `integrity`。verify は integrity・completed・lock/Dockerfile/compose/migration hash を現ツリーと照合し、schema head と **全 service image の build_id 一致**を検査（mismatch は exit 1）。

### 5. SBOM / 脆弱性検査（未導入時は偽 PASS にしない）
- **SBOM**: `scripts/build-release.sh` が `docker scout sbom`（syft ベース）で web image の **SPDX JSON**（Python/npm/OS packages）を生成し hash を manifest へ。ツール未導入なら**明示的に unavailable**（案内表示）。**正体不明のバイナリを自動 download・実行しない**。SBOM/scan は **release artifact（`release/` は `.gitignore`）** で、git には commit しない。
- **脆弱性 scan（Phase 10A.1: 実 scan 完了）**: `aquasec/trivy` image（image id で実行、`<none>` tag 依存を回避）を **事前取得した DB cache volume `${TRIVY_DB_VOLUME:-trivy_db_cache}` + `--skip-db-update`** でオフライン実 scan。DB 準備は `docker run --rm -v trivy_db_cache:/root/.cache/ <trivy> image --download-db-only`。**scanner の version・image id/digest・DB `UpdatedAt`・severity 別件数・status（`pass`/`warn`/`fail`/`unavailable`）** を manifest へ記録。**DB 未 cache/失敗/timeout は `unavailable`**（偽 PASS 禁止、operator action を表示）。production policy: **scanner unavailable→FAIL**（`RELEASE_SCANNER_UNAVAILABLE_POLICY`）/ **DB が `RELEASE_VULN_DB_MAX_AGE_DAYS`（既定7日）超→FAIL** / **未承認 CRITICAL（`RELEASE_MAX_CRITICAL_VULNERABILITIES` 超）→FAIL**。ignore は理由・期限・参照必須（既定空）。**自動修正・依存 upgrade・自動 ignore はしない**。

### 5.1 release manifest 真正性（Phase 10A.1）
canonical SHA-256 は改ざん**検出**用。**production release は HMAC 署名を必須**（`RELEASE_MANIFEST_HMAC_KEY_FILE`、**backup/audit の署名 key とは別**、key 値/path 非出力）。key 未設定の production release は release-check `release_manifest_authenticated`=FAIL。verify は wrong/missing key を `manifest_integrity_mismatch`/`integrity_key_missing` で FAIL。development は SHA-256 を WARN 許容。

### 6. provenance / build identity
build_id を web API / worker heartbeat / scheduler / frontend bundle / audit deploy event / backup manifest / release manifest / **OCI container labels** に一貫して埋め込む。release manifest は **web/worker/scheduler/migrate が同一 build_id** であることを検証（`docker inspect` + 各 image で `build_id()` を実行）。

### 7. release-check / release-readiness 統合
`release-check` に追加（development は未設定を WARN/PASS 許容、production は厳格）: `git_tree_clean`（prod+dirty→FAIL）・`application_version`（prod で `0.0.0-dev`→FAIL）・`schema_head_captured`・`release_manifest`（integrity+completed）・`service_image_build_match`（不一致→FAIL）・`image_digests_captured`・`sbom_present`（prod 無→FAIL）・`vulnerability_scan`（unavailable→policy で WARN/FAIL、CRITICAL 超→FAIL）。**Phase 10A.1 追加**: `python_lock_exact`・`python_lock_hashes_valid`・`installed_packages_match_lock`（実 install が lock と一致）・`base_images_digest_pinned`・`base_images_match_manifest`・`vulnerability_scan_completed`・`vulnerability_db_fresh`・`release_manifest_authenticated`（prod は HMAC 必須）。**production はいずれか未達で FAIL、development は lock/digest 不備・scan unavailable・SHA-256 のみを WARN**。read-only `GET /api/system/release-readiness` + Liked Videos 画面の **Release information パネル**（version・commit/build 短縮・SBOM/scan status + severity・release-check summary。**repo path / registry credential / host path / secret / scanner 生 command / deploy・依存更新ボタンなし**）。

### 8. 運用（runbook）
```bash
./scripts/build-release.sh vX.Y.Z   # clean→guard→tests→build→inspect→SBOM→scan→manifest→verify→release-check→marker
./scripts/verify-release.sh         # 最新 release manifest を現ツリーと照合（read-only）
archiver release status             # release manifest summary（件数・status のみ）
archiver release verify-manifest --manifest release/<ts>/release-manifest.json
```
- `release/`（manifest / SBOM / scan report / ログ）は **`.gitignore` 済み**。deploy は**別操作**（`scripts/deploy.sh`）で、build-release は**サービスを recreate せず volume も触らない**。
- **禁止（継続）**: 実動画DL / full archive / `metadata-run --all --confirm` / `--include-permanent` / permanent 削除 / **現 project の volume 削除・`down -v`** / 依存の無断 upgrade / 脆弱性の自動 ignore / 外部バイナリの無断 download・実行 / release・SBOM・scan artifact や `.env`・secret・cookie の commit / secret・path・生 identity の API/UI/report 露出。

---

## Phase 10B: 脆弱性triageと統制されたremediation（vulnerability triage & controlled remediation）

Phase 10A.1のreal scanで検出したCRITICAL/HIGHを**正確に分類**し、**最小変更**で修正するか**明示的に受入判断**できる状態にする。**依存の無差別upgradeや無断ignoreはしない**。

### 1. triage — `archiver release triage-scan` / `app/services/vuln_triage.py`
Trivy JSONを解析し、**PURLで分類**（`os` / `python` / `npm` / `binary`(=wheel等にvendorされたlib) / `application`）。**Trivyがdebian OS targetに入れていても、`pkg:rpm/almalinux/…` のようなPURLはbundled binary**として区別する（例: psycopg2-binary wheel内の`pcre2`はOS pcre2（既にパッチ済）とは別物）。CVE重複集約・fixed/no-fix・severity別集計。host path/secretは出さず、**package pathは落とす**。`triage-scan`は時限exceptionを評価し、未承認CRITICALが残れば**exit 1**（buildが gate できる）。

### 2. exception policy — `vulnerability-exceptions.yml`（repo追跡・operator承認制）
修正不能・実質到達不能なCVEだけ、**期限付きで明示受入**できる。必須項目: `vulnerability_id` / `package` / `installed_version` / `reason` / `reachability_assessment` / `compensating_control` / `approved_by` / `approved_at` / `expires_at` / `tracking_reference`。**期限なし・理由なしは無効**、**期限切れはrelease-check FAIL**、**CRITICALの自動ignoreなし**、package/version変更で再評価（stale判定）。secret/個人情報/private URLは禁止。**テンプレートは空**で、operator承認なしにexceptionは追加しない。

### 3. scanner provenance（Phase 10B.2で厳密化）
`triage-scan`がscanner（Trivy）の version / **image ID（`.Id`=full `sha256:…`）** / **registry RepoDigest（`.RepoDigests[0]`）** / source / operator検証状態を記録し manifestへ。**image IDとRepoDigestを厳密に区別し、RepoDigestを合成しない**。status: **`digest_pinned`**（実RepoDigestあり）/ **`local_image_id_verified`**（RepoDigestなし＋full image ID＋operator確認）/ **`unverified`**（それ以外。短縮image IDや合成digestは拒否してここへ）。production は`digest_pinned`/`local_image_id_verified`のみPASS、**`unverified`は単独PASSにしない**（`scanner_provenance_verified`）。詳細は Phase 10B.2 を参照。

### 4. remediation report — `archiver release remediation-report`
before/after 2つのTrivy JSONを diff し、CRITICAL/HIGHの added/removed/unchanged・severity前後・overall status・**canonical integrity（SHA-256 or HMAC）** を機械可読で出力（host path/secretなし）。

### 5. OS remediation（Phase 10B.1）
`CVE-2026-40393`（mesa/GL、Debian fix `25.0.7-2+deb13u1`）を **targeted `apt-get install --only-upgrade`**（**blanket `apt-get upgrade`ではない**）で適用。ffmpeg/deno/yt-dlp等は不変。**残りのCRITICAL（libglib2.0 / libmbedcrypto16×2 / libxml2 / perl-base×3）はDebianに修正が存在せず**、operator exception対象として追跡（自動patchしない）。pcre2 3件は psycopg2-binary vendored lib（OS pcre2は既にパッチ済）。

### 6. release-check gates（production）
`critical_vulnerabilities`（**未承認CRITICAL>0→FAIL**）/ `high_vulnerabilities`（policy: warn=件数把握 / fail）/ `scanner_provenance_verified` / `vulnerability_report_integrity` / `vulnerability_exceptions_valid`（期限切れ/無効→FAIL）/ `vulnerability_db_fresh` / `base_image_remediation_status` / `dependency_remediation_status`。development は未達をWARN。

**禁止（継続）**: 依存の無差別upgrade / `npm audit fix --force` / 脆弱性の無断ignore / **期限なしexception** / scanner unavailableの偽PASS / floating base tagへの後退 / hash lock解除 / release・scan・exception artifactの不用意なcommit。

### Phase 10B.2: bundled-library除去とscanner provenance厳密化（bundled-library elimination & scan provenance closure）

`-binary` manylinux wheelは自己完結の共有lib群（libpq/libssl/**libpcre2 10.32**…）をvendorし、そのpcre2がTrivyで`pkg:rpm/almalinux/pcre2@10.32`として**CRITICAL 3件（CVE-2022-1586 / CVE-2022-1587 / CVE-2025-58050）**に計上されていた。**source版psycopg2**へ切替え、`_psycopg.so`を**system libpq5 → system libpcre2-8-0（パッチ済 10.46）**へリンクさせvendored pcre2を排除する（exceptionは追加しない）。

- **lock正規再生成**: `gen-python-lock.py`で`psycopg2-binary==2.9.12`→`psycopg2==2.9.12`（sdist hash `1dedb1c7…`）へ。**他61パッケージは不変**（無差別upgradeなし、`--require-hashes`維持）。
- **Docker多段（builder/runtime分離）**: 専用`wheelbuild` stage（`gcc`/`libc6-dev`/`libpq-dev`）で `pip wheel --require-hashes --no-binary psycopg2`（**全hash検証＋sourceコンパイル**、`SOURCE_DATE_EPOCH`でtimestamp固定）。**注意（10B.3で実証）**: wheelは**bit-for-bit reproducibleではない** — pipがランダムtemp dirでコンパイルするため `_psycopg.so` のGNU build-id（→wheel sha256）がbuild毎に変動する。ただしこれは**metadataのみ**で、コード・DT_NEEDED（libpq.so.5/libc.so.6）・脆弱性除去結果（system-libリンク、vendored pcre2なし）は不変。runtimeは**`libpq5`のみ**導入し、**closed wheelhouseから`--no-index`でoffline install**。**gcc/make/libpq-dev/headerはruntimeに残さない**。built-wheelのsha256を `make-runtime-lock.py` でruntime lockへ反映し**`--require-hashes`をruntime installでも維持**（hash lockは解除しない）。built-wheelのsha256はmanifestへ記録。build時に**vendored libs不在をassert**（`psycopg2-binary`混入でbuild FAIL）。
- **runtime link検証**: 変更後imageで `psycopg2_binary.libs/` **不在** / **pcre2 10.32不在** / `_psycopg.so`が**system libpq5**へlink / **compiler不在**を確認。
- **SBOM fallback**: docker scoutがoffline hang/失敗時、**Trivyのoffline cacheでCycloneDX SBOM**を生成（scout→trivy→syftの順、各段timeout）。tool/version/format/**sha256**をmanifestへ。生成物は**host path/secret leak scan**（`archiver release scan-artifact-leaks`）で検査し、leak時は破棄。**SBOM missingはproduction FAIL**（偽PASSなし）。
- **apt再現性**: runtimeの`dpkg-query`一覧＋**sha256**をmanifestへ記録（同一base digestでもapt更新でbuild結果が変わることを検出可能に）。mesa/ffmpeg/libpq5の実versionを明示。snapshot repo未導入のため **`apt_packages_pinned=recorded_unpinned`（release-check WARN）** ＝「**依存/baseはfixed、apt transactionはnot fully reproducible**」と正直に表示（**floating aptを「完全再現可能」と報告しない**）。

**禁止（10B.2追加）**: compilerをruntimeへ残す / scanner RepoDigestの合成 / SBOM unavailableの偽PASS / hash lock解除 / `psycopg2-binary`への復帰（source buildの再監査なしに）。

### Phase 10B.3: 残存OS CVEの到達可能性と exception decision dossier（reachability & decision dossier）

残る**7件のCRITICAL（全て実Debian trixie OS・no-fix）**について、**有効なexceptionを追加せず**、operatorがCVE単位で承認/拒否/保留できる**判断材料（dossier）**を用意する。`vulnerability-exceptions.yml`は空のまま。

- **canonical CVE inventory**（`app/services/vuln_inventory.py` + `archive release cve-inventory`）: rc2/rc3/rc4の生Trivy JSONを **version非依存の`(cve,package)`キー**で横断集約し、first/last/removed release・current_status・evidence hash・**canonical integrity（SHA-256/HMAC）**を記録。
- **libxml2不整合の訂正**: 10Bで`CVE-2026-6653`(libxml2)がremovedと報告されたが、**生JSONではrc2/rc3/rc4の全てで継続してCRITICAL**。10B.1のapt更新でversionが`deb13u2→deb13u3`にbumpし、remediation diffが同一CVEをremoved+added両方に計上した**`package_version_reclassified`**アーティファクト（net効果ゼロ）。真の10B削減(14→10)は**mesa CVE-2026-40393×4のみ**。`reconcile_remediation`がこれを自動検出。
- **primary-source**（Debian Security Tracker）: 7件全て trixie Vulnerable・no-fix・"Minor issue"。libxml2(UAF DoS)/glib(GDBus introspection DoS)/mbedtls×2(TLS resumption・FFDH、fix in sid)/perl×3(regex/Archive::Tar、8376は32bit限定)。
- **runtime reachability**（ldd/`/proc/self/maps`実測）: **Python web/APIプロセスは4パッケージのいずれもロードしない**。libxml2/mbedcrypto/glibは**ffmpeg/ffprobeサブプロセスのみ**がリンク → libxml2=`potentially_reachable`（DASH/字幕XML解析）、mbedtls=`potentially_reachable`（SRT限定、YouTubeはHTTPS）、glib=`not_reachable_with_evidence`（GDBus未使用）。perl×3=`not_reachable_with_evidence`（perl runtime未起動、8376は32bit限定で当環境arm64に非該当）。
- **package除去/service分離の評価**（本phaseでは分割**未実施**、効果と複雑性のみ報告）: libxml2/mbedcrypto/glibは**ffmpeg依存**で、ffmpegは動画archiveに必須 → 除去不可。perl-baseはDebian baseパッケージ（install-time）で安全な除去は困難。**ただしffmpegを実際に使うのはworkerのみ** — web/migrate/schedulerをffmpeg無しの別flavorに分ければ、それら3イメージから libxml2/mbedcrypto/glib（4 CVE）を排除可能（workerには残る、perl-baseは全flavorに残る）。トレードオフ: Dockerfile多flavor化・flavor毎のscan/manifest・イメージ分岐の複雑性。→ operator判断事項として記録。
- **wheel再現性の実証**: psycopg2 wheelを**独立clean builderで2回build**して比較 → **bit-for-bit一致せず**。差分要因は`_psycopg.so`の**GNU build-id**（pipがランダムtemp dirでコンパイルするため）。**DT_NEEDED（libpq.so.5/libc.so.6）・コード・脆弱性除去結果は不変**。「決定的」表現をDockerfile/READMEで訂正済。runtime lockが実build-wheelのsha256を捕捉するため`--require-hashes`は維持。
- **scanner provenance決定記録**: Trivy image full ID `sha256:a8ca29078522…`、**RepoDigestのsha256が.Idと一致＝genuineなregistry manifest digestではない** → 合成同等として拒否 → **`unverified`維持**（承認は捏造しない）。runbook: registry到達時に本物の`.RepoDigests[0]`取得で`digest_pinned`、またはoperatorが`.Id`を公式値と照合の上`RELEASE_SCANNER_OPERATOR_VERIFIED=1`で`local_image_id_verified`。
- **exception PROPOSALS（非active）**: `vulnerability-exception-proposals.yml`（**承認欄`approved_by`/`approved_at`/`approval_reference`は空**、`meta.active: false`）。各CVEに reachability/evidence/risk/compensating_controls/recommended_decision（`exception_candidate`×4=glib+perl×3、`wait_for_upstream`×3=libxml2+mbedtls×2）/proposed_max_duration_days（**固定期限は決めず推奨日数のみ**）/tracking/evidence_hash。**release-checkはこれをactive exceptionとして数えない**（active源は`vulnerability-exceptions.yml`のみ）。
- **decision dossier**: `docs/vulnerability-decision-dossier.{json,md}`（機械可読＋人間可読、per-CVE表＋operator選択肢、canonical/dossier integrity付き）。`archive release decision-dossier`で生成。
- **release-check gates（10B.3追加）**: `vulnerability_inventory_consistent`（integrity＋文書化された不整合）/ `vulnerability_reachability_complete` / `vulnerability_decision_dossier_valid`（integrity mismatch/承認済proposal→FAIL）/ `scanner_operator_approval_present`（unverified→WARN、捏造しない）/ `wheel_reproducibility_status`（not_bit_reproducible→WARN）。**`critical_vulnerabilities`は7 unapproved CRITICALのままFAIL維持**、`scanner_provenance_verified`もunverifiedでFAIL維持。

**禁止（10B.3追加）**: active exceptionの追加 / approval情報の捏造 / 期限なしexception / fixあるCVEのignore / exploit code実行 / scanner RepoDigest合成 / proposalをactive扱い。

---

## Phase 11A: ローカル単独利用の製品受入とUI安定化（local single-user product acceptance）

operator判断により、残存する**7件のCRITICAL（全て実Debian OS・no-fix）はローカル単独利用の範囲で既知リスクとして受容**し、製品・UI品質を優先する。この判断は限定条件下での開発継続であり、**リスク解消ではない**（[ADR](docs/decisions/phase-11-local-single-user-risk-acceptance.md)）。

- **release-check FAILは維持** — コード/policy/結果をPASSへ書き換えない。production では 7 CRITICAL + unverified scanner で FAIL のまま。active exception も追加しない。
- **security posture の正直な表示**: read-only `GET /api/system/release-readiness` に `security_posture`（operating_mode / **known_critical_accepted** / production_ready / active_exceptions / dossier参照）を追加。Release information パネルに **「local single-user (dev) ／ 7 known CRITICAL — accepted (local) ／ not production-ready ／ 0 active exceptions」** を簡潔表示（**CRITICAL 7件を隠さず**、accepted-risk と production-blocked を区別、画面全体を赤で占有しない）。この summary は**表示専用でrelease-check結果を変更しない**。
- **UI stabilization（既存1930件データで検証、実DLなし）**: 全route（Dashboard / Videos / Video detail + player / Jobs / Job detail / Settings / Liked videos / Collections / Search / NotFound）を実操作。media は HTTP Range **206** seek・missing は **404**、失敗jobは分類付きdiagnostic、secretは全maskで**leakなし**、console error **ゼロ**。
- **修正**: header の陳腐な "Phase 5A" → "local single-user"／Search scope checkbox の `aria-label`／**SPA `index.html` に `Cache-Control: no-cache`**（content-hash付きassetは長期キャッシュ可のまま、deploy後に旧UIが残らないよう修正。P0=0・主要P1修正済）。

**禁止（11A）**: release-check の PASS 化・結果改変 / security status の隠蔽 / active exception 追加 / production-ready と称する / 匿名・第三者への操作権限や無制限のinternet露出 / 実動画DL・volume削除・`down -v` / secret・path・raw identity の露出。

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
# --- Phase 6D: 大容量 benchmark / job 化 / progress / cancel ---
archiver takeout benchmark PATH --kind liked_videos|watch_history|search_history|all [--limit N] [--dry-run]
archiver takeout import-liked-videos PATH --limit N --job [--dry-run] [--now]   # background job
archiver takeout import-watch-history PATH --limit N --job
archiver takeout import-search-history PATH --limit N --job
archiver takeout sessions progress SESSION_ID
archiver takeout sessions cancel SESSION_ID
# --- Phase 6E: no-raw-json / db-stats / benchmark-large / retention / safe-large ---
archiver storage db-stats                              # DB 件数 + 概算サイズ + raw_json 実 blob 数
archiver takeout benchmark-large PATH [--include-search]   # liked+watch 一括 dry-run benchmark
archiver takeout import-liked-videos PATH --no-raw-json [--job --limit N]   # raw blob 非保存
archiver takeout import-watch-history PATH --safe-large [--limit N] [--apply]   # job+no-raw-json+dry-run 既定
archiver takeout import-all PATH --no-raw-json
archiver takeout sessions cleanup --keep-last N [--older-than-days D] --dry-run   # session 行のみ剪定（job/data は不可侵）
archiver takeout sessions cleanup --keep-last N --apply
# --- Phase 6F: build/preflight / import-large / verify / auto-cleanup ---
archiver system build-info                             # app_version / build_id / schema_head
archiver system preflight                              # DB/Redis/alembic/web=worker build 一致（stale で exit 1）
archiver takeout preflight-large PATH [--kind ...]     # 大容量 import 前チェック（dry-run）
archiver takeout import-large PATH --kind watch_history [--limit N] [--apply]   # 既定 dry-run+no-raw-json+job, 自動 preflight
archiver takeout verify-import SESSION_ID | --latest [--kind ...]   # import 後検査 + 漏洩 grep
archiver takeout sessions cleanup-auto --dry-run | --apply          # 設定ベースの session 剪定
archiver takeout sessions cleanup-status               # auto cleanup 設定 + 直近結果
# --- Phase 6G: staged production import + operation report ---
archiver takeout import-staged PATH --kind watch_history [--apply] [--allow-full] [--max-stage N] [--raw-json] [--no-job]
archiver takeout import-report --latest | SESSION_ID | --kind watch_history --recent 10
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

# --- Phase 9F: backup 整合性 / DR ---
archiver backup write-manifest --artifact /backups/db-<ts>.sql.gz [--schema-head REV] [--summary-file F]
archiver backup verify-manifest --manifest /backups/db-<ts>.sql.gz.manifest.json [--write-marker]
archiver backup archive-manifest --out F [--hash-limit N]   # media 実在+サイズ(+hash)のスナップショット
archiver backup verify-archive --manifest F [--no-hashes]   # 現状と照合（public id のみ報告）
archiver backup status                                       # backup/verify/rehearsal の経過時間
archiver audit establish-signing-boundary --type restore_boundary \
  --reason-code db_restore --apply --confirm-restore         # break-glass（dry-run 既定）
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
| GET | `/api/takeout/import-sessions` | import 履歴一覧（件数のみ・パス/raw_json 非保存。6D で job_id/parser/eps 付き）【Phase 6C/6D】 |
| GET | `/api/takeout/import-sessions/{session_id}` | import 履歴詳細【Phase 6C】 |
| POST | `/api/takeout/benchmark` | 取り込み throughput / peak memory 測定（dry-run 既定・本文非返却）【Phase 6D】 |
| POST | `/api/takeout/import-liked-videos-job` | liked import を background job 化（`{path,limit,dry_run}`）【Phase 6D】 |
| POST | `/api/takeout/import-watch-history-job` | watch import を background job 化【Phase 6D】 |
| POST | `/api/takeout/import-search-history-job` | search import を background job 化【Phase 6D】 |
| GET | `/api/takeout/import-sessions/{session_id}/progress` | 実行中 import の進捗（status/phase/件数/eps）【Phase 6D】 |
| POST | `/api/takeout/import-sessions/{session_id}/cancel` | 実行中 import の cancel 要求（checkpoint で停止）【Phase 6D】 |
| GET | `/api/storage/db-stats` | DB 件数 + 概算サイズ + raw_json 実 blob 数（集計のみ・本文非返却）【Phase 6E】 |
| POST | `/api/takeout/benchmark-large` | liked+watch（任意 search）一括 dry-run benchmark（eps/peak/推定時間/推奨 batch）【Phase 6E】 |
| POST | `/api/takeout/import-sessions/cleanup` | 古い import session 行のみ剪定（`{keep_last,older_than_days,dry_run}`。**job/import 済みデータは削除しない**・bounds 未指定で no-op・running 保護）【Phase 6E】 |
| GET | `/api/system/build-info` | プロセスの build 識別（app_version / build_id / schema_head / job types）【Phase 6F】 |
| GET | `/api/system/health/full` | DB/Redis + worker heartbeat + web=worker build 一致 + schema head 一致【Phase 6F】 |
| POST | `/api/takeout/preflight-large` | 大容量 import 前チェック（ZIP/parser=ijson/サンプル bench/DB件数。dry-run）【Phase 6F】 |
| GET | `/api/takeout/import-sessions/{id}/verify` | import 後検査（結果 + DB stats + raw_json 実 blob + 漏洩 grep + job status）【Phase 6F】 |
| GET | `/api/takeout/import-sessions/cleanup-status` | auto session-cleanup の設定 + 直近実行結果【Phase 6F】 |
| GET | `/api/takeout/import-report/latest` | 直近 import session の運用レポート（結果+job+verify+db-stats+leak+推奨）【Phase 6G】 |
| GET | `/api/takeout/import-report/{session_id}` | 指定 session の運用レポート【Phase 6G】 |

import 系（`/api/takeout/import`・`import-watch-history`・`import-search-history`・`import-liked-videos`・`import-all` と各 `-job`）は **`store_raw_json: false`**（既定 true）で raw 活動 blob を保存せず正規化フィールドのみ取り込み【Phase 6E】。
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
| GET | `/api/liked-videos/failure-breakdown` | 失敗 liked ジョブを理由別に集計(private/deleted/unavailable/network/rate_limited/unknown、件数のみ)【Phase 7H】 |
| GET | `/api/system/secrets-status` | cookie/PO-token/visitor_data の設定状況(boolean/masked のみ・実値/絶対パス非返却)【Phase 7I】 |
| GET | `/api/jobs?source_action=&reason=` | `source_action`/`scheduled_by`/`reason`(分類)でジョブ絞り込み【Phase 7D/7H】 |
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
  - `c3d4e5f6a7b8` `takeout_import_sessions` に `job_id`/`rq_job_id`/`parser_backend`/`entries_per_second`/`peak_memory_mb`/`cancel_requested`/`current_phase`/`last_update_at` を追加（Phase 6D・job 化/benchmark/progress。bool は `server_default false`）
  - **Phase 6E は migration 追加なし**（no-raw-json は既存 `raw_json` 列を NULL/省略するだけ、db-stats / benchmark-large / cleanup は読み取り・行削除のみ。head は `c3d4e5f6a7b8` のまま）。
  - **Phase 6F も migration 追加なし**（build-info/preflight は読み取りのみ、auto cleanup は既存 session 行削除のみ、build_id は実行時算出、cleanup status は config 配下のファイル。head は `c3d4e5f6a7b8` のまま）。
  - **Phase 6G も migration 追加なし**（import-staged は既存 import 経路の段階呼び出し、import-report は既存 session/job/stats の読み取り集約のみ。head は `c3d4e5f6a7b8` のまま）。
  - **Phase 7H も migration 追加なし**（失敗分類は error_text のパターン追加、failure-breakdown は既存 job の読み取り集約、metadata-fetched 判定は info_json 有無に精緻化。head は `c3d4e5f6a7b8` のまま）。
  - **Phase 7I も migration 追加なし**（cookie/PO-token は既存 config、secrets-status/preflight は boolean 集約、metadata-run は既存 enqueue のループ + 既存 job 集計のみ。head は `c3d4e5f6a7b8` のまま）。
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

移行元となったスタンドアロン yt-dlp CLI 設定（`base/youtube/twitch + overlay` の思想）は
`app/services/profiles.py` に取り込み済みです（bat 依存は廃止）。元の個人用設定ファイルは
公開リポジトリには含めていません。
