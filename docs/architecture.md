# Architecture

## What this build actually does
AI Brainstorming Studio は、ローカルの LM Studio を秘書兼議長AIとして使う macOS 向けデスクトップGUIアプリです。

**現在のビルドの動作範囲:**
1. ユーザーがプロジェクトフォルダと依頼内容を指定する（選択した時点では書き込まない）
2. `ContextScanner` がツリー・主要ファイル・既存AI設定を読み取る
3. `PromptBuilder` が共通コンテキストを作る
4. **LM Studio が単独で回答を作る**
5. 結論・採用方針・実行済み・次にやること・リスクの形で提示し、`.ai-brainstorm/` へ記録する

**動作しないもの（コードは存在するが停止中）:**
- 外部AI CLI（Claude / Antigravity / Codex）の起動 — 3枠とも `config.*_SLOT_ENABLED = False`
- AIによるファイル編集 — `config.IMPLEMENTATION_WRITES_ENABLED = False`
- 実装プランの承認ゲート、差分適用、テスト自動実行
- 新規プロジェクト作成フロー

停止理由と安全境界の設計は `safety-model.md` を参照してください。以下「目標状態」と明記した節は、スロットを再開する場合の設計であり、現在は動作しません。

## Permission policy
現在のビルドでは、**対象プロジェクトの既存ファイルへの書き込みは一切行いません**。アプリ自身のセッション記録は `.ai-brainstorm/`、CLI間の共有メモリは `.ai-shared/` へ保存します。

以下は書き込み機能を再開する場合の設計です（現在は未動作）。
- 自動: 選択プロジェクト内の通常ファイル作成・編集、既存依存のインストール、ローカルのtest/build/lint/起動確認
- 一括確認: 新規依存追加、削除、移動、大量置換、Git commit、長時間または高負荷の処理
- 必ず確認: push、reset、clean、force操作、外部公開、認証情報変更、OS設定、課金につながる操作
- 禁止: API key/pay-as-you-goへの自動切替、無承認の秘密情報送信、複数AIによる同一ラウンドの同時編集

いずれの状態でも、AIへ渡す読み取り対象から `.env`、秘密鍵、認証情報らしい内容は除外します。

