"""rikkelivo command line: source-first conversion with an optional wizard.

    rikkelivo SOURCE --to {rikkahub,kelivo,cuplivo} [-o OUT] [--legacy] [-f]
    rikkelivo SOURCE            # interactive, only on a TTY

The group inspects exactly one token to decide whether the invocation is the
source-first form; everything after that is parsed by Click normally, so
``--help`` never touches the source file and a source literally named
``to-kelivo`` is reachable through ``--`` or ``./to-kelivo``.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Iterable, Sequence

import click

from rikkelivo import __version__, archive, convert
from rikkelivo.model import App, ConversionPlan, Diagnostic, TargetProfile
from rich.markup import escape

from rikkelivo.style import console, err, summary_box, warn

TARGET_CHOICES = ("rikkahub", "kelivo", "cuplivo")
_TARGET_LABEL = {
    "rikkahub": "RikkaHub",
    "kelivo": "Kelivo（官方）",
    "cuplivo": "Cuplivo",
}
_KNOWN_COMMANDS = {"to-rikkahub", "to-kelivo", "_convert"}


class Cancelled(click.ClickException):
    exit_code = 130

    def __init__(self) -> None:
        super().__init__("已取消，未发布任何输出")


class _SourceContext(click.Context):
    @property
    def command_path(self) -> str:
        if self.parent is not None and not self.info_name:
            return self.parent.command_path
        return super().command_path


class _SourceCommand(click.Command):
    """A hidden internal command whose usage reads like the top-level form."""

    context_class = _SourceContext

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        prog = ctx.command_path.replace(" _convert", "").rstrip()
        formatter.write_usage(prog + " SOURCE", "", prefix="Usage: ")

    def format_options(self, ctx: click.Context,
                       formatter: click.HelpFormatter) -> None:
        super().format_options(ctx, formatter)


class SourceFirstGroup(click.Group):
    """GitHub-CLI-style group that accepts ``rikkelivo SOURCE`` directly."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        rewritten = self._rewrite(list(args))
        return super().parse_args(ctx, rewritten)

    @staticmethod
    def _rewrite(args: Sequence[str]) -> list[str]:
        if not args:
            return list(args)
        first = args[0]
        if first in ("-h", "--help", "--version", "-V"):
            return list(args)
        if first == "--":
            return ["_convert", *args[1:]]
        if first in _KNOWN_COMMANDS:
            return list(args)
        return ["_convert", *args]

    def resolve_command(self, ctx: click.Context, args: list[str]):
        cmd_name, cmd, rest = super().resolve_command(ctx, args)
        if cmd_name == "_convert":
            # Keep the internal name out of usage and error hints.
            return "", cmd, rest
        return cmd_name, cmd, rest

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write_usage(f"{ctx.command_path} SOURCE", "[OPTIONS]")

    def format_options(self, ctx: click.Context,
                       formatter: click.HelpFormatter) -> None:
        command = self.commands.get("_convert")
        if command is None:  # pragma: no cover - defensive
            return
        sub = click.Context(command, info_name=ctx.info_name, parent=ctx)
        sub.help_option_names = ctx.help_option_names
        command.format_options(sub, formatter)

    def format_commands(self, ctx: click.Context,
                        formatter: click.HelpFormatter) -> None:
        # The deprecated commands are intentionally undiscoverable.
        return


@click.group(
    name="rikkelivo",
    cls=SourceFirstGroup,
    invoke_without_command=False,
    add_help_option=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, prog_name="rikkelivo")
def main() -> None:
    """在 RikkaHub、Kelivo 与 Cuplivo 之间转换聊天备份。

    \b
    纯参数：
      rikkelivo backup.zip --to kelivo -o out.zip
      rikkelivo backup.zip --to rikkahub --legacy

    \b
    交互向导（终端）：
      rikkelivo backup.zip
    """


