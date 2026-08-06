# rikkelivo 转换规格说明

本文档描述 rikkelivo 在 RikkaHub 与 Kelivo（cuplivo）之间的完整转换规格。
所有映射均以两个项目源码与真实备份为据。

## 1. 转换范围总览

| 类别 | 双向 | 说明 |
|---|---|---|
| 会话与消息 | ✓ | 含思考内容（reasoning）、用量、翻译、时间戳 |
| 附件 | ✓ | Kelivo `[image:]/[file:]` 标记 ↔ RikkaHub image/document part |
| 多版本分支 | ✓ | `groupId + version` ↔ `message_node`（select_index） |
| 群聊 | ✓ | Kelivo/cuplivo 独有；经 `x_rikkelivo` 侧车字段无损往返 |
| 工具调用记录 | ✓ | RikkaHub `tool` part ↔ Kelivo `toolEvents` |
| 数据库 | → | 生成与 RikkaHub 自身备份一致的 Room v24 SQLite |
| 提供商/模型 | ✓ | 内置匹配 + uuid5 确定性映射；显示名透传 |
| 助手 | ✓ | 含当前助手切换、记忆、技能、头像 |
| MCP | ✓ | 内置服务补全/移除；headers、工具参数双向 |
| TTS / ASR / 快捷短语 / 提示注入 / 世界书 / 搜索服务 | ✓ | `*_v1` ↔ 原生列表 |
| 用户信息 | ✓ | 昵称/头像 |
| S3 / WebDAV | ✓ | 备份目的地配置 |
| Skills | ✓ | `skills/` 目录原样拷贝 |
| 模型体系 | ✓ | 默认聊天/快速/标题/建议/翻译/OCR/压缩模型 |

## 2. 聊天记录

### 2.1 消息

文本/思考内容/用量/翻译全部保留。时间戳：两边均为无时区 ISO（毫秒），
透传；RikkaHub reasoning part 的 `createdAt`/`finishedAt` 为 Instant（需
时区），Kelivo 的 naive 时间按转换机本地时区转 UTC-Z。

### 2.2 附件

Kelivo 把附件以标记内嵌在消息 content 中：`[image:<路径>]`、
`[file:<路径>|<文件名>|<MIME>]`。RikkaHub 用 image/document part
（`url` 为 `file:///…/upload/<文件名>` 形式）。

- K2R：解析标记为 part；路径改写为 RikkaHub 的托管目录前缀
  （`file:///data/user/0/me.rerere.rikkahub/files/upload/…`）。
- R2K：part 转回标记；保留路径尾段（`/upload/` 等），Kelivo 的
  `SandboxPathResolver.fix()` 会映射到其数据目录。

### 2.3 多版本分支

- K2R：Kelivo 的 `groupId + version` 对应一个 `message_node`（节点内按
  version 排序，`select_index` 取 `versionSelections[groupId]`，缺省最后
  一条）；`ConversationEntity.nodes` 恒为 `[]`（与 RikkaHub 自身行为一致）。
- R2K：每个节点一个 group（节点 id 为 groupId），节点内消息按序为
  version 0..n，`versionSelections[groupId] = select_index`（>0 时），
  `messageIds` 只保留每组 version-0 代表消息。

### 2.4 群聊（Kelivo/cuplivo 独有）

RikkaHub 无群聊概念，降级迁移：

- K2R：群聊会话导入为普通会话；每条助手消息内容前加 Markdown 引用块
  （`> **发送者**:`），并在消息 JSON 附加 `x_rikkelivo` 侧车字段
  （`kind`/`speaker`/`contentOriginal`/`groupId`/`subgroupId`/`version`/
  `isPreset`，首条消息另带 `groupChat`/`groupMembers` 元数据）。RikkaHub
  解码 `ignoreUnknownKeys=true`，字段被忽略、显示正常。
- R2K：带侧车字段的消息还原原始内容、`speakerAssistantId`，重建
  `groupChats`/`groupMembers` 与 `conversationKind: "group"`。
- 转换后请在 RikkaHub 中把群聊会话指派给一个助手。
- 注意：若在 RikkaHub 中重新生成/编辑这些消息，侧车字段会被 RikkaHub
  自身的序列化丢弃，转回 Kelivo 后引用块退化为普通文本。

### 2.5 工具调用记录

Kelivo 以 `toolEvents: {messageId: [{id, name, arguments, content, metadata?}]}`
记录工具调用（数据在 `tool_event_rows` 表，导出到 chats.json）。

