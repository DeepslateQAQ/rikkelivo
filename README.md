# rikkelivo

Python 开发的 RikkaHub ↔ Kelivo (以及它的 Fork: Cuplivo) 备份互转工具。

## 功能

- 几乎完全适配的备份转换
- RikkaHub FTS 索引支持
- Kelivo 独有功能兼容

## 安装

```bash
$ uv sync --group dev   # 或: uv pip install .
```

## 用法

```bash
$ uv run rikkelivo to-rikkahub <kelivo-backup.zip> [output.zip]   # Kelivo → RikkaHub
$ uv run rikkelivo to-kelivo  <rikkahub-backup.zip> [output.zip]  # RikkaHub → Kelivo
```

不指定输出文件名时，默认在输入同目录生成 `<原名>.rikkahub.zip` /
`<原名>.kelivo.zip`；已存在的输出文件需 `--force` 覆盖。

```bash
# 示例
rikkelivo to-rikkahub kelivo_backup_20260806T140526.000246.zip
rikkelivo to-kelivo rikkahub_backup_20260806_134946.zip --force
```

## 导入注意事项

> **⚠ 导入 Kelivo 前请先禁用所有 MCP 服务**（MCP 设置页逐个关掉，含内置）。
> Kelivo 启动时对启用的服务自动连接，迟到的连接回调会用旧内存覆盖刚导入的
> MCP 配置（Kelivo 的 BUG，详见 spec 第 5 节）。禁用后导入（选「完全覆盖」），
> 重启后再重新启用即可。
>
> **⚠ 技能（Skills）不会随备份恢复**（Kelivo 恢复逻辑不处理 `skills/` 目录）。
> 有技能时会额外生成 `<输出名>.skills.zip`，请在 Kelivo 技能页手动导入该 zip。

## 测试

```bash
$ uv run pytest
```
