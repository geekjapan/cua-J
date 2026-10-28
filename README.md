# cua-J

A history-independent import of the Cua Driver CLI and libraries from [trycua/cua](https://github.com/trycua/cua), upstream revision `bb507dd282c53b872edd5cabff81860fcaa746bf`.

Driver sources, Rust SDK, Python and TypeScript bindings, contract definitions, platform adapters, fixtures and embedded assets are retained. Spaces, VM management, model training and benchmark products are excluded. The only local crate dependency outside Driver is `cua-telemetry`; its two build-time catalogs are vendored using its upstream fallback mechanism.

## 導入するものと順序

実際に画面を操作するのはDriver本体です。スキルは、AIエージェントにDriverの使い方を伝える手順書です。Decision APIのPython実行例を使う場合は、Pythonの依存モジュールも導入します。

| 導入対象 | 用途・導入されるもの | コマンドを実行する場所 | 必要な場面 |
| --- | --- | --- | --- |
| Driver本体とRust SDK | 画面操作を実行する`cua-driver`とRustライブラリ | `cua-J`のソース取得先 | 実際の画面操作・MCP接続 |
| Pythonの依存モジュール | Python実行例用の`httpx`、`mcp`、`typesafe-sdk`など | `cua-J`のソース取得先 | Decision APIの実行例・比較測定 |
| `cua-j`スキル | エージェント用の`SKILL.md`とライセンス | Driverを使いたい利用先プロジェクト | エージェントに操作手順を追加する場合 |

エージェントから使う場合は、次の順に進めてください。

1. [Driver本体とRust SDKを導入](#install-on-macos)します。
2. 利用先プロジェクトで[スキルを導入](#install-the-agent-skill)します。
3. [MCPを設定](#connect-an-mcp-client)し、エージェントをDriverへ接続します。

Decision APIのPython実行例も使う場合は、続けて[Python依存モジュールの導入とAPI認証](#run-decisions-through-driver)を行います。Driver本体を導入済みなら、手順1を省略できます。開発用の[検証とCI](#verify-on-macos)は後半に記載しています。

<a id="install-on-macos"></a>

## 1. Driver本体とRust SDKを導入する（macOS）

Git、Xcode command line tools、`rustup`を準備してください。以下は、このforkのソースを取得してビルドする手順です。`cd cua-J`以降は、ソース取得先のルートで実行します。

```sh
git clone https://github.com/geekjapan/cua-J.git
cd cua-J
rustup toolchain install 1.95.0 --profile minimal
cargo +1.95.0 build --release --locked --manifest-path libs/cua-driver/rust/Cargo.toml -p cua-driver -p cua-driver-sdk
export CUA_DRIVER_BIN="$PWD/libs/cua-driver/rust/target/release/cua-driver"
export PATH="$PWD/libs/cua-driver/rust/target/release:$PATH"
cua-driver --version
cua-driver list-tools
```

`cua-driver --version`が`0.38.0`を表示し、`list-tools`でツール一覧を取得できれば、CLIの導入確認は完了です。エージェントから画面を操作する場合は、後述のMCP接続とmacOS権限も設定してください。

ビルド成果物は`libs/cua-driver/rust/target/release/`に生成されます。`CUA_DRIVER_BIN`はPython実行例が使うCLIを指定し、`PATH`はシェルから`cua-driver`を呼び出せるようにします。これらの`export`は現在のシェルで有効です。別のターミナルでも使う場合は、ソース取得先の絶対パスをシェル設定へ追加してください。`CARGO_TARGET_DIR`を設定している場合は、その出力先に読み替えます。

<a id="install-the-agent-skill"></a>

## 2. エージェント用スキルを導入する

Node.js 22.20以降を準備し、**Driverを使いたいプロジェクト**へ移動して実行します。以下の`/path/to/your-project`は、利用先のパスに置き換えてください。

```sh
cd /path/to/your-project
npx skills add geekjapan/cua-J --skill cua-j
```

対話画面で導入先のエージェントを選択します。Codexを指定して対話を省略する場合は、同じ利用先プロジェクトで次を実行します。

```sh
npx skills add geekjapan/cua-J --skill cua-j --agent codex --yes
```

このコマンドは、[cua-jスキル](skills/cua-j/SKILL.md)の手順書とライセンスを利用先プロジェクトへ配置します。**Driver本体、Pythonの依存モジュール、APIキーの設定はそれぞれの手順で用意します。** 全プロジェクトで使うユーザー単位の導入には`--global`を追加します。対応エージェントとオプションは[skills CLIの公式リファレンス](https://github.com/vercel-labs/skills/blob/main/docs/commands.md)を参照してください。

Codexを指定した導入先は`<利用先プロジェクト>/.agents/skills/cua-j/`です。スキルはDriver内蔵のガイドを読み、接続と操作を案内します。

<a id="connect-an-mcp-client"></a>

## 3. エージェントをDriverへ接続する（MCP）

スキルを導入したエージェントに、DriverのMCPサーバーも登録します。`command -v cua-driver`でCLIの絶対パスを確認してください。

次は`mcpServers`形式を使うクライアントの設定例です。`command`を、手順1でビルドした実行ファイルの絶対パスに置き換えて追加します。クライアントごとのMCP設定方法に従ってください。

```json
{
  "mcpServers": {
    "cua-driver": {
      "command": "/absolute/path/to/cua-J/libs/cua-driver/rust/target/release/cua-driver",
      "args": ["mcp", "--direct"]
    }
  }
}
```

設定を反映してクライアントを再接続し、`get_guide`などのDriverツールが表示されることを確認してください。

macOSでは`--direct`により、このforkのランタイムをMCPプロセス内で起動します。AccessibilityとScreen Recordingは、起動元のターミナルやエージェントホストの権限を使います。画面操作やスクリーンショットを使う場合は、**システム設定 > プライバシーとセキュリティ**で、その起動元に権限を付与してください。引数を`mcp`だけにすると、インストール済み`CuaDriver.app`へのプロキシ接続になります。詳細は[macOSのプロセス識別と権限](libs/cua-driver/README.md#macos-process-identity-and-permissions)を参照してください。

<a id="run-decisions-through-driver"></a>

## Decision APIのPython実行例を使う（任意）

Python実行例ではOpenAI Decisions、Microsoft Decision-1、通常のOpenAI Responsesによるfunction callを扱います。以下は、実行例の依存モジュールとAPI認証を準備する手順です。

<a id="set-up-the-python-example"></a>

### Pythonの依存モジュールを導入する

Python 3.10以降と`uv`を準備してください。検証にはPython 3.13.13とuv 0.11.6を使用しました。**`cua-J`のソース取得先のルートへ移動して**、依存関係を同期します。

```sh
uv sync --frozen --project libs/cua-driver/examples/jev-use
uv run --frozen --project libs/cua-driver/examples/jev-use \
	python libs/cua-driver/examples/jev-use/verify_decision_cli.py --model mock
```

`uv sync`は、Python実行例用の仮想環境と依存ライブラリを用意します。`from cua_driver import CuaDriver`用のPython SDKの導入とは別の手順です。この実行例はDriverへMCPで接続するため、Python SDKを必要としません。

mock確認で`"kind": "selected"`と`"selected_id": "submit-form"`が表示されれば、依存環境の確認は完了です。この確認にAPIキーや画面操作は不要です。

### API認証を設定する

保存先は**`cua-J`のソース取得先のルートにある`.env.local`**です。Git管理の対象外で、利用するプロバイダーの認証情報を記入します。以下のコマンドは、既存ファイルの内容を保持したまま権限を設定します。

```sh
touch .env.local
chmod 600 .env.local
```

| プロバイダー | `.env.local`に設定する変数 |
| --- | --- |
| OpenAI Decisions or Responses | `OPENAI_API_KEY` |
| Microsoft Decision-1 | HTTPSリソースルートの`AZURE_ENDPOINT`、`AZURE_DEPLOYMENT_NAME`（既定は`decision-1`）、`AZURE_API_KEY`または`AZURE_ENTRA_TOKEN` |

各値を`NAME=value`形式で1行ずつ記入してください。Entraトークンは実行時点で有効なものを使います。

OpenAI Decisionsへ合成リクエストを1回送る例です。**設定したアカウントの有料APIを呼び出します。** このコマンド自体は画面を操作しません。ソース取得先のルートで実行してください。

```sh
uv run --frozen --project libs/cua-driver/examples/jev-use --env-file .env.local \
	python libs/cua-driver/examples/jev-use/python/choose_decision.py \
	--model openai-decisions \
	< libs/cua-driver/examples/jev-use/fixtures/jev-choice-request-v1.json
```

Microsoftの場合は、対応する認証変数を設定して`--model microsoft-decision1`に変更します。成功時は`cua.decision_choice_v1`を返し、判断結果は`selected`、`reobserve`、`abstain`のいずれかです。APIや応答検証の失敗は`error`、設定不備は非ゼロ終了で報告します。

### 実際の操作と比較結果

選択結果は元の観測と許可済みの引数に結び付けて検証します。モデルの確信度は操作権限を与えず、Driverのトークンやcapture IDも推論中にUIが変わらなかったことを保証しません。

See [the decision providers and native comparison](libs/cua-driver/examples/jev-use/decision-models.md#remote-decision-providers) for configuration and verification commands. The comparison uses isolated AppKit tasks with an independent state oracle. It does not measure general desktop planning or unrestricted text generation. The TypeScript and existing browser runners retain their upstream provider support.

The [45-trial native comparison](libs/cua-driver/examples/jev-use/benchmarks/2026-10-28-native.md) records measured completion rates, inference and task times, failures and limits. Sanitized trial data and source hashes accompany the report.

The [45-trial public website comparison](docs/benchmarks/2026-10-28-websites.md) records completion, action outcomes and timing on Wikipedia, Python documentation and Hacker News. See [the website setup](libs/cua-driver/examples/jev-use/decision-models.md#compare-actual-public-websites) to repeat it with isolated Chrome profiles and independent completion checks.

<a id="verify-on-macos"></a>

## 開発者向け検証（macOS）

These checks use a debug build and require Python 3 and Z3 5.1.0. Run from the repository root.

```sh
cargo +1.95.0 build --locked --manifest-path libs/cua-driver/rust/Cargo.toml -p cua-driver -p cua-driver-sdk
python3 scripts/verify-runtime.py
python3 scripts/verify-formal.py --mutation
```

The runtime check launches an isolated daemon and reads native display dimensions through the public CLI. It does not inject input or capture the screen. Evidence survives teardown in ignored `artifacts/`.

The formal check covers one capture and every sequence of four operations drawn from ten lifecycle and admission cases. Z3 checks a symbolic lifecycle model. The Rust check exercises 10,000 traces against the actual source. The mutation run removes action consumption temporarily, requires detection and restores the source. Run it without concurrent source edits. This is bounded sequential verification, not a proof of the whole application or concurrent Rust execution.

Project-local instructions and feature maps are in [.cursor/skills/verify-cua-driver](.cursor/skills/verify-cua-driver/SKILL.md) and [.cursor/skills/verify-capture-consumption](.cursor/skills/verify-capture-consumption/SKILL.md). Use `/maintain-verification-skill` and `/maintain-formal-verification-skill` to update them after changes.

## CI

[GitHub Actions](.github/workflows/ci.yml) runs on pushes to `main`, pull requests and manual dispatch. An Ubuntu job runs the locked Python decision and controller tests without credentials or desktop input. A macOS 26 job builds the CLI and Rust SDK, runs core, contract and SDK tests, runs CLI tests serially, then runs the bounded formal check with mutation detection. CI logs are retained for seven days.

The hosted job does not run `scripts/verify-runtime.py`. Native display verification remains a separate local check using the commands above. CI does not certify desktop interaction or other platforms.

## Import and licenses

[UPSTREAM.json](UPSTREAM.json) records each imported path and SHA-256. The upstream Git history is not imported. The pre-existing local initialization commit is preserved. Upstream contributor copyright notices are retained.

Local changes include the consolidated root license and its packaging input paths, the reduced `libs/cua/Cargo.toml`, the Rust toolchain pin changed from 1.97.1 to the tested 1.95.0, root documentation and ignores, verification helpers and skills, and the bounded test. The Python example adds remote decision providers, guarded native execution, a Responses baseline and repeatable native and website comparisons. The Rust Driver implementation remains unchanged. `.DS_Store` and upstream `CLAUDE.md` instructions are excluded. `UPSTREAM.json` lists deliberate modifications to imported files.

[LICENSE](LICENSE) contains the upstream MIT notice and parallel copyright notices for Cua AI, Inc. and geekjapan modifications. Third-party notices and licenses remain beside their assets. The vendored `teleport-targets.json` comes from an FSL-1.1-MIT directory and retains its [source license](libs/cua/crates/cua-telemetry/vendor/LICENSE-teleport). This import as a whole must not be described as MIT-only. Optional perception models are not downloaded by this build.

Upstream component documentation remains under [libs/cua-driver](libs/cua-driver/README.md). Links and release scripts that target the full upstream monorepo are historical upstream instructions; the supported local entry points are the commands above.
