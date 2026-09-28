# dart-flutter-ht

这是官方 `flutter/agent-plugins` 的 `dart-flutter` 1.0.6 fork，增加原生 Dart LSP，并让 Dart MCP 以 `--disable analysis` 运行。除下列文件外，其余上游内容保持不动：

- `.claude-plugin/plugin.json`、`.claude-plugin/marketplace.json`：使用本 fork 的名称和仓库地址；禁用 Dart MCP 分析工具。
- `.lsp.json`、`bin/dart-lsp`：注册原生 Dart analysis server LSP，并按项目 FVM 配置选择 SDK。
- `bin/dart-lsp-idle`：客户端一段时间没有发消息（不含 analysis server 主动推送的通知）时关闭 analysis server 的 stdio 代理。
- `hooks/hooks.json`、`hooks/lsp_nudge.py`：LSP-vs-grep 引导 hook，见下文「LSP 引导 hook」。
- `hooks/hooks.json`、`hooks/stop_analyze.py`：收尾前 Dart 分析闸门，见下文「收尾分析闸门」。
- `ht/tests/test_dart_lsp.sh`：验证 SDK 选择与 LSP 启动行为。
- `ht/tests/test_dart_lsp_idle.py`：验证 `bin/dart-lsp-idle` 的空闲退出逻辑。
- `ht/tests/test_lsp_nudge.py`：验证引导 hook 的检测逻辑。
- `ht/tests/test_stop_analyze.py`：验证收尾分析闸门的检测逻辑。
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

## 收尾分析闸门

**是什么**：`hooks/stop_analyze.py` 在 `PostToolUse`（`Edit`/`Write`/`MultiEdit`；`NotebookEdit`
从不带 `.dart` 路径，不跟踪）记录本轮改过的 `.dart` 文件，再在 `Stop`/`SubagentStop`（模型准备
结束这一轮时，包括这个 hook 上次挡完之后 Claude Code 自动发起的重试，即 `stop_hook_active`
为真的那次）按最近的 `pubspec.yaml` 分组，对每个改动过的包根跑一次
`dart analyze --format=machine .`（整包分析，连带把改动在别的文件里引出的报错也一起抓到），
只看 `ERROR` 级别。包根下嵌套的子包（比如自带 `pubspec.yaml` 的 `example/`，依赖多半没装）
即使被 `dart analyze` 扫到也一律不算数：解析结果按每条诊断自己最近的 `pubspec.yaml` 核实，
不属于当前改动包根的一律丢弃，避免无关子包的假错误把这轮结束挡住（曾经还想过顺带缩小
`dart analyze` 的扫描目标，评审实测没什么收益，已去掉——固定跑整个目录，靠这层过滤兜底）。

**判重不判"有没有错"**：每条存活错误按 `相对路径|code|message` 算一个指纹（**不含行号**——同一
文件里更早处的改动会让后面所有行号整体偏移，如果指纹里带行号，一个模型完全没碰过的老错误会
因为纯粹的行号漂移在 `stop_hook_active` 重试时被误判成"新错误"，逼模型为一个自己管不了的东西
再挨一次堵；`code + message` 在实践中已经足够代表"同一个诊断"，reason 文本里给模型看的
`line:col` 仍然是当次的真实行号，只是不参与判重）。每个"范围"（主线程一份，每个仍在跑的
subagent 各一份）都会持久化一份"已经报过"的指纹集合 B：

- 非 `stop_hook_active`（这一轮第一次检查）：当前错误集合 E 为空 → 放行、清掉这个范围的记录
  （已改动文件列表 + B）；E 非空 → 挡住，`reason` 列出全部错误，并把 B 写成 E。
