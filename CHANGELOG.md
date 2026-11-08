# 更新日志

## 1.0.10 · 2026-11-08

### 新功能

- **新增 LLM 工具 `llm_recall_and_ban`**：一步完成「撤回某人的消息 + 禁言该用户」，不用再走「先查消息 ID → 再撤回 → 再禁言」三段式。
  - 参数：`user_id`（必填）、`count`、`message_ids`、`duration`、`reason`、`need_auth`
  - `duration` 填 `0` = 只撤回不禁言；留空 = 使用随机时长
  - 撤回与禁言**各自独立执行**，任一失败不影响另一项，结果分两行汇报
  - 撤回只能撤最近窗口内的消息（NapCat `get_group_msg_history` 上限），超窗口之外的旧消息撤不动，会明确提示「未撤到消息」


## 1.0.9 · 2026-11-08

### 清理

- **删除死代码 `core/llm_handle.py`（165 行）**：`LLMHandle` 类全库零引用（`ai_set_card` / `ai_set_title` / `get_llm_nick` / `parse_args` 均只有定义无调用），是原插件遗留的「让 LLM 改名片/头衔」功能，重构时没接进 `main.py`
- **删除 `data.py: remove()`**：列表字段删除方法零调用，实际删除逻辑走 `add()` 的反向或直接 `set()`

### 修复

- **`全禁` 别名重复**：原写 `alias={"全员禁言", "全员禁言"}`，集合去重后只有一个别名；改为 `alias={"全员禁言", "群全员禁言"}`
- **6 条官方平台专属命令在 OneBot 下静默无响应**：`群管帮助`、`群管状态`、`禁言状态`、`入群申请`、`群黑名单`、`解除群拉黑` 原本只有 `if is_official(event):` 分支且 `event.stop_event()` 在分支内，OneBot 用户发这些命令后什么也收不到 —— 现在补 `else` 分支统一回一句「XX 是 QQ 官方机器人专用指令，当前 NapCat / OneBot 平台不适用」

### 新增

- **`HELP_ONE` OneBot 版帮助**：`/群管帮助` 在 OneBot 平台原本直接 return（`HELP` 只写了官方机器人的命令集），现在会返回覆盖本平台全部 39 条可用命令的分类清单

### 说明

- `群管帮助` 的 `event.stop_event()` 从 `if` 内部移到 `if/else` 之后，保证两个平台都会终止事件传播


## 1.0.8 · 2026-11-07

### 新功能

- **新增 LLM 工具 `llm_download_group_file`**（仅 LLM 工具，不提供群命令）：把群文件下载到本地并作为文件消息发回群聊。
  - 参数 `path` 支持文件名、`文件夹名/文件名`，或 `llm_view_group_file` 返回的序号（`"3"` / `"2/1"`）
  - 权限收紧为「超管 ∪ 特殊权限名单」，**群主/管理员不在放行范围**（只能 `llm_view_group_file` 看清单，不能下载）
  - 只发包，不暴露下载直链
  - 体积上限可配（`download_max_mb`，默认 100 MB），超限直接拒绝，避免把大文件拖到宿主
  - `core/file_handle.py` 新增 `_locate_file` / `_get_group_file_url` / `download_group_file`；其中 `_locate_file` 复用了原先散在 `view_group_file` 里的定位逻辑

### 细节

- `get_group_file_url` 兼容 `{"url": ...}` 与 `{"data": {"url": ...}}` 两种返回包裹
- `busid` 缺失时补 NapCat 默认值 `102`
- 文件名里的路径分隔符会被替换成 `_`，防止写出缓存目录
- 下载结果为 0 字节时自动清理并报错，不静默返回空文件
- `core/file_handle.py` 补 `from pathlib import Path` 导入（新方法用了 `Path | None` 注解）
- `permission.py` 新增 `is_user_strict_admin()`（超管 ∪ 名单，不含群主/管理员），供高成本工具复用

## 1.0.7 · 2026-11-03

### 修复

- **群文件大小显示为 0.00 KB**：`core/file_handle.py` 读的是 `size` 字段，而 NapCat / Lagrange 返回的是 `file_size`，取不到就落到默认值 0，于是所有文件都被显示成 0.00 KB。
  - 新增 `_parse_size()`：按 `file_size` → `size` → `fileSize` 顺序探测，兼容各协议端字段命名；
  - 字段全缺时显示「未知（协议端未返回大小字段）」，不再谎报成 0；
  - 真实的 0 字节文件仍正确显示 `0 B`。
