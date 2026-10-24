# rikkelivo

RikkaHub、Kelivo（官方）与 Cuplivo 之间的聊天备份转换工具。

## 支持的格式

**输入（自动识别并完整校验）**

- RikkaHub：Room 24–31 的 `rikka_hub.db` + `settings.json` + 附件
- Kelivo / Cuplivo 原生归档：archive 2/3 + SQLite schema 1–3 的
  `manifest.json` + `settings.json` + `database/kelivo.db`
- 历史 Kelivo `chats.json` v1（官方）与 Cuplivo `chats.json` v2
- 历史 Cuplivo JSONL v2（`conversations.jsonl` / `messages.jsonl` /
  `chats_meta.json`）
- 仅设置备份（无数据库，保留可表达的文件）

文件必须由参数显式给出，不会联网，也不会修改源文件。

**输出（最新稳定格式，或按目标解释的 `--legacy`）**

| `--to` | 默认输出 | `--legacy` 输出 |
| --- | --- | --- |
| `rikkahub` | RikkaHub 3.1.0（Room 31，完整 schema） | Room 24（2.4.11 / 2.5.0 基线） |
| `kelivo` | 官方 Kelivo 1.3.1+（archive 3 + SQLite 3） | 官方 `chats.json` v1 |
| `cuplivo` | Cuplivo 4.3.0+（archive 3 + SQLite 3） | Cuplivo `chats.json` v2 |

最新 Kelivo 与 Cuplivo 都拒绝 JSON v2；`--to cuplivo --legacy` 的恢复目标是
历史 Cuplivo 版本，而不是最新版本。

## 用法

```bash
# 纯参数：不询问任何问题
uv run rikkelivo backup.zip --to kelivo
uv run rikkelivo backup.zip --to rikkahub --legacy -o out.zip
uv run rikkelivo backup.zip --to cuplivo --force

# 交互向导（仅当 stdin/stdout 都是 TTY 且 TERM 不是 dumb）
uv run rikkelivo backup.zip
```

- 默认输出位于源文件旁：`<源名>.<目标>.zip`；`--legacy` 为
  `<源名>.<目标>.legacy.zip`。显式相对 `-o/--output` 相对调用者 cwd。
- 已存在的普通输出在参数模式需要 `--force`；向导会请求一次明确的覆盖确认。
- 文件名与命令同名时可用 `--` 或 `./` 消歧，例如
  `rikkelivo -- to-kelivo --to kelivo`。
- `to-rikkahub` / `to-kelivo` 仍可用，但已隐藏并弃用：默认输出格式已改为
  最新目标，stderr 会给出新写法。它们不再生成 skills 旁路 zip。

交互向导使用方向键与 Enter：显示实际检测结果 → 选目标（含同应用）→
选最新/legacy → 输入输出路径 → 显示计数与所有降级/丢弃内容 → 覆盖确认 →
最终确认。两个确认默认 No；Ctrl-C、EOF 或取消退出 `130` 且不发布输出。

## 行为与损失报告

- 成功摘要写 stdout；所有数据损失、重建限制和弃用提示写 stderr。有损成功
  仍然退出 `0`；无法识别/损坏/写入失败退出 `1`；参数错误退出 `2`。
- 输出先写入输出目录内的唯一临时文件，完整重校验后才原子发布。非 `--force`
  使用 hardlink，发布前出现同名文件不会覆盖；目标文件系统不支持安全发布时
  明确失败，不做有竞态的覆盖回退。
- 源文件及其 symlink / hardlink 别名绝不会被覆盖。
- 允许有损，但绝不隐藏：不使用 `x_rikkelivo` 私有字段、不生成额外保留包、不
  伪造目标应用的原生功能。独立媒体创作库、独立翻译历史、独立用量流水、
  favorite / folder、Cuplivo 群聊控制等目标无法表达的内容会按类别计数报告。
- 工作区只迁移元数据：目标中的目录内容、rootfs、shell 与工具授权不会凭空恢复。
- 记忆按目标权限模型迁移：RikkaHub 共享记忆只写入原本启用共享的助手；Kelivo
  的全局/私有/archived 条目按目标能力投影，空白与不可表达状态会报告。
- 消息搜索索引始终重建。当前 Python 的 SQLite 缺少 RikkaHub 私有的 `simple`
  分词器时会使用 `unicode61`，并报告 `search_tokenizer_changed`；普通实体表与
  WAL 数据仍完整读取。
- native 归档的 `manifest.json` 会如实写入 `appVersion = rikkelivo/<版本>`，
  不谎称由目标应用导出。

## 安装

```bash
uv sync --group dev            # 开发
uv sync --group dev --group build  # 需要构建单文件版时
```

## 测试

```bash
uv run pytest
```

测试数据是仓库内生成的确定性 fixture，不依赖真实用户备份。

## 自动构建

推送任意分支或手动触发（Actions → Run workflow）时，GitHub Actions 用
Nuitka 构建 5 个平台的单文件二进制（`linux-amd64`、`linux-arm64`、
`windows-amd64`、`darwin-amd64`、`darwin-arm64`），构建后执行 `--help` /
`--version` 冒烟检查并上传产物，见 `.github/workflows/nuitka-build.yml`。

本机构建：

```bash
uv sync --all-groups
scripts/build.sh       # Linux / macOS → build/linux|macos/rikkelivo
scripts/build.ps1      # Windows → build\win\rikkelivo.exe
```

Linux 构建需 `patchelf`，macOS/Windows 不需要；产物为 Nuitka onefile，
已包含 Questionary / prompt_toolkit / wcwidth 与 `rikkelivo/schemas` 资源，
不依赖目标机器安装 Python。

NixOS 等非常规发行版或需要 glibc 兼容面时，用 Docker 容器构建：

```bash
scripts/build-in-container.sh              # ubuntu:22.04 (glibc 2.35) → dist/rikkelivo
scripts/build-in-container.sh --cleanup    # 构建后删除复用的构建容器
```

## 格式来源

`src/rikkelivo/schemas/` 内是固定上游提交导出的 Room / Drift schema（运行必需
资源）。来源、提交与许可说明见 `src/rikkelivo/schemas/README.md`。