@main.command("_convert", hidden=True, cls=_SourceCommand,
              context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("source", type=click.Path(exists=True, dir_okay=False,
                                          path_type=Path))
@click.option("--to", "target",
              type=click.Choice(TARGET_CHOICES, case_sensitive=False),
              help="目标应用")
@click.option("-o", "--output", type=click.Path(dir_okay=False, path_type=Path),
              help="输出路径（默认在源文件旁）")
@click.option("--legacy", is_flag=True,
              help="写出目标应用的 legacy 格式（目标相关）")
@click.option("-f", "--force", is_flag=True, help="覆盖已存在的输出文件")
@click.version_option(__version__, prog_name="rikkelivo")
def convert_command(source: Path, target: str | None, output: Path | None,
                    legacy: bool, force: bool) -> None:
    """将 SOURCE 备份转换为目标应用的备份。"""
    if target is not None:
        _run_conversion(source, target=target, output=output, legacy=legacy,
                        force=force)
        return
    if not _interactive_available():
        raise click.UsageError(
            "缺少 --to；非交互环境请显式指定 --to {rikkahub,kelivo,cuplivo}")
    _run_wizard(source, output=output, legacy=legacy, force=force)


def _interactive_available() -> bool:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    term = os.environ.get("TERM", "")
    return bool(term) and term.lower() != "dumb"


@main.command("to-rikkahub", hidden=True, cls=_SourceCommand,
              context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("kelivo_backup", type=click.Path(exists=True, dir_okay=False,
                                                 path_type=Path))
@click.argument("output", required=False, type=click.Path(path_type=Path))
@click.option("--legacy", is_flag=True)
@click.option("-f", "--force", is_flag=True)
@click.version_option(__version__, prog_name="rikkelivo")
def to_rikkahub_command(kelivo_backup: Path, output: Path | None,
                        legacy: bool, force: bool) -> None:
    """[已弃用] 将 Kelivo 备份转换为 RikkaHub 备份。"""
    _deprecated_notice("to-rikkahub", "rikkahub")
    _run_conversion(kelivo_backup, target="rikkahub", output=output,
                    legacy=legacy, force=force)


@main.command("to-kelivo", hidden=True, cls=_SourceCommand,
              context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("rikkahub_backup", type=click.Path(exists=True, dir_okay=False,
                                                   path_type=Path))
@click.argument("output", required=False, type=click.Path(path_type=Path))
@click.option("--legacy", is_flag=True)
@click.option("-f", "--force", is_flag=True)
@click.version_option(__version__, prog_name="rikkelivo")
def to_kelivo_command(rikkahub_backup: Path, output: Path | None,
                      legacy: bool, force: bool) -> None:
    """[已弃用] 将 RikkaHub 备份转换为 Kelivo 备份。"""
    _deprecated_notice("to-kelivo", "kelivo")
    _run_conversion(rikkahub_backup, target="kelivo", output=output,
                    legacy=legacy, force=force)


def _deprecated_notice(command: str, target: App) -> None:
    warn(f"{command} 已弃用；默认输出格式已改变，"
         f"请改用 `rikkelivo SOURCE --to {target}`")


# ---------------------------------------------------------------------------
# Diagnostics and summaries
# ---------------------------------------------------------------------------

def _report_diagnostics(diagnostics: Iterable[Diagnostic]) -> None:
    for diagnostic in diagnostics:
        if diagnostic.count <= 0:
            continue
        warn(f"{diagnostic.detail}（{diagnostic.category}：{diagnostic.count}）")
        if diagnostic.code == "search_tokenizer_changed":
            warn("仅验证了原生实体与重建后的搜索索引，源库的 FTS 未被视为可读")


def _print_result(result, diagnostics: Iterable[Diagnostic],
                  started: float) -> None:
    counts = result.counts
    keys = (
        ("conversations", "会话"), ("messages", "消息"),
        ("memory", "记忆"), ("workspaces", "工作区"),
        ("assistants", "助手"), ("providers", "提供商"),
        ("files", "文件"), ("usage", "用量记录"),
    )
    rows = [(label, str(counts[key])) for key, label in keys
            if counts.get(key)]
    if not rows:
        rows = [("数据", "仅设置")]
    rows.append(("输出", str(result.output)))
    rows.append(("耗时", f"{time.monotonic() - started:.2f}s"))
    summary_box("转换完成", rows)
    _report_diagnostics(diagnostics)


# ---------------------------------------------------------------------------
# Non-interactive conversion
# ---------------------------------------------------------------------------

def _run_conversion(source: Path, *, target: App, output: Path | None,
                    legacy: bool, force: bool) -> None:
    started = time.monotonic()
    profile = convert.target_profile(target, legacy=legacy)
    destination = _resolve_output(source, output, profile, force=force)
    try:
        with convert.prepare_conversion(source, profile) as plan:
            result = convert.write_conversion(plan, destination, force=force)
            diagnostics = plan.diagnostics
    except archive.ArchiveError as exc:
        raise click.ClickException(str(exc)) from exc
    _print_result(result, diagnostics, started)


def _resolve_output(source: Path, output: Path | None, profile: TargetProfile,
                    *, force: bool) -> Path:
    if output is None:
        return convert.default_output(Path(source), profile)
    destination = Path(output)
    if destination.exists() and destination.is_dir():
        raise archive.ArchiveError(f"输出路径是一个目录: {destination}")
    if destination.exists() and not force:
        raise click.UsageError(f"{destination} 已存在（使用 --force 覆盖）")
    return destination


# ---------------------------------------------------------------------------
# Interactive wizard
# ---------------------------------------------------------------------------

def _wizard_library():
    try:
        import questionary
    except ImportError as exc:  # pragma: no cover - packaging bug
        raise click.ClickException(
            "当前安装缺少 questionary，无法启动交互向导") from exc
    return questionary


def _run_wizard(source: Path, *, output: Path | None, legacy: bool,
                force: bool) -> None:
    started = time.monotonic()
    questionary = _wizard_library()
    try:
        info = archive.inspect_backup(source)
    except archive.ArchiveError as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(f"[bold]源备份[/bold] {escape(str(source))}")
    rows = [("来源", info.describe()),
            ("家族", "RikkaHub" if info.family == "rikkahub" else "Kelivo 家族"),
            ("数据库", f"SQLite {info.schema_version}" if info.schema_version
             else ("有" if info.database_present else "无")),
            ("设置", "有" if info.settings_present else "无"),
            ("选择编码", info.selection_encoding)]
    summary_box("检测结果", rows)
    default_target = info.producer or "kelivo"
    choice = _ask_select(
        questionary, "目标应用：",
        [{"name": _TARGET_LABEL[value], "value": value}
         for value in TARGET_CHOICES],
        default=default_target)
    target: App = choice  # type: ignore[assignment]
    use_legacy = legacy
    if not legacy:
        mode = _ask_select(
            questionary, "输出格式：",
            _format_choices(target),
            default="latest")
        use_legacy = mode == "legacy"
    profile = convert.target_profile(target, legacy=use_legacy)
    default_path = (Path(output) if output is not None
                    else convert.default_output(source, profile))
    if not default_path.is_absolute():
        default_path = Path.cwd() / default_path
    answer = _ask_text(questionary, "输出路径：", str(default_path))
    if not answer:
        answer = str(default_path)
    destination = Path(answer).expanduser()
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    needs_force = destination.exists()
    if needs_force and not force:
        allowed = _ask_confirm(
            questionary, f"{destination} 已存在，覆盖？", default=False)
        if not allowed:
            raise Cancelled()
        needs_force = True
    stack = _ExitStackProxy()
    try:
        with _spinner("正在读取并校验备份"):
            plan = stack.enter(convert.prepare_conversion(source, profile))
        _show_preview(plan)
        proceed = _ask_confirm(questionary, "开始转换？", default=False)
        if not proceed:
            raise Cancelled()
        result = convert.write_conversion(
            plan, destination, force=needs_force or force)
        diagnostics = plan.diagnostics
    except archive.ArchiveError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        stack.close()
    _print_result(result, diagnostics, started)


class _ExitStackProxy:
    """A tiny context stack so the wizard can keep prepare open across prompts."""

    def __init__(self) -> None:
        self._stack = []

    def enter(self, context):
        value = context.__enter__()
        self._stack.append(context)
        return value

    def close(self) -> None:
        while self._stack:
            context = self._stack.pop()
            try:
                context.__exit__(None, None, None)
            except Exception:  # pragma: no cover - cleanup must not mask
                pass


def _spinner(message: str):
    from rikkelivo.style import spinner
    return spinner(message)


def _format_choices(target: App) -> list[dict[str, str]]:
    if target == "rikkahub":
        latest = "最新：RikkaHub 3.1.0（Room 31）"
        legacy = "legacy：Room 24（2.4.11 / 2.5.0 基线）"
    elif target == "kelivo":
        latest = "最新：Kelivo 1.3.1+（archive 3 + SQLite 3）"
        legacy = "legacy：官方 chats.json v1"
    else:
        latest = "最新：Cuplivo 4.3.0+（archive 3 + SQLite 3）"
        legacy = "legacy：Cuplivo chats.json v2"
    return [{"name": latest, "value": "latest"},
            {"name": legacy, "value": "legacy"}]


def _ask_select(questionary, message: str, choices: list[dict[str, str]],
                *, default: str | None) -> str:
    try:
        answer = questionary.select(
            message,
            choices=[questionary.Choice(title=item["name"], value=item["value"])
                     for item in choices],
            default=default,
        ).unsafe_ask()
    except KeyboardInterrupt as exc:
        raise Cancelled() from exc
    if answer is None:
        raise Cancelled()
    return answer


def _ask_text(questionary, message: str, default: str) -> str | None:
    try:
        return questionary.text(message, default=default).unsafe_ask()
    except KeyboardInterrupt as exc:
        raise Cancelled() from exc
    except EOFError as exc:
        raise Cancelled() from exc


def _ask_confirm(questionary, message: str, *, default: bool) -> bool:
    try:
        answer = questionary.confirm(message, default=default).unsafe_ask()
    except KeyboardInterrupt as exc:
        raise Cancelled() from exc
    except EOFError as exc:
        raise Cancelled() from exc
    return bool(answer)


def _show_preview(plan: ConversionPlan) -> None:
    counts = plan.counts
    rows = [(label, str(counts[key])) for key, label in (
        ("conversations", "会话"), ("messages", "消息"),
        ("memory", "记忆"), ("workspaces", "工作区"),
        ("assistants", "助手"), ("providers", "提供商"),
        ("files", "文件"), ("usage", "用量记录"),
    ) if counts.get(key)]
    if not rows:
        rows = [("数据", "仅设置")]
    summary_box("转换预览", rows)
    if plan.diagnostics:
        warn("以下内容会被降级、丢弃或无法等价值恢复：")
        _report_diagnostics(plan.diagnostics)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def entrypoint() -> None:
    # Windows consoles and pipes commonly use cp1252; force UTF-8 so Chinese
    # output cannot raise UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        main(standalone_mode=False)
    except click.UsageError as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.ClickException as exc:
        err(exc.message)
        sys.exit(exc.exit_code)
    except click.exceptions.Abort:
        err("aborted")
        sys.exit(130)
    except (KeyboardInterrupt, EOFError):
        err("已取消，未发布任何输出")
        sys.exit(130)
    except Exception as exc:  # pragma: no cover - last-resort guard
        err(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