- R2K：RikkaHub `tool` part → `toolEvents[messageId]`（`arguments` 解析
  JSON、`output` 文本拼接为 `content`）。不再丢弃。
- K2R：`toolEvents` → `tool` part（含 `approvalState`/`metadata`）。

## 3. 数据库（RikkaHub 侧，K2R 生成）

与 RikkaHub 自身备份一致：

- 完整 Room v24 schema（`app/schemas/…/24.json`）；`PRAGMA user_version =
  24`；`room_master_table` 写入真实身份哈希 `0ea1aaebfa031c7995c45a1e35822e1a`
  —— 恢复后 Room 直接打开，不触发校验或迁移。
- 消息全文索引 `message_fts`（fts5，`simple` 分词，缺失时回退
  `unicode61`）填充文本/节点/会话/标题/更新时间，恢复即可搜索。
- `managed_files` 上传登记表按 `upload/` 目录生成（mime 由文件名推断，
  时间取 zip 条目时间）。
- 助手记忆写入 `MemoryEntity` 表。

## 4. 设置桥（尽力而为，有损）

### 4.1 共享标量键

同名同型键逐字拷贝（`themeId`、`dynamicColor`、`assistantId`、
`chatModelId`、`fastModelId`、各类 prompt、`webServer*` 等）。

### 4.2 提示词变量

- RikkaHub `PlaceholderTransformer` 同时渲染 `{{key}}` 与 `{key}`，键集：
  `char`/`user`/`nickname`/`cur_date`/`model_id`/`model_name`/`locale`/
  `timezone`/`system_version`/`device_info`/`battery_level`。
- Kelivo（cuplivo）系统提示词只渲染单括号 `{key}`，键集：
  `assistant_name`/`nickname`/`cur_date`/`cur_time`/`cur_datetime`/`model_id`/
  `model_name`/`locale`/`timezone`/`system_version`/`device_info`/
  `battery_level`。
- 转换：`{{char}} ↔ {assistant_name}`、`{{user}} ↔ {nickname}`，同名键在
  双括号/单括号之间互转，未知键原样保留；`messageTemplate` 的
  `{{ message }}` 两边一致，不做改动。作用于 5 个共享 prompt 键、助手
  `systemPrompt`、提示注入与世界书条目内容。

### 4.3 容器键映射（`*_v1` ↔ 原生列表）

| Kelivo（JSON 字符串） | RikkaHub（原生列表） |
|---|---|
| `provider_configs_v1` | `providers` |
| `assistants_v1` | `assistants` |
| `mcp_servers_v1` | `mcpServers` |
| `tts_services_v1` | `ttsProviders` |
| `quick_phrases_v1` | `quickMessages` |
| `instruction_injections_v1` | `modeInjections` |
| `world_books_v1` | `lorebooks` |
| `search_services_v1` | `searchServices` |

- **内置提供商**按 `(baseUrl, providerType)` / RikkaHub UUID 互相匹配，
  复用对方内置 ID（避免重复创建）；自定义提供商用 uuid5 确定性映射
  （保证 id 均为合法 UUID，避免 kotlinx/Uuid 解析崩溃）。
- **内置 MCP**：Kelivo 自带 `@kelivo/fetch`、`@kelivo/subagent`（启用）、
  `@kelivo/filesystem`（默认禁用）。R2K 自动补全三个内置；K2R 移除它们。
  MCP 请求头（RikkaHub `[{"first","second"}]` ↔ Kelivo Map）与工具参数
  （JSON Schema ↔ `params`）双向转换；Kelivo transport 枚举
  （`sse/http/stdio/inmemory`）↔ RikkaHub（`sse/streamable_http`）。
- 模型 ID 互解：Kelivo 模型名 ↔ RikkaHub 模型 UUID；显示名
  （`displayName` ↔ `modelOverrides.name`）透传（如「小赢鲸 Deepseek V4
  Flash」不退化）。
- **搜索服务**：类型兼容（`bing_local`/`tavily`/`exa`/`jina`/`ollama` 等）
  转成 `search_services_v1`（legacy `apiKey` 迁入 `apiKeys` 列表），同步
  `search_enabled_v1`/`search_selected_v1`/`search_common_v1` 使搜索生效；
  应用独有类型（如 RikkaHub `custom_js`、Kelivo `duckduckgo`）丢弃并警告。

### 4.4 助手