## Components
- `src/main.py`: アプリ起動エントリーポイント
- `src/gui/app.py`: シェルウィンドウ。ヘッダー、タブバー、タブ切替、キュー振り分け、キーボードショートカット
- `src/gui/project_tab.py`: プロジェクトフォルダ1つ分の状態とパネル。実行スレッドとキャンセルもタブ単位で持つ
- `src/gui/components/tab_bar.py`: ブラウザ風タブストリップ（閉じるボタン、＋、実行中インジケータ）
- `src/gui/components/header_status_bar.py`: LM Studio、CLI、RTK の状態表示（全タブ共通）
- `src/gui/components/model_import_dialog.py`: 議長AIが`/model`貼り付けテキストを解析してカタログを更新するモーダル。`focus_agents`でAIを絞った文言に切り替わるが、内容の受理自体は絞らない
- `src/gui/components/markdown_editor.py`: 統合回答の表示・編集
- `src/services/tab_session_manager.py`: 開いているタブの復元情報を `~/.ai-brainstorm-studio/` に保存
- `src/services/run_registry.py`: `RunContext`（immutable, run_id付き）と`ProjectRunRegistry`。canonical/resolved project path単位で同時実行を1件に制限する
- `src/agent_models.json`: AI別の選択可能モデル・effortのカタログ。`billing_status` は `subscription_safe` / `usage_credits` / `pay_as_you_go` / `unknown` の4値（Phase Dで拡張済み）
- `src/services/agent_model_selector.py`: `agent_models.json`を読み、モデルIDとeffortをカタログへ照合検証する。Existing CLI Modeでは課金ゲートによる可用性判定は行わない。`CHAIR_AUTO_SELECT`センチネル（「議長AIにお任せ」の内部表現）と`resolve_chair_auto_agents()`もここにある。`select()`は元々部分集合のagentに対しても1回のchair呼び出しで選定できる
- `src/services/agent_model_detector.py`: `agy models` の動的解析と、CLIバージョン変化の検出。カタログのアトミック更新（Phase Dで追加）。`refresh_all()`は起動時・手動ボタン・`app.py`の30分間隔バックグラウンドタイマーの3経路から呼ばれる
- `src/services/model_catalog_updater.py`: 議長AIにCLI貼り付けテキストを解析させ、カタログへマージする。既存IDのマージでは`selector_default`等の人間レビュー済みフィールドを引き継ぎ、未指定`billing_status`は`unknown`にfail-closedする
- `src/services/cli_execution_policy.py`: 「利用者の設定をどこまで上書きしてよいか」を1つのオブジェクトへ集約。`existing_config` が有効。`strict_subscription` は旧設計で、テストからのみ参照される
- `src/services/cli_status.py`: 失敗分類 → 日本語ラベル・復旧案内の一元マッピング
- `src/services/cli_setup_guidance.py`: Claude Code、Antigravity CLI、Codex CLIについて、ユーザーが確認して使う公式手順・コピー用インストール／ログインコマンドを管理する。インストールやログインは実行しない
- `src/config.py`: 定数、除外ディレクトリ、重要ファイル、旧strictポリシー用の環境変数除外リスト
- `src/models.py`: スキャン結果、CLI結果、ブレスト結果のデータ構造
- `src/services/context_scanner.py`: ファイルツリー、主要ファイル、AI関連ファイル、`.ai-shared/memory.md` の検出
- `src/services/workspace_manager.py`: `.ai-brainstorm/` のセッション成果物・履歴・vendor context保存、および `.ai-shared/` の共有メモリ雛形と内側の `.gitignore` の初期化
- `src/services/prompt_builder.py`: 共通コンテキストとAI別プロンプトの生成。議長AIの要約結果とは別に、`AGENTS.md` と共有メモリを上限付きの固定枠として全CLIへ同一内容で渡す。LM Studioが応答しない場合もこの固定枠は保持する
- `src/services/role_orchestrator.py`: Author、Critic、Verifier のラウンド別ローテーション計画
- `src/services/chair_agent.py`: LM Studio の OpenAI互換ローカルAPI呼び出し。議長モデルは `config.LM_STUDIO_CHAIR_MODEL` で固定し、未ロードならfail-closed（別モデルへ勝手に流れない）。`config.LM_STUDIO_CHAIR_REASONING_EFFORT` を `reasoning_effort` として送る（既定 `none`。送らないと推論モデルが本文を返さず、議長が機能しない）
- `src/services/cli_adapters.py`: Claude、Antigravity、Codex のコマンド差分吸収。`WriteGrant` が無ければ全AIがread-only（claude `--permission-mode plan --tools ""`、agy `--mode plan --sandbox`、codex `--sandbox read-only`）。grantがある場合のみ、その1体だけが書込モードになる（claude `acceptEdits` + `--tools default`、agy `accept-edits`、codex `workspace-write`）。`bypassPermissions` と `danger-full-access` はどの経路でも出力しない
- `src/services/cli_runner.py`: AI CLI の並列実行
- `src/services/process_runner.py`: 非対話subprocess、タイムアウト、キャンセル、環境変数除去
- `src/services/refinement_loop.py`: 全体ワークフローの中心処理
- `src/services/run_state.py`: runの局面（`preparing` / `planning` / `waiting_approval` / `implementing` / ...）と遷移表、`RunStateMachine`。`ProjectTab.running` / `cancelling` はここからの導出であり、独立したフラグを持たない。承認を経ない `planning -> implementing` は遷移表で禁止されている
- `src/services/health_checker.py`: CLIとLM Studioの検出、認証プリフライト
- `src/services/safety_guard.py`: 課金系環境変数や既存プロジェクト状態の警告
- `src/services/response_preprocessor.py`: LM Studio投入前のAI回答軽量化
- `src/services/chair_output.py`: 議長回答の見出し照合（表記揺れ・装飾・複合見出しを吸収し、行頭の見出しだけを対象にする）と言語判定（かな比率）。厳密一致で回答を破棄していた経路を置き換えるためのもの
- `src/services/question_manager.py`: ユーザー確認事項の抽出（行末が `?` / `？` の行のみ。箇条書き記号とMarkdown強調を正規化して重複排除）
- `src/services/autonomy_controller.py`: 自動化レベルの定義。現在GUIが提供するのは「相談のみ」「実装案まで」の2段階（実装レベルは定義を残すが非表示）。`AutomationCapabilities`（ラウンド数・プラン作成・書込可否・テスト実行）が単一の情報源で、効果が発生する場所で必ず参照される。旧5段階のラベル／整数の移行マップも持つ
- `src/services/write_grant.py`: **AI CLIへファイル編集を許可する唯一の手段**。read-onlyは「フラグがFalse」ではなく「grantが存在しない」状態で、全コマンドビルダの既定が `grant=None` = read-only。`granted_after_approval()` は `approved=False` で例外を投げ、`grant_for()` は agent と run_id の両方を照合するため、Implementerへのgrantが同一runのCriticや別のrunへ広がらない
- `src/services/implementation_plan.py`: 承認対象の構造化プラン（変更概要／対象ファイル／実装手順／テスト／リスク）の解析と、`ApprovalRequest` / `ApprovalDecision` / `ImplementationOutcome`。見出しの表記揺れを吸収し、パース失敗時も生テキストを表示して承認ゲート自体は必ず通す
- `src/services/git_checkpoint.py`: 承認時点のcommit記録と事後差分（untrackedファイルを含む）。**アプリはcommit / reset / clean / stashを一切実行せず**、取り消しコマンドを提示するだけ。未コミット変更がある場合は `reset --hard` を提示しない
- `src/services/test_runner.py`: 承認済み実装後のテスト実行。コマンドは**検出のみで推測しない**（既知構成に一致しなければ「実行しなかった」と報告）。固定argvで、AI出力やユーザー入力は混入しない
- `src/services/chat_room_manager.py`: プロジェクト別チャットルーム履歴
- `src/services/recent_projects_manager.py`: 最近使ったプロジェクト一覧
- `src/services/usage_tracker.py`: `rtk gain` の概要表示
- `src/services/verification_runner.py`: 簡易検証

