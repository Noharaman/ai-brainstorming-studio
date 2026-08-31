# AI Agent Guide

## ⚠️ このビルドの現状（最初に読むこと）

**現在このアプリは LM Studio 単体で動作する、読み取り専用の相談ツールです。**

- **外部AI CLI（Claude Code / Antigravity / Codex）は起動しません。** 3枠とも停止中。
- **AIによるファイル編集は行いません。** 実装機能は停止中。
- 相談は LM Studio のみで完結します。

停止理由と再開条件は `docs/safety-model.md` に記載しています。**下記の「3社に相談して統合する」という記述は将来目標であり、現在は動作しません。**

### 変更してはならない安全境界

以下は「動いていないから直してよい」ものではありません。**安全境界を実証できるまで意図的に閉じてある**ものです。理由を理解せずに `True` へ戻さないでください。

| 設定 | 値 | 閉じている理由 |
| --- | --- | --- |
| `config.IMPLEMENTATION_WRITES_ENABLED` | `False` | 編集を行うCLIプロセスをOSレベルで隔離する手段がない |
| `config.CLAUDE_SLOT_ENABLED` | `False` | 管理者ポリシーhookを無効化・検出できない |
| `config.CODEX_SLOT_ENABLED` | `False` | MCPを実行単位で無効化できない（実測） |
| `config.ANTIGRAVITY_SLOT_ENABLED` | `False` | 実行単位の無効化フラグが存在しない（実測） |
| `config.RUN_TESTS_AUTOMATICALLY` | `False` | AIが書き換えたコードをアプリの権限で実行することになる |

書き込み権限は**3箇所**（レベルの能力判定・`WriteGrant` 生成・プロセス起動直前）で遮断しています。1箇所だけ戻しても到達しませんし、**戻すべきでもありません。**

## Project overview（将来目標）

AI Brainstorming Studio は、LM Studio のローカルAIを秘書兼議長として使い、Claude Code、Antigravity CLI、Codex CLI へ役割別に相談する macOS 向けデスクトップGUIアプリ**を目指しています**。

ユーザーはプロジェクトフォルダと依頼内容を指定し、アプリはプロジェクト構造、主要ファイル、既存AI設定を読み取って短い共通コンテキストを作成します。その後、各AI CLIの回答を収集し、LM Studioで比較・統合して、ユーザー向けに短い結論、次の作業、確認事項を返します。

このプロジェクトでは、既存CLI設定の尊重、ローカル優先、既存ファイル保護、GUIの非ブロッキング動作を最重要の前提とします。

### 課金と認証に関する方針（2026-08-16更新 / Existing CLI Mode）
アプリは各AI CLIの**既存のログイン、モデル、provider、ローカル設定をそのまま利用**します。すべての実行がサブスクリプション枠に収まることをアプリが証明しようとはしません。

区別すべき2つの約束を混同しないこと。

1. **アプリは認証・provider・接続先を自動的に変更しない。** ログイン操作、認証情報の読み書き、API key や有料providerへの自動切り替えを行わない。
2. **アプリは、利用者の既存CLI・アカウント設定がどう課金されるかを判定も保証もできない。** 追加料金が発生しないとは書かない。

具体的な線引き。

- **アプリが行わないこと**: 認証・provider・接続先・モデル設定の変更、ログイン／ログアウトの自動実行、認証情報の読み取り・保存・削除・書き換え、API keyや有料providerへの自動フォールバック、アカウントのcredits設定の判定。
- **アプリが維持する運用上の境界**（課金保証とは別）: 読み取り専用／plan実行、非対話実行、timeout、cancellation、process group cleanup、GUIバックグラウンド実行、出力サイズ制限、秘密情報を含まないログ、部分成功時の継続。
- **利用者の責任範囲**: どのモデルを選ぶか、そのモデルがサブスク枠かcredits消費かAPI課金か、アカウント側のcredits／overage設定。アプリは分かる範囲を表示し、`unknown`／`usage_credits` のモデルには実行前に明示確認を求めるが、確定はしない。

この方針はREADMEにも利用者向けに明記すること。**保証できないものを保証すると書かない。**

> **移行完了（2026-08-17、Phase E）**: アプリ専用Claudeトークン（`claude setup-token`）、Claude署名・認証ゲート、`billing_status`による実行可否ゲートは削除済み。旧`strict_subscription`ポリシーは差分をテストで示すためだけに残っており、実行時には選択されない。

## Primary goals
- LM Studio を議長AIとして使い、複数AI CLIの回答を安全に統合する
- 各CLIの既存ログイン・既存設定をそのまま使い、アプリ側で書き換えない
- API key 課金や pay-as-you-go へ自動切り替えしない
- GUIを固めず、CLI実行を非対話・タイムアウト付きで扱う
- 対象プロジェクトの既存ファイルやAI設定を勝手に移動・上書きしない
- `.ai-brainstorm/` にアプリ用の作業記録を集約する
- ユーザー向け出力は短い結論、採用方針、確認事項に絞る
- 保守しやすい構成にし、既存機能を壊さず段階的に変更する