- `stop_hook_active`（自动重试）：NEW = E − B。NEW 非空 → 挡住，但 `reason` 只列 NEW 里的新
  错误（B 已经报过的不重复烦模型），然后把 B 更新成 B ∪ E；NEW 为空 → 放行并清掉这个范围的
  记录——E 里剩下的错误模型要么已经修好，要么等于隐式/显式声明"这个是历史遗留、不归我管"，
  不会揪着同一条错误无限重试。Claude Code 自己对连续挡回合有次数上限（8 次），就算这个 hook
  一直挡，也不会真死循环；只是万一撞到那个上限，这一轮结束时范围记录可能还没清干净，不影响
  下一轮——下一轮的检查永远是从头算一遍新的 E，不依赖上一轮没走完的状态。

`reason` 里改动过的文件的报错排在前面，总共最多 30 行（超出显示 `+N more`），末尾附一句
「Fix these before finishing. If an error pre-existed and is unrelated to your change, say so
and stop again.」。

**跑不出结果的包根怎么算**：`dart analyze` 退出码 0–3 是正常范围，机读输出照单全收；退出码
64（用法错误）视为这个包根"跳过"；其他退出码如果解析不出任何诊断，也按同一逻辑跳过（判定为
工具本身没跑起来，而不是包本身干净）；SDK 本身解析失败或没解出可用路径（`dart-lsp` 报错/空输出）
同样算一次跳过。跳过只影响"整个范围一条错误都没有"这个结果该怎么解读，从不直接导致挡住或放行
——范围里只要有任何包根跑出真错误，该错误依然正常挡住，不受别的包根被跳过影响。"一条错误都没有"
时是否放行要分两种情况：这一轮第一次检查（非 `stop_hook_active`）时，如果这是因为有包根被跳过
（而不是真的全部跑干净），就不放行、记录原样留着，等下一次真正跑出结果再判；但
`stop_hook_active` 重试时，只要没有新错误（NEW 为空）就无条件放行并清记录，哪怕这一轮同样有
包根被跳过——重试这一轮如果还剩什么错误，那都是上一轮已经报过的，跳过一个不影响这个结论的
包根不该让同一条已经报过的旧错误在后面每一轮都重新把这个文件挡住。

**pub workspace**：一个包声明了 `resolution: workspace` 但自己没有
`.dart_tool/package_config.json` 时，不代表依赖没装——pub workspace 的依赖只在 workspace 根
（`pubspec.yaml` 里带 `workspace:` 的那层）解析一次。跳过判断会额外往上找最近一层声明了
`workspace:` 的祖先目录，那里的 `.dart_tool/package_config.json` 存在就照常分析（`dart
analyze` 仍以该成员包自己的目录为 cwd）。

**后台 subagent**：一个 subagent 自己的 `SubagentStop` 通过（或超时放弃）只会清掉它自己那份
`已报过` 指纹集合，并记一个"已完成"标记——它记录过的改动**不会**被删除，还留着、仍标着它的
`agent_id`，等主线程之后的 `Stop` 来吸收、亲自再查一遍。主线程的 `Stop` 只看 `agent_id` 为空
（主线程自己的改动）加上已经标记"已完成"的 subagent 留下的改动；一个还没跑到自己
`SubagentStop` 的 subagent，它的改动对主线程的 `Stop` 完全不可见——既不会被拿去分析，也不会
被清掉。主线程自己的 `Stop` 放行时，会把这次吸收进来的所有改动记录（连同它们的"已完成"标记）
一起清掉。`SubagentStop` 事件本身没带 `agent_id`（Claude Code 内部的一些辅助 agent 会这样）
时直接放行，不去动主线程或别的 agent 记录的改动。

如果一个 subagent 自己的 `SubagentStop` 在重试时判定"没有新错误"而放行（比如它认为某条错误是
历史遗留、不归它管），这只是它自己这个范围不再重复提醒——它留给主线程吸收的"已完成"标记照样
会写，等主线程之后自己的 `Stop` 检查到同一批改动时，会从头再判一次（主线程自己的"已报过"集合
是空的），同一条错误完全可能在主线程那边再报一次。这是有意为之，不是 bug：subagent 决定不再为
一件它已经提醒过一次的事情继续烦它自己，不等于主线程也认可这件事没问题——主线程理应有自己独立
判断一次的机会。