- 字段白名单转换，Kelivo 独有字段（`memoryMode`/`proactiveCare*` 等）丢弃
  或默认值。
- **当前助手**：R2K 时 `current_assistant_id_v1` 取 RikkaHub 默认助手
  `assistantId`（会话实际使用的助手），否则取首个转换助手；K2R 时
  `assistantId` 取 Kelivo `current_assistant_id_v1`。
- **记忆**：RikkaHub `MemoryEntity` ↔ Kelivo `assistant_memories_v1`
  （`{id, assistantId, content}`），assistantId 按转换后 id 映射。
- **技能**：`enabledSkills ↔ skillIds`（技能名列表）映射。

### 4.5 模型体系（fastModelId 等）

RikkaHub 模型键：`chatModelId`（默认聊天）、`fastModelId`（快速，标题/建议
未指定时的回退）、`titleModelId`、`suggestionModelId`、`translateModeId`、
`ocrModelId`、`compressModelId`。
Kelivo 对应：`selected_model_v1`（当前模型，一切回退的根）与
`title_model_v1`/`suggestion_model_v1`/`translate_model_v1`/`ocr_model_v1`/
`compress_model_v1`（`"providerKey::modelId"` 格式）。

- R2K：默认聊天模型 → `selected_model_v1`；标题/建议未指定则写入快速
  模型；翻译/OCR/压缩逐一解析映射。
- K2R：反向解析回各模型键；`chatModelId` 取 Kelivo 当前模型；`fastModelId`
  优先取标题模型（与 R2K 往返一致）否则取当前模型——保证引用始终可解析。
- `imageGenerationModelId`：Kelivo 无对应键（图像生成走工具能力），保持
  遗留键拷贝。

### 4.6 用户信息 / S3 / WebDAV

- 用户信息：`displaySetting.userNickname/userAvatar` ↔ `user_name`/
  `avatar_type`/`avatar_value`（emoji/url/file 三类头像）。
- S3：`s3Config` ↔ `s3_config_v1`（endpoint/region/bucket/accessKeyId/
  secretAccessKey/pathStyle 等）。
- WebDAV：`webDavConfig` ↔ `webdav_config_v1`（url/username/password/path/
  includeChats/includeFiles）。

### 4.7 类型安全（关键）

Kelivo 的恢复通过其自定义 `prefs.restore()` 逐键写入，只接受
`String/bool/int/double/List<String>`。RikkaHub 侧的对象数组键
（`searchServices`/`asrProviders`/`lorebooks`/`assistantTags`/
`customThemes` 等镜像键）在 Kelivo 设置中一律保持空列表（数据走 `*_v1`
字符串），避免整个设置恢复被中断。

### 4.8 目标应用独有键

（如 RikkaHub `displaySetting`/`s3Config`、Kelivo `display_*_v1` 系列）
使用目标应用自身默认值。

## 5. Kelivo 导入 MCP 的 BUG（重要）

Kelivo 启动时对启用中的 MCP 服务自动连接（`unawaited`），`connect()` 成功
后执行 `refreshTools() → _persist()`。恢复流程（`data_sync.dart`）通过
`prefs.restore()` 写入设置后**不重载 mcp_provider**，若自动连接在恢复完成
之后才返回，`_persist()` 会用启动时缓存的旧内存覆盖刚导入的
`mcp_servers_v1`——表现为 MCP 服务「永远写不进去」，而其他设置正常。

**规避**：导入前在 MCP 设置页禁用所有服务（含内置），导入（选「完全覆盖」）
并自动重启后重新启用。转换器在检测到自定义 MCP 时会输出此警告。

## 6. 时区约定

`chats.json` 时间戳为无时区本地时间；与 RikkaHub 的 epoch 毫秒互转按转换机
本地时区解释。两次转换在同一时区下往返稳定。

## 7. 测试

```bash
uv run pytest
```

覆盖：真实备份双向转换、群聊无损往返、附件标记往返、多版本分支、Room
身份哈希、工具事件往返、MCP 内置/headers/schema、模型体系映射、设置类型
安全、用户信息/记忆/技能/S3/WebDAV。

## 8. 参考实现

转换策略参考 [jwbb903/Rikkahub2Kelivo](https://github.com/jwbb903/Rikkahub2Kelivo)
（内置提供商映射、groupId 分组建节点、modelIDMap），并修复其两个问题：
生成的 SQLite 写入假 Room 身份哈希（真实应用首次打开即崩溃）、不处理群聊。