- 新增 `_human_size()`：单位扩展到 B / KB / MB / GB，1 KB 以下按字节显示，不再一律除以 1024 显示成「0.00 KB」。

## 1.0.6 · 2026-11-03

### 新功能

- **`instant_unban` 支持分人执行**（命令与 LLM 工具均支持）：
  - 命令：末尾加开关词 `玩,真`，与目标按位置对应
    - `禁言 123,456 1h,2h 玩,真` → 123 吓唬后解禁，456 真禁 2 小时
    - `娱乐式禁言 123,456 1h 玩,真` → 同上
  - LLM：`llm_set_group_ban` 新增 `instant_unbans` 参数，填 `"true,false,true"`
- 开关词表：`玩 / 假 / 吓 / 吓唬 / 玩笑` = 立刻解禁；`真 / 正 / 实 / 正常` = 正常禁言

### 细节

- `parse_ban_args` 返回值从 2 元组扩为 3 元组（多出 flags 列表）
- 出现开关词时，纯数字一律当 QQ 号（`禁言 123 玩` 正确识别为吓唬 123）
- 开关数量少于目标数量时直接报错，不静默套用首个值，避免「该真禁的被当吓唬」
- `core/normal_handle.py` 的 `set_group_ban` 新增 `instant_unbans` 参数，`instant_unban` 类型放宽为 `bool | list | tuple | str`

## 1.0.5 · 2026-11-03

### 新功能

- **禁言支持批量与逐人时长**（命令与 LLM 工具均支持）：
  - 命令：`禁言 123,456 1h` 两人共用；`禁言 123,456 1h,2h` 逐个对应
  - `娱乐式禁言` 同样支持批量，语义一致（每人各发一次立刻解禁）
  - LLM：`llm_set_group_ban` 的 `user_id` 可填 `123,456,789`，新增 `durations` 参数填 `1h,2h,3h` 逐人设时长
- `utils.py` 新增三个解析助手：`split_list_arg` / `pair_targets_and_times` / `parse_ban_args`

### 细节

- 时长少于目标数量时报错并指出差额，不会静默乱配
- 单个目标失败不影响同批其他人，逐条回执
- 分隔符兼容英文逗号、中文逗号、顿号
- `llm_set_group_ban` 的 `duration` 类型由 number 改为 string，可直接传 `"1h"` 等单位串（纯数字仍按秒）

## 1.0.4 · 2026-11-01
### 删除项
- 删除**宵禁**功能：指令「开启宵禁」「关闭宵禁」、LLM 工具 `llm_start_curfew` / `llm_stop_curfew`、`core/curfew_handle.py`、`curfew_data.json` 持久化及相关配置。
- 删除**违禁词/刷屏禁言**功能：指令「禁词禁言」「设置禁词」「内置禁词」「刷屏禁言」、消息事件监听 `on_ban_words` / `spamming_ban`、`core/banpro_handel.py`、内置词库 `SensitiveLexicon.json` 及相关配置项（`builtin_ban` / `custom_ban_words` / `word_ban_time` / `spamming_ban_time`）。
- 同步清理 `config.py` 的 `ban_lexicon_path` / `curfew_file` / `spamming_count` / `spamming_interval`、`data.py` 字段中文映射、`_conf_schema.json` 四个配置项、README / OFFICIAL.md 相关描述。
### 结果
- 群聊命令由 50 条降到 43 条，LLM 工具由 23 个降到 21 个。

## 1.0.0 · 2026-11-01

全新插件 `astrbot_plugin_groupadmin`，基于 `astrbot_plugin_qqadmin`（v3.4.1）重构。

### 权限体系重构

- 引入「特殊权限名单制」四级权限模型：
  - AstrBot 超级管理员：一切权限 + 各群特殊权限名单管理；
  - 特殊权限名单成员（每群独立，数据库 `special_admins` 字段，QQ 号与官方 OpenID 同列表）：可执行本群所有群管指令，不受群主/管理员身份限制，名单成员自身仍可被操作；
  - QQ 群主/管理员：基础管理指令 + 查询指令；
  - 普通成员：仅查询类指令（群友信息/群公告查看/群文件列表）。
- 新增超管指令「特殊权限 查看/添加/删除/清空」与对应 LLM 查询工具，用于管理各群名单。
- 新增配置项 `default.items.special_admins`（每群名单，默认群中为新群模板值）与顶层 `official_special_admins`（QQ 官方机器人启用名单，默认开）。