一个 subagent 的改动记录只有两条路径会被清掉：（a）它自己标记"已完成"之后，被后续某次主线程
`Stop` 吸收；（b）7 天清理。如果一个 subagent 始终没能带着"干净"结果走到自己的 `SubagentStop`
——比如一直卡在 8 次连续挡回合的上限、或者每次检查都超时、或者每次都撞上跳过（见上文"跑不出
结果的包根怎么算"）——那就永远不会写"已完成"标记，主线程也就永远不会去吸收它，它的记录会一直
留在会话状态文件里，直到 7 天清理把它连带清走。这是刻意接受的简化：没有再单独给 subagent 做一条
专属的过期路径。

已知限制：如果 Claude Code 用同一个 `agent_id` 恢复一个已经标记"已完成"的 subagent 继续跑，它
在下一次自己的 `SubagentStop` 之前做的新改动，理论上可能被中间插入的一次主线程 `Stop` 提前吸收
走——这个 hook 没有 `SubagentStart` 事件可用，无法侦测到"恢复"并撤回那个标记。

**为什么**：模型收尾时经常忘了跑 `flutter analyze`，留下编译不过的 Dart 代码。每次改完就查
诊断在之前的评审中被否决——编辑中途的代码本来就合法地处于半成品状态（重命名改了一半、新
调用点先加后补实现），逐次提醒只会刷噪音；只在模型准备收尾时查一次，语义上更贴近「这轮活干
完了应该是能跑的」。判重逻辑是因为早期版本在 `stop_hook_active` 时直接无条件放行——模型的
修复到底有没有生效、或者它是不是把一个历史遗留错误当成"已声明豁免"，都完全没人再检查；而且
被判定"历史遗留"的记录会一直留着，导致后面每一轮只要碰过这个文件就要重新付一次分析加一次
强制往返的代价。

**超时**：所有包根共用一个整体截止时间（`DART_STOP_ANALYZE_TIMEOUT`，默认 100 秒，留出余量给
`hooks.json` 里 120 秒的 `Stop`/`SubagentStop` 超时），SDK 解析也算在这个预算里；超时会放行并
清掉这个范围的记录（不留着让下一轮继续背这个查不完的债）。每个子进程（SDK 解析、
`dart analyze`）单独起一个进程组，个体超时时整个进程组一起杀掉，避免 `dart analyze` 派生出的
analysis server 常驻进程变孤儿。

**状态清理**：每次 `PostToolUse` 顺带做一次机会性清理（每小时最多一次，用一个时间戳文件节流）：
任何会话的状态文件（`.jsonl`、它的锁文件、按范围存的已报过指纹快照）只要超过 7 天没更新就一起
删掉，年龄判断以那份 `.jsonl` 自己的 mtime 为准（不用锁文件的 mtime——`flock` 本身不会更新锁
文件的 mtime，用它判断容易把一个还在用、只是刚好没改动的会话误删）。会话的锁文件**不会**在
状态被清空的当下跟着删——这时候可能正有另一个进程卡在打开同一把锁的路径上，此时把路径删掉会
把锁"劈成两半"（删的人和刚好在这个空档新建同名文件加锁的人，各自锁住的其实是两个不同的 inode，
后续互不排斥），锁文件只在 7 天清理时才会被清掉，那时候不会再有进程试图打开它。按范围存的
已报过指纹快照文件本身不加锁——同一个范围（主线程一份，每个仍在跑的 subagent 各一份）任一时刻
只可能有一个写者，没有需要锁保护的读写重叠。

**关闭**：设置环境变量 `DART_STOP_ANALYZE=0`（三个事件都静默不输出/不记录）。

**已知限制**：只跟踪经 `Edit`/`Write`/`MultiEdit` 工具改的 `.dart` 文件——用 `Bash` 里的
`sed`/Python 脚本等方式改写 `.dart` 文件不会被这里记录，也就不会单独触发收尾重查（如果同一轮
还有别的文件是经工具改的，那些改动仍会照常触发）。

## SDK 选择和限制