## Tab model
実装済み:
- 1タブ＝1 canonical/resolved project path。同じ実パスを重複タブで開こうとした場合は既存タブへ移動する（`BrainstormApp.find_tab_by_path`/`new_tab`/`open_path`）
- 異なるプロジェクトは同時実行数を制限しない。同じproject pathに対してはアプリ全体で1実行だけ許可する（`ProjectRunRegistry`）
- 実行中かどうかに関わらず、一度バインドされたタブのproject pathは不変。別フォルダを開く操作は新規タブ（または既存タブへのフォーカス）として扱う（`ProjectTab.open_project`）
- 実行開始時に immutable な `RunContext(run_id, tab_id, project_root, room_id, request, automation_level, cancel_event)` を作る。`ProjectRunRegistry.try_start`（予約）→ 部屋作成 →`finalize`（確定）の二段階APIで、対象フォルダへの最初のディスク書込みより先に実行枠を予約する
- ワーカースレッドからGUIへは `tab_id` と `run_id` を含むイベント（`queue`のタプルは`(kind, tab_id, run_id, payload)`）を流し、`ProjectTab.owns_run(run_id)`が偽なら`_poll_queue`が古いrunの遅延イベントを破棄する
- Tkinter の `after()` ループはアプリ全体で1本だけ動かす
- ヘッダーのヘルス表示とRTK表示は全タブ共通。実行中バッジはいずれかのタブが実行中なら点灯する
- 同名フォルダは親ディレクトリ名を付けて `parent/name` と表示する
- UI配置と開いているタブは `~/.ai-brainstorm-studio/`、プロジェクト固有の履歴とチェックポイントは `.ai-brainstorm/` に保存する
- キャンセル要求時は`ProjectTab.cancelling`を内部的にTrueにする（GUI表示は未実装）。ラウンド実行中にキャンセルされた場合、`RefinementLoop`はそのラウンドのLM Studio要約呼び出しを追加実行しない

- タブ帯は run の局面を区別して表示する。`run_state.STATE_MARKERS` が唯一の対応表で、実行中 `●` / 承認待ち `◆` / 一時停止 `❙❙` / 失敗 `✕` を描き分ける。完了した run をまだ見ていないタブには未読印 `◉` が付き、そのタブを開くと消える。マーカーは `TabInfo` が保持するため、他のタブを開閉して帯を再構築しても失われない。

## Project creation（将来構想・現在は未実装）
依頼内容から新規プロジェクトを作成する構想があります。**現在のビルドには含まれておらず、外部のプロジェクト作成helperも同梱していません。**

- 作成処理そのものはGUI側で再実装せず、外部helperを引数配列（shell文字列ではなく）で呼び出す設計
- LM Studioが依頼内容から `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` に合う名前を生成する
- 承認前は dry-run のみを実行し、対象フォルダへ書き込まない
- 失敗時は安全なロールバック結果を表示し、実装を開始しない

## Persistence and recovery
- 保存対象: タブ、選択ルーム、自動化設定、依頼ドラフト、ログ、AI出力、差分、確認待ち、進行段階、停止理由、スクロール位置
- すべてのrunへUUIDベースの一意な`run_id/session_id`を付ける
- アプリ終了時は実行中プロセスと子孫プロセスの停止完了を確認し、runを`paused`として保存する
- 再起動後は閉じる前の画面を復元し、paused runには安全な再開操作を表示する
- 実プロセスの復元は行わず、最後に完了したステージのチェックポイントから再実行する
- 保存前に秘密値、device code、credential URL、tokenらしい値を共通redactorで除去する

## Target data flow（目標状態・現在は未動作）
以下は3AIと承認ゲートが有効な場合のフローです。現在は上記「What this build actually does」の5ステップのみが動きます。