### 删除项

- 删除 `perms`（每命令权限门槛）配置：共 30 个子命令权限项，不再支持逐命令调整权限等级。
- 删除 `vote_ban`（投票禁言）及其 ttl/threshold 配置与相关指令。
- 删除 `level_threshold`（高等级成员阈值）与 `join_min_level`（进群等级门槛）配置，移除整个等级权限体系及「进群等级」指令。

### 其他

- WebUI 每群配置表单新增「特殊权限名单」编辑框。
- 命令清单按管理类/查询类/超管类重新分组，README 重写。

## v3.4.1 · 2026-10-06

- 新增 QQ 官方群聊成员禁言、解禁、移出、黑名单和成员查询、手动入群审批的独立 API 适配。
- 保留管理员授权、群身份及平台权限检查；官方 OpenID 不与 OneBot QQ 号混用。
- 未开放或未适配的原功能明确提示，保留 OneBot 处理路径；详见 OFFICIAL.md。
- QQ 官方修改通过离线模拟，实际上线仅做只读权限检查。

## v3.4.0

New Features:
- 将多种 QQ 群管理操作暴露为 LLM 工具，并在其中内置权限校验，用于静音、改名、头衔管理、公告、宵禁和群文件等操作。
- 增加可配置的群管理指令，通过聊天命令实现按群导出、导入和重置配置。
- 引入专门的 RecallHandle 处理消息撤回，并接入主插件。

Bug Fixes:
- 修复入群审批通知的格式，避免出现多余的空行。
- 确保成员清理日志在尝试踢人前先记录群昵称，避免汇总中缺失名称。

Enhancements:
- 标准化管理指令处理逻辑，让其返回可读的文本结果而不是直接发送消息，以便同时复用在聊天命令和 LLM 工具中。
- 简化并整合日常群管理操作，如单人禁言、全员禁言、群昵称、头衔、管理员、精华、头像以及群名处理，并提供更清晰的反馈信息。
- 优化宵禁、公告、入群、成员清理和群文件管理流程，使其返回文字摘要并提供更好的错误信息。
- 调整布尔值解析和权限辅助方法，以支持 LLM 专用的权限校验路径。
- 更新文档以反映新的指令集，标记哪些指令暴露为 LLM 工具，并引导用户使用新的帮助命令。

Documentation:
- 修改 README 中的指令表，记录更新后的指令名称、别名，并标明哪些指令可作为 LLM 工具使用，同时更新帮助用法说明。

Chores:
- 移除未使用的 logo 打印和禁我功能，并从核心插件导出中删除旧版 LLMHandle，相应功能已被LLM工具等位替代。

## v3.3.0

新功能:

- 引入一个 Web 设置页面（HTML/CSS/JS），用于浏览 QQ 群并编辑各群的管理配置，支持跟随默认模板。
- 暴露用于设置页面的 HTTP API，用来加载 schema/初始化数据、刷新群列表，以及获取/更新/重置群配置。
- 新增群信息缓存服务，用于从各适配器聚合 QQ 群元数据，并向插件提供统一且带缓存的视图。

增强:

- 优化 QQAdminDB，区分“跟随默认模板”的群记录与“显式配置”的群记录，提供规范化的群快照，并支持替换或重置群配置。
- 允许按群覆盖关键行为（随机封禁时间范围、投票封禁参数、LLM 消息窗口、权限阈值与映射、`admin_audit`），同时在 `PluginConfig` 中保留默认模板。
- 更新权限检查逻辑，从群配置而非全局配置中读取所需等级和阈值，并移除对私聊中执行受权限保护命令的支持。
- 调整入群、普通、banpro 和 LLM 相关的处理器，通过共享的群配置快照遵从各群的配置值。
- 改进 `PluginConfig` 的运行时处理，提供更安全的随机封禁时间解析，并增加用于构建默认群配置结构的构建器。
- 新增一个 Web 控制器，将插件专用路由注册到 AstrBot 上下文中，并统一 JSON 响应格式与错误处理。

文档:

- 添加一个简单的 CHANGELOG，记录在 v3.3.0 版本中引入 Web 前端的变更，同时更新Readme文档。

杂项:

- 将插件元数据版本升级至 v3.3.0，声明最低 AstrBot 版本要求，并移除旧的文本版 `ADMIN_HELP` 及其命令入口。