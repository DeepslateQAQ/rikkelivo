"""rikkelivo CLI: GitHub CLI-style bidirectional backup converter.

Convert chat backups between RikkaHub and Kelivo, two independent Android
chat apps.  Chat history (conversations, messages, reasoning, uploads) is
converted faithfully; app settings are bridged best-effort with warnings.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from rikkelivo import __version__
from rikkelivo.convert import to_kelivo, to_rikkahub
from rikkelivo.style import comment, console, err, ok


def _default_output(input_path: Path, target: str) -> Path:
    return input_path.with_name(f"{input_path.stem}.{target}.zip")


def _resolve_output(input_path: Path, output: str | None, target: str,
                    force: bool) -> Path:
    out = Path(output) if output else _default_output(input_path, target)
    if out == input_path:
        raise click.ClickException("输出路径不能与输入备份相同")
    if out.exists() and not force:
        raise click.ClickException(
            f"{out} 已存在（使用 --force 覆盖）"
        )
    return out


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="rikkelivo")
def main() -> None:
    """在 RikkaHub 与 Kelivo 之间互转备份。

    \b
    rikkelivo to-rikkahub kelivo_backup.zip [output.zip]
    rikkelivo to-kelivo  rikkahub_backup.zip [output.zip]
    """
    from rikkelivo.style import banner
    banner()


@main.command("to-rikkahub")
@click.argument("kelivo_backup", type=click.Path(exists=True, dir_okay=False,
                                                path_type=Path))
@click.argument("output", required=False, type=click.Path(path_type=Path))
@click.option("--force", "-f", is_flag=True, help="覆盖已存在的输出文件")
def to_rikkahub_cmd(kelivo_backup: Path, output: Path | None, force: bool) -> None:
    """将 Kelivo 备份转换为 RikkaHub 备份。"""
    started = time.monotonic()
    src = Path(kelivo_backup)
    out = _resolve_output(src, str(output) if output else None, "rikkahub", force)
    comment(f"正在读取 Kelivo 备份: [cyan]{src}[/cyan]")
    summary, notes = to_rikkahub(src, out)
    _finish("RikkaHub 备份", out, summary, notes, started)


@main.command("to-kelivo")
@click.argument("rikkahub_backup", type=click.Path(exists=True, dir_okay=False,
                                                   path_type=Path))
@click.argument("output", required=False, type=click.Path(path_type=Path))
@click.option("--force", "-f", is_flag=True, help="覆盖已存在的输出文件")
def to_kelivo_cmd(rikkahub_backup: Path, output: Path | None, force: bool) -> None:
    """将 RikkaHub 备份转换为 Kelivo 备份。"""
    started = time.monotonic()
    src = Path(rikkahub_backup)
    out = _resolve_output(src, str(output) if output else None, "kelivo", force)
    comment(f"正在读取 RikkaHub 备份: [cyan]{src}[/cyan]")
    summary, notes = to_kelivo(src, out)
    _finish("Kelivo 备份", out, summary, notes, started)


def _finish(kind: str, out: Path, summary: str, notes: list[str],
            started: float) -> None:
    ok(summary)
    drops = [n for n in notes if not n.startswith("⚠")]
    warns = [n for n in notes if n.startswith("⚠")]
    if drops:
        console.print(f"[bold yellow]! 丢弃：{'、'.join(drops)}[/bold yellow]")
    if warns:
        console.print("---", style="dim")
        for note in warns:
            console.print(f"[bold yellow]{note}[/bold yellow]")
        console.print("---", style="dim")
    ok(f"输出：{out}（耗时 {time.monotonic() - started:.2f}s）")


def entrypoint() -> None:
    try:
        main(standalone_mode=False)
    except click.ClickException as exc:
        err(f"{exc.message}")
        sys.exit(exc.exit_code)
    except click.exceptions.Abort:
        err("aborted")
        sys.exit(130)
    except Exception as exc:  # pragma: no cover - last-resort guard
        err(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