1. GUIが依頼とproject pathを受け取り、project lockと一意なrun IDを確保する
2. `SafetyGuard`、`ContextScanner`、CLIプリフライトが安全性と利用可能AIを確認する
3. `RoleOrchestrator` が利用可能AIへ Implementer、Critic、Verifier を割り当てる
4. 3AIの計画回答をLM Studioが統合し、確認事項とプランをGUIへ返す
5. GUIがrunを`waiting_approval`にし、ユーザー承認を待つ
6. 承認後、Implementerへだけ通常編集権限を付与し、実装とローカル検証を行う
7. CriticとVerifierがread-onlyで成果物を確認し、LM Studioが追加ラウンドの要否を判断する
8. 最大2ラウンドまたは停止条件で終了し、変更、テスト、差分、プレビューを保存・表示する
9. ユーザーの修正指示は新しいplanning cycleとして記録し、再承認後に実装する
10. completed、failed、cancelled、pausedの終端状態でlockを解放する

## Health status flow
- `HealthChecker.check_all()` はモデルを呼ばず、実行ファイルの存在とLM Studioの到達性を調べる。CLI発見時は `installed_unverified` とする。
- `HealthChecker.preflight_all()` は短い安全プロンプトで実応答を確認し、結果を `ToolStatus` としてGUIキューへ送る。
- `BrainstormApp` は discovery と preflight/run の状態をマージする。CLI未検出・slot無効は常に優先し、それ以外は直近の実行時証拠を優先する。
- `HeaderStatusBar` は構造化statusから色・日本語ラベル・復旧案内を描画する。通常再チェックでログイン切れや上限到達を消さない。
- 明示的な「AI接続テスト」だけがプリフライトを再実行する。通常のステータス再チェックはAI利用枠を消費しない。
- Claude Code、Antigravity CLI、Codex CLIの導入・再認証支援は、公式手順とコマンドコピーまで。アプリはシェル、インストーラー、ログイン用CLIを自動起動せず、認証方式を固定せず、認証情報を扱わない。

## External services
- LM Studio local server: `http://localhost:1234/v1`
**目標状態（Existing CLI Mode）**: いずれのCLIも、利用者の既存ログイン・既存設定のまま起動する。アプリは認証・provider・接続先・モデル設定を変更しない。

- Claude Code CLI: 通常のClaude Pro/Max等のログインを利用する。
- Antigravity CLI: `agy` の通常のGoogle AIプランログインを利用する。旧 `gemini` はレガシー扱いで、plan/read-onlyを保証できないため自動実行しない（課金ではなく安全性が理由）。
- Codex CLI: ChatGPT/Codexの通常ログインと `~/.codex/config.toml` をそのまま利用する。
- RTK: **AI CLI では使わない**。`rtk` の削減はツール系コマンドの出力圧縮であり、`claude`/`agy`/`codex` の削減実績は0%（2026-08-16実測）。一方で `rtk` はコマンド行を永続DB（`history.db`）へフル保存するため、経由させるとプロンプト全文が記録される。利用者が他用途で使う `rtk` 自体には影響しない。

アプリはログイン操作を代行せず、認証情報の読み書きも行わない。API key や有料providerへの自動フォールバックも行わない。**ただし、その実行がどう課金されるかはアプリからは判定できない。**

> **移行完了（Phase E、2026-08-17）**: 上記が実際のコードの挙動である。アプリ専用トークン（`claude_token_store`）、認証ゲート（`claude_auth_guard`）、トークン登録ダイアログ（`claude_auth_dialog`）は削除済み。3AIとも `rtk` 非経由。

## Constraints
- 対象プロジェクト選択時点ではファイルを書き込まない
- `.ai-brainstorm/` は実行開始または明示的なチャット操作時にだけ作成する
- `.ai-shared/` も実行開始時にだけ初期化し、既存の共有メモリを上書きしない
- `.ai-shared/memory.md` は変化する引継ぎだけを扱い、コード、Git状態、一次資料の代わりにしない。秘密情報・会話全文・未確認の推測を保存しない
- 既存AI設定ファイルは検出するが、勝手に変更しない
- 秘密情報をログ・保存物・GUI表示へ出さない（`secret_redactor`）
- **（レガシー / Phase Bで撤去）** CLI子プロセスから課金系・クラウド系環境変数を除去する。Existing CLI Modeでは、利用者が設定した`ANTHROPIC_API_KEY`・Bedrock/Vertex・provider等は**除去しない**。「秘密値をログへ出さない」ことと「既存の環境変数を消す」ことは別の要件であり、前者だけを維持する
- CLI実行は非対話、タイムアウト付き、キャンセル可能にする
- GUIメインスレッドでCLIやLM Studio通信を待たない
- **OSサンドボックスが完成するまでAI CLIにファイル編集権限を与えない**（現在の停止理由。`safety-model.md` 参照）
- 再開後も、現在のラウンドでImplementerに指定された1AIだけへ選択プロジェクト内の通常編集権限を与える
- API key、pay-as-you-go、旧Geminiの安全性未確認経路へ自動フォールバックしない
- README.md は人間向け仕様書として扱い、AI内部メモにはしない
