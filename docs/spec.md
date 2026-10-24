# rikkelivo 转换规格

本规格描述当前实现（三应用、原生格式优先）。所有格式常量来自固定上游提交，
schema 资源随包分发；转换过程不联网。

## 1. 支持的输入

| 来源 | 载荷 | 识别与校验 |
| --- | --- | --- |
| RikkaHub | `rikka_hub.db` + `settings.json` + 附件 | Room 24–31：实际 `user_version`、完整实体表/列/索引/外键、存在的 Room identity；WAL 按 SQLite 指针重放，SHM 不恢复 |
| Kelivo / Cuplivo | `manifest.json` + `settings.json` + `database/kelivo.db` | archive 2/3；SQLite 1–3 精确 schema；manifest 全文件 bytes/SHA256、conversation/message 计数、scope、payloadKind |
| Kelivo / Cuplivo | `chats.json` v1/v2 | 根版本、必填值、日期、重复 ID、owner 冲突、toolEvents/geminiThoughtSigs、groupChats/groupMembers |
| Cuplivo | `conversations.jsonl` / `messages.jsonl` / `chats_meta.json` | `format_version=2`、固定文件名、逐行 wrapper、声明计数 |
| 任意 | 仅 `settings.json`（可带附件） | 靠应用特征键区分家族；裸 `{}` 不猜 |

同包出现互斥载荷（如 `rikka_hub.db` 与 `chats.json`）直接失败；损坏的
manifest 不会回落到旧 JSON；未来版本只有声明了可读兼容性才接受，并按当前
已知结构投影并报告未知表/列。

## 2. 输出

| 目标 | latest | legacy |
| --- | --- | --- |
| RikkaHub | Room 31（3.1.0） | Room 24（2.4.11/2.5.0 基线） |
| Kelivo | archive 3 + SQLite 3，`appVersion="rikkelivo/<版本>"` | `chats.json` v1 |
| Cuplivo | archive 3 + SQLite 3 | `chats.json` v2 |

- Room 输出建完整目标 schema、精确 identity、重建 FTS、关闭的单文件数据库，
  并做逐表 integrity_check 与 foreign_key_check。
- archive 3 输出真实 manifest：`formatVersion/minimumReadableFormatVersion`、
  `payloadKind`、`scope`（15 类）、`businessEntityRowIds`、entries bytes/hash、
  database block 计数；不附加旧 `chats.json`/`deleted.json`。
- 仅设置源保持仅设置：不创建空数据库。空但存在的聊天域仍写出 0 行数据库。
- 旧 JSON 输出：`settings.json` + `chats.json`（+ Cuplivo 的 `deleted.json`）
  与目标支持的附件；官方 JSON1 写完整 messageIds 与 ordinal selection，
  Cuplivo JSON2 写代表 messageIds 与 actual version。

## 3. 选择与分支

- 原生 SQL：`version_selections_json` / `conversation_rows.truncate_index`
  使用实际 revision version 与逻辑 group 槽位。
- RikkaHub：节点 `select_index`；输出 `nodes="[]"`，消息只在 `message_node`。
- 历史 JSON1：官方 importer 把 selection 当 ordinal；历史 Cuplivo JSON2 与
  JSONL2 使用实际 version。根 `version` 不能单独证明方言：无群聊字段且消息
  候选无法判定时，转换拒绝猜测；两种解释得到不同 message 即报歧义。
- 历史 JSON 的 `truncateIndex` 先按源消息顺序折算为逻辑 group 边界；输出
  legacy 时反映射回对应消息序列的首索引。
- 群聊/并行 subgroup/speaker：目标不支持时按
  `(conversation, groupId, subgroupId, speaker)` 拆成稳定 lineage 并线性化；
  仅在包含原选中 identity 的 lineage 保留选择，其余取各自末修订，报告并行
  语义被线性化。RikkaHub/官方 JSON1 对助手正文加入发送者引用。
- 上下文边界：RikkaHub 最后选中消息的 `isContextCheckpoint` ↔ Kelivo
  `truncate_index`；无法表达的空上下文不造空消息，保留可见历史并报告。

## 4. 设置、模型与认证

- 提供商/模型按 `(provider preference key, logical model ID)` 配对；
  `apiModelId` 只是请求名，只有在本 provider 内唯一时才作为别名。未限定的
  同名模型只有在全部保留 provider 中唯一时才解析。
- 无法映射的 active 引用被清空并报告，不偷偷换同名 provider/model。
- ChatGPT/Grok 是原生 OAuth 账户的明确例外：按目标原生类型双向映射
  access/refresh/expiry/account/email/plan；Kelivo 侧 `sessionId` 是新的本地
  缓存身份，`requiresLogin=false`。缺失 refresh、未知 expiry、或源
  `requiresLogin=true` 时目标保持 signed-out，不伪造凭据。Kimi/Claude OAuth
  在 RikkaHub 没有对应账户类型，按 unsupported 丢弃。
