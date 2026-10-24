"""Conversion lifecycle: profile, prepare, atomic publish.

``prepare_conversion`` owns one private staging directory and the validated
source archive.  Everything that can fail semantically -- reading, schema
migration, mapping, loss accounting -- happens inside it, so the interactive
front end never asks a question and then discovers new data loss.  Publishing
is a separate, atomic step: the archive is written to a unique temporary file
in the output directory, fully re-validated, and only then linked or replaced
into place.
"""

from __future__ import annotations

import errno
import os
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from rikkelivo import archive, kelivo, mapping, model, rikkahub, schema
from rikkelivo.model import (
    App,
    ConversionPlan,
    ConversionResult,
    Diagnostic,
    TargetProfile,
)

RH_LATEST = schema.RH_LATEST
RH_LEGACY = schema.RH_LEGACY
KELIVO_ARCHIVE_VERSION = 3
KELIVO_LEGACY_JSON = 1
CUPLIVO_LEGACY_JSON = 2


def target_profile(app: App, *, legacy: bool = False) -> TargetProfile:
    """The exact native format a target app is expected to import."""
    if app == "rikkahub":
        version = RH_LEGACY if legacy else RH_LATEST
        return TargetProfile("rikkahub", "rikkahub", model.KIND_RH_SQLITE,
                             None, version, legacy)
    if app == "kelivo":
        if legacy:
            return TargetProfile("kelivo", "kelivo", model.KIND_KELIVO_JSON,
                                 KELIVO_LEGACY_JSON, None, True)
        return TargetProfile("kelivo", "kelivo", model.KIND_KELIVO_ARCHIVE,
                             KELIVO_ARCHIVE_VERSION, schema.DRIFT_LATEST, False)
    if app == "cuplivo":
        if legacy:
            return TargetProfile("cuplivo", "kelivo", model.KIND_KELIVO_JSON,
                                 CUPLIVO_LEGACY_JSON, None, True)
        return TargetProfile("cuplivo", "kelivo", model.KIND_KELIVO_ARCHIVE,
                             KELIVO_ARCHIVE_VERSION, schema.DRIFT_LATEST, False)
    raise ValueError(f"unknown target app: {app}")


def _adapter(family: str):
    return rikkahub if family == "rikkahub" else kelivo


@contextmanager
def prepare_conversion(source: Path, target: TargetProfile
                       ) -> Iterator[ConversionPlan]:
    """Read, validate, migrate and project a source; clean up on exit."""
    source = Path(source)
    if not source.exists():
        raise archive.ArchiveError(f"源文件不存在: {source}")
    if not source.is_file():
        raise archive.ArchiveError(f"源路径不是文件: {source}")
    with tempfile.TemporaryDirectory(prefix="rikkelivo-stage-") as temporary:
        staging = Path(temporary)
        with archive.open_backup(source) as zf:
            detected = archive.detect_archive(zf)
            info = archive.validate_format(zf, detected, staging / "source")
            adapter = _adapter(info.family)
            backup = adapter.read_backup(zf, info, staging / "source")
        projected, diagnostics = mapping.project_backup(backup, target)
        extra: list[Diagnostic] = list(diagnostics)
        output_tables = getattr(projected.payload, "tables", None)
        if target.family == "rikkahub" and output_tables \
                and not schema.probe_fts().simple:
            extra.append(Diagnostic(
                "search_tokenizer_changed", "search", 1,
                "输出消息搜索索引使用 unicode61 分词器"))
        diagnostics_final = model.merge_diagnostics(extra)
        counts = mapping.counts_for_backup(projected)
        writer_adapter = _adapter(target.family)

        def _write(destination: Path) -> None:
            writer_adapter.write_backup(projected, target, destination)

        plan = ConversionPlan(
            target=target,
            backup=projected,
            diagnostics=diagnostics_final,
            counts=counts,
            write=_write,
            source=source,
        )
        yield plan


def _same_file(left: Path, right: Path) -> bool:
    try:
        if left.exists() and right.exists():
            return os.path.samefile(left, right)
    except OSError:
        pass
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _validate_destination(source: Path, output: Path, *, force: bool) -> None:
    parent = output.parent
    if not parent.exists() or not parent.is_dir():
        raise archive.ArchiveError(f"输出目录不存在: {parent}")
    if output.exists() and output.is_dir():
        raise archive.ArchiveError(f"输出路径是一个目录: {output}")
    if _same_file(source, output):
        raise archive.ArchiveError("输出不能覆盖源备份或其别名")
    if output.exists() and not force:
        raise archive.ArchiveError(f"{output} 已存在（使用 --force 覆盖）")
    if not os.access(parent, os.W_OK):
        raise archive.ArchiveError(f"输出目录不可写: {parent}")


def _verify_archive(path: Path, target: TargetProfile,
                    payload: object) -> None:
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise archive.ArchiveError(f"输出 ZIP 条目损坏: {bad}")
            detected = archive.detect_archive(zf)
            expected = target.kind
            if detected.kind == model.KIND_SETTINGS_ONLY and (
                    target.kind == model.KIND_RH_SQLITE
                    or target.kind == model.KIND_KELIVO_ARCHIVE):
                tables = getattr(payload, "tables", None)
                if not tables:
                    expected = model.KIND_SETTINGS_ONLY
            if detected.kind != expected:
                raise archive.ArchiveError("输出备份类型与目标不一致")
            with tempfile.TemporaryDirectory(prefix="rikkelivo-verify-") as tmp:
                archive.validate_format(zf, detected, Path(tmp))
    except (zipfile.BadZipFile, OSError) as exc:
        raise archive.ArchiveError(f"输出备份校验失败: {exc}") from exc


def write_conversion(plan: ConversionPlan, output: Path, *,
                     force: bool = False) -> ConversionResult:
    """Write the prepared archive atomically and return the published result."""
    output = Path(output)
    source = Path(plan.source) if plan.source is not None else Path()
    _validate_destination(source, output, force=force)
    parent = output.parent
    handle = tempfile.NamedTemporaryFile(
        prefix=".rikkelivo-", suffix=".tmp", dir=parent, delete=False)
    temporary = Path(handle.name)
    handle.close()
    temporary.unlink()
    published = False
    try:
        plan.write(temporary)
        if not temporary.exists():
            raise archive.ArchiveError("写入器没有生成输出文件")
        _verify_archive(temporary, plan.target, plan.backup.payload)
        if force:
            os.replace(temporary, output)
            published = True
        else:
            try:
                os.link(temporary, output)
                published = True
            except FileExistsError as exc:
                raise archive.ArchiveError(
                    f"{output} 在发布前出现，未覆盖已有文件") from exc
            except OSError as exc:
                if exc.errno in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP,
                                 errno.ENOSYS, errno.EMLINK, errno.EACCES):
                    raise archive.ArchiveError(
                        f"目标目录不支持安全发布（hardlink）: {exc}") from exc
                raise archive.ArchiveError(
                    f"无法安全发布输出文件: {exc}") from exc
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
        if published and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return ConversionResult(output=output.absolute(),
                            counts=dict(plan.counts),
                            diagnostics=plan.diagnostics)


def default_output(source: Path, target: TargetProfile) -> Path:
    stem = Path(source).stem
    if target.legacy:
        return Path(source).with_name(f"{stem}.{target.app}.legacy.zip")
    return Path(source).with_name(f"{stem}.{target.app}.zip")