## Working rules
1. 作業開始前に `README.md`、関連する `src/services/`、関連GUIファイルを確認する
2. 現在のディレクトリ、Git状態、未追跡ファイルを確認してから変更する
3. 不明な仕様を推測だけで実装しない
4. 追加課金、外部API送信、API key 利用、クラウド従量課金につながる変更をデフォルトにしない
5. CLI実行、認証、サンドボックス、権限モードの変更は影響範囲を明記してから行う
6. GUIメインスレッドで長時間処理やCLI待機を行わない
7. 対象プロジェクト選択時は読み取り専用を維持し、`.ai-brainstorm/` 作成は実行時に限定する
8. 既存の `AGENTS.md`、`CLAUDE.md`、`GEMINI.md`、`.claude/`、`.gemini/`、`.codex/` は検出対象であり、勝手に統合・削除・置換しない
9. 共通ルールの本体は `AGENTS.md` とし、このプロジェクト用に新しい `GEMINI.md` は作成しない
10. 秘密情報をコード、ドキュメント、ログ、`.ai-brainstorm/` に記録しない
11. 修正後は可能な範囲でテストまたはインポート確認を実行する
12. 不要なファイル、依存関係、大規模リファクタリングを追加しない

## Documentation
タスクに応じて必要なファイルだけを確認する。すべてのドキュメントを無条件に読み込まない。
- `README.md`: 人間向けの仕様書、機能要件、運用方針。冒頭に現在の動作範囲がある
- `docs/safety-model.md`: **安全境界の設計。脅威・実測結果・判断・再開条件。** 権限や実行経路を触る前に必ず読む
- `docs/architecture.md`: 実装構成、データフロー、外部依存
- `docs/decisions.md`: 現在も有効な設計判断と理由
- `.ai-brainstorm/`: アプリ実行時に生成される作業記録。必要な場合だけ参照し、公式資料として扱わない

## Architecture notes
- `src/gui/app.py` がGUI、プロジェクト選択、実行開始、ログ表示、結果表示を担当する
- `src/services/context_scanner.py` が対象プロジェクトのツリー、主要ファイル、AI関連ファイルを読み取る
- `src/services/workspace_manager.py` が `.ai-brainstorm/` の初期化とセッション成果物の保存を担当する
- `src/services/prompt_builder.py` が共通コンテキストとAI別プロンプトを作る
- `src/services/role_orchestrator.py` がラウンドごとの Author/Critic/Verifier 割り当てを管理する
- `src/services/cli_adapters.py` と `src/services/cli_runner.py` がAI CLIごとの差分と並列実行を扱う
- `src/services/process_runner.py` が非対話subprocess、タイムアウト、キャンセル、子環境の構築を扱う（Existing CLI Modeでは `os.environ` をそのまま子へ渡し、秘密値をログへ出さないことだけを維持する。課金系環境変数の除去は旧`strict_subscription`ポリシーにのみ残る）
- `src/services/refinement_loop.py` がスキャン、プリフライト、役割ローテーション、AI実行、統合、履歴保存の中心になる
- `src/services/chair_agent.py` がローカルLM Studioへの問い合わせを担当する

## Before making changes
1. Git状態を確認する
2. 依頼に関係するREADME、docs、ソースを読む
3. 変更範囲と対象ファイルを特定する
4. 追加課金、外部送信、秘密情報、ファイル変更権限への影響を確認する
5. GUIの応答性、CLIタイムアウト、キャンセル処理への影響を確認する
6. 必要であれば実装計画を提示する

## After making changes
以下を報告する。
- 変更した内容
- 変更したファイル
- 実行したテスト
- テスト結果
- 残っている問題
- 人間による確認が必要な事項
- 次に行うべき作業

## Session handoff
重要な作業の完了時は、必要に応じて公開ドキュメント（`README.md` / `docs/architecture.md` / `docs/safety-model.md` / `docs/decisions.md`）を更新する。一時的な会話内容ではなく、別のAIが作業を再開するために必要な情報だけを残す。

安全境界に関わる変更を行った場合は、`docs/safety-model.md` に「何を測ったか」「どう判断したか」を追記する。**実測していないことを実測したように書かない。**

## Communication
- 説明、質問、作業報告は日本語で行う
- コード、識別子、Gitコミットメッセージは英語を使用する
- ユーザー向け回答は結論、採用方針、実行済み、次にやること、確認事項、リスクを中心に短くまとめる

## Safety
明確な指示がない限り、以下を実行しない。
- API key 課金、従量課金API、クラウド pay-as-you-go への切り替え
- 本番環境へのデプロイ
- リモートリポジトリへのpush
- リモートブランチの削除
- `git reset`、`git clean`、force push
- ファイルの大量削除、移動、大量置換
- `.env`、認証情報、秘密鍵、トークンの変更または内容出力
- OS設定、常駐サービス、ログイン項目、権限設定の変更
- AI CLIへのファイル編集権限付与