- 活动 service-account、活动多 key、未知认证 kind 在没有等价结构时丢弃
  provider 与受影响模型；未启用的备用 key 只警告，不丢 provider。诊断绝不
  打印 token/密钥。
- Room 输出重放上游 Settings V1–V5：MCP 短类型、preset parts 短类型且递归
  tool.output、assistant quickMessages → 全局列表 + ID、移除旧注入开关并清
  关联、balanceOption → quotaQuery（legacy 目标反向降级）。
- 现代 Kelivo 写真实使用的键：`title_*_v1`、`suggestion_*_v1`、
  `translate_*_v1`、`ocr_*_v1`、`compress_model_v1`、
  `tts_selected_service_id_v1`、`asr_selected_service_id_v1`、
  `per_chat_model_enabled_v1`；`assistants_v1` 外层仍是 JSON 字符串，内层
  presetMessages 为数组。

## 5. 会话原生子集

- 会话模型固定：RikkaHub `config.chatModelId` ↔ Kelivo
  `chat_model_provider`/`chat_model_id`，并确保
  `per_chat_model_enabled_v1` 使绑定生效。
- workspace / skills / 自定义 prompt 写 native extras：
  `workspace.id`、`workspace.cwd`、`skills.ids`、`prompt.system`、
  `prompt.instructionIds`、`prompt.worldBookIds`。保持源权限门：助手未允许
  会话系统提示词时不把内容搬到目标 extras。
- 源 `[]` 技能列表表示显式禁用，不转成 all；RikkaHub 显式技能名列表在目标
  写成这些 ID。MCP 会话关联可保存，但生成时仍可能按助手取工具，明确报告
  不等价。

## 6. 记忆

- 同家族原生对象保持目标可表达的 type/scope/status。
- RikkaHub 24–27 → 28+ 只做上游 `legacyMemoriesToProfile` 变换（按旧整数 id
  排序、trim 行拼单行、frontmatter、`- [stated]`）；这条原生迁移的
  `updated_at` 使用迁移时间并报告。RH 28–31 → 31 原样保留所有 Markdown 文件
  （含非 pinned 路径），不聚合。
- RikkaHub → Kelivo：每个非空白 MD 文件一条 typed entry；`/profile.md` →
  identity，其余 → instruction；content 原样。共享 `__global__` 只复制到原本
  `useGlobalMemory=true` 的助手，保持私有范围；使用 global 的助手的私有文件
  转为 archived；空白文件丢弃并报告。
- Kelivo → RikkaHub：active global + 该助手 active private 写入每个助手自己
  的库，`useGlobalMemory=false`；archived 丢弃报告；没有助手的 global 丢弃。
  typed entries 每组最多两篇 `/profile.md`、`/preferences.md`，按
  workflow/voice/instruction 排序，正文原样，updated_at 取最大值；>48KiB 保留
  正文并警告。已有 RH markdown 不经过该聚合器。
- 降到 legacy RikkaHub 简单记忆时，每个可见文档/active entry 一条 content。

## 7. 独立数据类别

| 类别 | 同家族 | 跨家族 / 无目标能力 |
| --- | --- | --- |
| 媒体创作库 | 保留 session/node/record 与 `media_creation/` 文件 | 按数量报告丢弃，不伪造聊天/文件 |
| 独立翻译历史 | 保留原始行，不截断 | 报告丢弃；消息 translation 照常 |
| 工作区 | 保留配置、绑定与真实文件 | 只迁移元数据，报告目录/rootfs/授权未恢复 |
| 独立用量 ledger | 原样保留，空表也权威 | 报告丢弃；消息自身 token 保留 |
| favorite / folder / GenMedia / consolidation | 保留 | 报告丢弃或 source 缺资产 |
| tombstone / 生成任务 | 原生类型保留 | 报告丢弃 |

RikkaHub 目标没有 ledger 时，只用可证实 CHAT 的消息 whole-turn 用量重建
（role=assistant 且 prompt/completion 至少一个 >0），报告
`usage_history_reconstructed`；已有 ledger 绝不补建。

## 8. 附件与工具

- 一次预分配映射：同家族保留安全相对路径；到 RikkaHub 时 upload/images/
  avatars 等映射到扁平 `upload/`，名字为源完整路径 SHA256 前 16 位 + 安全
  basename；到 Kelivo 时使用 `kelivo-file:///` 分段 percent-encoding。
- 缺失本地附件保留可读引用并报告；Kelivo 侧标 `unavailable`。远程 URL 原样。
- 工具部分保持出现顺序；Kelivo `tool_call` ↔ RikkaHub tool（input JSON
  字符串、output 递归 parts）。服务端工具在 Kelivo 侧只保留可读输出并报告
  调用语义丢失。
- provider artifact 按 `(revision, kind, payload)` 保留；同家族保留所有
  kind，历史/跨家族只保留 Gemini 思考签名，其余按 kind 报告丢弃。

## 9. CLI 与发布

见 README：源文件优先、纯参数模式零提问、TTY 向导、退出码 0/1/2/130、
stderr 损失报告、原子发布、源别名保护、弃用命令隐藏但保留语法。
