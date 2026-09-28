# dart-flutter-ht

这是官方 `flutter/agent-plugins` 的 `dart-flutter` 1.0.5 fork，增加原生 Dart LSP，并让 Dart MCP 以 `--disable analysis` 运行。除下列文件外，其余上游内容保持不动：

- `.claude-plugin/plugin.json`、`.claude-plugin/marketplace.json`：使用本 fork 的名称和仓库地址；禁用 Dart MCP 分析工具。
- `.lsp.json`、`bin/dart-lsp`：注册原生 Dart analysis server LSP，并按项目 FVM 配置选择 SDK。
- `bin/dart-lsp-idle`：客户端一段时间没有发消息（不含 analysis server 主动推送的通知）时关闭 analysis server 的 stdio 代理。
- `hooks/hooks.json`、`hooks/lsp_nudge.py`：LSP-vs-grep 引导 hook，见下文「LSP 引导 hook」。
- `ht/tests/test_dart_lsp.sh`：验证 SDK 选择与 LSP 启动行为。
- `ht/tests/test_dart_lsp_idle.py`：验证 `bin/dart-lsp-idle` 的空闲退出逻辑。
- `ht/tests/test_lsp_nudge.py`：验证引导 hook 的检测逻辑。
- `HT.md`：本说明。

## 为什么拆分分析

2026-09-24 实测，一个 Flutter 会话里 Dart MCP 与 LSP 各自启动一份 analysis server；单份稳态约 400–700 MB，峰值约 1 GB。旧版 SDK 的 Dart MCP server 会在启动时立即启动 analysis server 并让它常驻。由原生 LSP 承担代码分析、Dart MCP 只保留其他工具，可避免同一会话重复运行分析服务。

## 工具分工

- 定义、引用、调用层级、符号、hover：使用原生 `LSP` 工具。工具是 deferred 的；需要时执行 `ToolSearch select:LSP`。
- 修改 `.dart` 后检查错误：在 shell 运行 `flutter analyze`。
- 运行时错误、DTD、pub：使用 Dart MCP。
- `.lsp.json` 设置 `diagnostics: false`，避免存量警告持续刷屏；需要诊断时主动运行 `flutter analyze`。

## LSP 引导 hook

2026-09-28 实测（`.omc/research/agent-lsp-trigger-patterns.md`、
`.omc/research/lsp-missed-opportunities.md`）：几个 Flutter 会话里模型调用原生 `LSP`
工具 0 次，同期跑了约 160 次/天的 grep；一条 CLAUDE.md 规则不起作用。研究显示比较式措辞
（"findReferences 比 grep 更准，因为…"）+ 反合理化清单（Serena 的做法）比祈使句更有效，
且不应拦截或改写命令——因此本仓没有做严格的 PreToolUse deny/rewrite，只做不阻塞的提示。

`hooks/lsp_nudge.py`（纯 stdlib、无外部依赖）处理两类事件：

- **SessionStart**：cwd 在 Dart 项目内（向上找最近的 `pubspec.yaml`）时，注入一段约
  120 词的 `additionalContext`：LSP 各操作相对 grep 的优势、如何启动
  （`ToolSearch select:LSP`）、反合理化清单（"文件小""grep更快""已经知道名字"等不是跳过
  LSP 的理由），以及 grep 仍然合适的场景（字符串字面量、JSON/arb/l10n key、注释/TODO、
  非 Dart 文件、过滤别的工具输出、未检出分支的 git 历史）。
- **PreToolUse**（`Bash`/`Grep`/`Read`）：只在窄范围、高精度的形状上给出 ≤60 词的短提示，
  从不 deny、从不设置 `permissionDecision`、从不改写工具输入：
  - `Bash`/`Grep` 对 Dart 源码（`.dart` 作为真实路径/glob 后缀、`lib` 作为路径分段、
    `--include=*.dart` / `--type dart` / `-t dart` 等标志）的 grep/rg，且至少一个模式
    分支形似 Dart 标识符（camelCase/PascalCase、`Name(`、`class X`/`extends X`/
    `implements X`/`with X`）时触发，按形状建议
    `goToDefinition`/`goToImplementation`/`findReferences`。明确排除：过滤其他工具输出
    的 grep（`flutter analyze | grep` 等）、纯 snake_case 的 JSON key、任意 `git` 子命令
    （`git grep`/`git show`/`git log -S`/`git ls-tree`，不论有没有 ref、是不是 `origin/`
    分支）、通用词（见脚本内 `GENERIC_WORDS`）、框架生命周期/重写方法（`fromJson`、
    `toJson`、`setState`、`build`、`initState`、`dispose`、`copyWith`、
    `didChangeDependencies`、`didUpdateWidget`、`notifyListeners`、`toString`、
    `hashCode` 等，见 `FRAMEWORK_NOISE_WORDS`——这些方法到处都有重写，findReferences 噪音
    太大）、显式指向 `.arb`/`.json`/`.yaml`/`.yml`/`.md`/`.txt` 的目标（即使路径经过
    `lib/`，例如 `grep loginTitle lib/l10n/intl_en.arb` 或 `--include=*.json`）。
    `Grep`/`Read` 的范围只按工具调用点名的目标路径判定（`path`/`file_path`），不回退到
    `cwd`；`Bash` 仍按 `cwd` 判定，因为它没有结构化的目标路径。
  - `Read` 一个 ≥250 行（行数计数到 250 即停止，提示统一说「≥250 行」而非精确行数）、
    未指定 `offset`/`limit`、开头 8KB 内不含 NUL 字节（避免误判二进制/损坏文件）的
    `.dart` 文件时，建议先用 `documentSymbol` 看大纲。
  - 检测规则来自 `lsp-missed-opportunities.md`「Mechanical detectability at
    PreToolUse time」一节，在 `ht/tests/test_lsp_nudge.py` 里用一份≥40条、逐条标注出处
    的真实命令回放集固定验证（精度门槛 90%）。

