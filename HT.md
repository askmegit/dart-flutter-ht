# dart-flutter-ht

这是官方 `flutter/agent-plugins` 的 `dart-flutter` 1.0.5 fork，增加原生 Dart LSP，并让 Dart MCP 以 `--disable analysis` 运行。除下列文件外，其余上游内容保持不动：

- `.claude-plugin/plugin.json`、`.claude-plugin/marketplace.json`：使用本 fork 的名称和仓库地址；禁用 Dart MCP 分析工具。
- `.lsp.json`、`bin/dart-lsp`：注册原生 Dart analysis server LSP，并按项目 FVM 配置选择 SDK。
- `ht/tests/test_dart_lsp.sh`：验证 SDK 选择与 LSP 启动行为。
- `HT.md`：本说明。

## 为什么拆分分析

2026-09-24 实测，一个 Flutter 会话里 Dart MCP 与 LSP 各自启动一份 analysis server；单份稳态约 400–700 MB，峰值约 1 GB。旧版 SDK 的 Dart MCP server 会在启动时立即启动 analysis server 并让它常驻。由原生 LSP 承担代码分析、Dart MCP 只保留其他工具，可避免同一会话重复运行分析服务。

## 工具分工

- 定义、引用、调用层级、符号、hover：使用原生 `LSP` 工具。工具是 deferred 的；需要时执行 `ToolSearch select:LSP`。
- 修改 `.dart` 后检查错误：在 shell 运行 `flutter analyze`。
- 运行时错误、DTD、pub：使用 Dart MCP。
- `.lsp.json` 设置 `diagnostics: false`，避免存量警告持续刷屏；需要诊断时主动运行 `flutter analyze`。

## SDK 选择和限制

`bin/dart-lsp` 按顺序选择 Dart SDK：项目 `.fvmrc` → `.fvm/fvm_config.json` → FVM default → `PATH`。Dart MCP 的 command 仍是 `PATH` 上的 `dart`，需要 Dart ≥ 3.12 才支持 `--disable` 参数；全局 FVM 切回旧版时 MCP 会启动失败。LSP 与 MCP 在不同 Claude 会话中仍各有一份 analysis server；上游 `dart-lang/sdk#62606` 尚未落地。

## 同步上游

运行 `git fetch upstream && git merge upstream/main`。如有冲突，只在 `.claude-plugin/plugin.json` / `.claude-plugin/marketplace.json` 解决：保留本 fork 的 `name`、`version` 和 MCP `args`，吸收上游其他字段。合并后按上游新版本号更新 `version` 的版本前缀，并保留 `-ht.N` 后缀，例如上游从 `1.0.5` 升级时将其更新为对应的新 `X.Y.Z-ht.1`。

## 安装

在 Claude Code 中运行 `/plugin marketplace add askmegit/dart-flutter-ht`，再运行 `/plugin install dart-flutter-ht@dart-flutter-ht`。同时卸载官方 `dart-flutter` 和独立 `dart-lsp` 插件，避免重复启动 MCP/LSP 或 analysis server。