`bin/dart-lsp` 按顺序选择 Dart SDK：项目 `.fvmrc` → `.fvm/fvm_config.json` → FVM default → `PATH`。Dart MCP 的 command 仍是 `PATH` 上的 `dart`，需要 Dart ≥ 3.12 才支持 `--disable` 参数；全局 FVM 切回旧版时 MCP 会启动失败。LSP 与 MCP 在不同 Claude 会话中仍各有一份 analysis server；上游 `dart-lang/sdk#62606` 尚未落地。

## 空闲自动退出

`bin/dart-lsp` 默认经 `bin/dart-lsp-idle`（纯 Python 标准库、无新依赖）包一层再启动真正的 analysis server：代理原样转发 LSP 消息，同时用 `Content-Length` 帧解析观察双向消息，统计还没收到响应的 in-flight 请求。空闲计时只看**客户端**发来的消息——analysis server 自己主动推的 `publishDiagnostics`/`$/analyzerStatus` 等通知（改文件、`git checkout`、`pub get`、跑 build 都会触发）不会刷新计时，否则一个正在忙的 worktree 永远不会被判定为空闲。空闲达到 `DART_LSP_IDLE_SECONDS`（默认 600 秒，从客户端最后一条消息算起）且没有 in-flight 请求（双向都算：客户端请求等服务端响应、服务端请求等客户端响应）时，代理向 analysis server 发 `shutdown`/`exit` 后退出，会话保持连接的 stdio 随之关闭。

- `DART_LSP_IDLE_SECONDS=0` 关闭这层代理，恢复直接 `exec` analysis server（不占用额外进程，也不会空闲退出）。
- 不是非负整数时回退到默认 600 秒，并在 stderr 打印一行警告。
- PATH 上找不到可用 `python3`（`command -v` 找到但实际跑不动，例如 macOS Xcode CLT 的 stub、没配置版本的 pyenv shim）时自动跳过代理，直接 `exec`，并在 stderr 打印一行提示（行为等价于设为 0）。
- 空闲退出后，Claude Code 在下一次调用 `LSP` 工具时会透明重新拉起 analysis server（已实测连续 4 次杀进程都能重连），代价是几秒冷启动，换来空闲会话零常驻内存。Claude Code 会把每次空闲退出记成一次 `lsp_server_crashed`，是纯 cosmetic 的日志措辞（这里是代理主动、优雅关闭，不是真崩溃）；只要下一次调用成功重新拉起，它自己的崩溃计数就会清零，不会攒够次数触发 `maxRestarts` 熔断。

## 同步上游

分支：`main` 只镜像上游，不放本 fork 的提交；`ht` 是本 fork 的改动，也是 GitHub 默认分支（插件市场按默认分支安装）。

1. 同步 `main`：GitHub 网页点 Sync fork，或 `git fetch upstream && git push origin upstream/main:main`（永远是快进）。
2. 合进 `ht`：`git checkout ht && git merge upstream/main`。用 merge 不用 rebase，`ht` 已发布，不改写历史。
3. 如有冲突，一般只在 `.claude-plugin/plugin.json` / `.claude-plugin/marketplace.json` / `HT.md`：保留本 fork 的 `name`、`version` 和 MCP `args`，吸收上游其他字段。按上游新版本号更新 `version` 前缀并把后缀重置为 `-ht.1`，例如上游 `1.0.6` → `1.0.6-ht.1`。
4. 跑 `sh ht/tests/test_dart_lsp.sh && python3 -m unittest ht.tests.test_dart_lsp_idle ht.tests.test_lsp_nudge`，通过后 `git push origin ht`。

本 fork 改了什么：`git log main..ht` / `git diff main...ht`。

## 安装

在 Claude Code 中运行 `/plugin marketplace add askmegit/dart-flutter-ht`，再运行 `/plugin install dart-flutter-ht@dart-flutter-ht`。同时卸载官方 `dart-flutter` 和独立 `dart-lsp` 插件，避免重复启动 MCP/LSP 或 analysis server。