关闭：设置环境变量 `DART_LSP_NUDGE=0`（两个事件都会静默不输出）。

## SDK 选择和限制

`bin/dart-lsp` 按顺序选择 Dart SDK：项目 `.fvmrc` → `.fvm/fvm_config.json` → FVM default → `PATH`。Dart MCP 的 command 仍是 `PATH` 上的 `dart`，需要 Dart ≥ 3.12 才支持 `--disable` 参数；全局 FVM 切回旧版时 MCP 会启动失败。LSP 与 MCP 在不同 Claude 会话中仍各有一份 analysis server；上游 `dart-lang/sdk#62606` 尚未落地。

## 空闲自动退出

`bin/dart-lsp` 默认经 `bin/dart-lsp-idle`（纯 Python 标准库、无新依赖）包一层再启动真正的 analysis server：代理原样转发 LSP 消息，同时用 `Content-Length` 帧解析观察双向消息，统计还没收到响应的 in-flight 请求。空闲计时只看**客户端**发来的消息——analysis server 自己主动推的 `publishDiagnostics`/`$/analyzerStatus` 等通知（改文件、`git checkout`、`pub get`、跑 build 都会触发）不会刷新计时，否则一个正在忙的 worktree 永远不会被判定为空闲。空闲达到 `DART_LSP_IDLE_SECONDS`（默认 600 秒，从客户端最后一条消息算起）且没有 in-flight 请求（双向都算：客户端请求等服务端响应、服务端请求等客户端响应）时，代理向 analysis server 发 `shutdown`/`exit` 后退出，会话保持连接的 stdio 随之关闭。

- `DART_LSP_IDLE_SECONDS=0` 关闭这层代理，恢复直接 `exec` analysis server（不占用额外进程，也不会空闲退出）。
- 不是非负整数时回退到默认 600 秒，并在 stderr 打印一行警告。
- PATH 上找不到可用 `python3`（`command -v` 找到但实际跑不动，例如 macOS Xcode CLT 的 stub、没配置版本的 pyenv shim）时自动跳过代理，直接 `exec`，并在 stderr 打印一行提示（行为等价于设为 0）。
- 空闲退出后，Claude Code 在下一次调用 `LSP` 工具时会透明重新拉起 analysis server（已实测连续 4 次杀进程都能重连），代价是几秒冷启动，换来空闲会话零常驻内存。Claude Code 会把每次空闲退出记成一次 `lsp_server_crashed`，是纯 cosmetic 的日志措辞（这里是代理主动、优雅关闭，不是真崩溃）；只要下一次调用成功重新拉起，它自己的崩溃计数就会清零，不会攒够次数触发 `maxRestarts` 熔断。

## 同步上游

运行 `git fetch upstream && git merge upstream/main`。如有冲突，只在 `.claude-plugin/plugin.json` / `.claude-plugin/marketplace.json` 解决：保留本 fork 的 `name`、`version` 和 MCP `args`，吸收上游其他字段。合并后按上游新版本号更新 `version` 的版本前缀，并保留 `-ht.N` 后缀，例如上游从 `1.0.5` 升级时将其更新为对应的新 `X.Y.Z-ht.1`。

## 安装

在 Claude Code 中运行 `/plugin marketplace add askmegit/dart-flutter-ht`，再运行 `/plugin install dart-flutter-ht@dart-flutter-ht`。同时卸载官方 `dart-flutter` 和独立 `dart-lsp` 插件，避免重复启动 MCP/LSP 或 analysis server。
