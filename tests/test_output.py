"""Atomic publication: alias protection, force, races and cleanup."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from backup_fixtures import make_backup, read_observables

from rikkelivo import archive, cli, convert


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _source(tmp_path: Path) -> Path:
    return make_backup(tmp_path / "src.zip", app="kelivo", kind="json",
                       version=1)


def _plan(source: Path, app: str = "rikkahub", legacy: bool = False):
    return convert.prepare_conversion(
        source, convert.target_profile(app, legacy=legacy))  # type: ignore[arg-type]


def _invoke(runner: CliRunner, args):
    return runner.invoke(cli.main, args, catch_exceptions=False)


# ---------------------------------------------------------------------------
# Source aliases
# ---------------------------------------------------------------------------

def test_output_cannot_replace_source_with_force(runner, tmp_path):
    source = _source(tmp_path)
    original = source.read_bytes()
    result = _invoke(runner, [str(source), "--to", "kelivo",
                              "-o", str(source), "--force"])
    assert result.exit_code == 1
    assert source.read_bytes() == original


def test_output_symlink_to_source_is_rejected(runner, tmp_path):
    source = _source(tmp_path)
    link = tmp_path / "link.zip"
    link.symlink_to(source)
    result = _invoke(runner, [str(source), "--to", "kelivo",
                              "-o", str(link), "--force"])
    assert result.exit_code == 1
    assert link.is_symlink()


def test_output_hardlink_to_source_is_rejected(runner, tmp_path):
    source = _source(tmp_path)
    link = tmp_path / "hard.zip"
    os.link(source, link)
    assert link.stat().st_ino == source.stat().st_ino
    result = _invoke(runner, [str(source), "--to", "kelivo",
                              "-o", str(link), "--force"])
    assert result.exit_code == 1
    assert link.stat().st_ino == source.stat().st_ino


# ---------------------------------------------------------------------------
# Existing outputs and force
# ---------------------------------------------------------------------------

def test_existing_output_without_force_is_usage_error(runner, tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "out.zip"
    output.write_bytes(b"existing")
    result = _invoke(runner, [str(source), "--to", "kelivo",
                              "-o", str(output)])
    assert result.exit_code == 2
    assert output.read_bytes() == b"existing"


def test_force_replaces_existing_output(runner, tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "out.zip"
    output.write_bytes(b"existing")
    result = _invoke(runner, [str(source), "--to", "kelivo",
                              "-o", str(output), "--force"])
    assert result.exit_code == 0
    assert output.read_bytes() != b"existing"
    assert read_observables(output)["conversations"] == 2


def test_direct_writer_rejects_existing_output(tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "out.zip"
    output.write_bytes(b"existing")
    with _plan(source) as plan:
        with pytest.raises(archive.ArchiveError, match="已存在"):
            convert.write_conversion(plan, output)
    assert output.read_bytes() == b"existing"


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

def test_writer_failure_preserves_existing_target(tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "out.zip"
    output.write_bytes(b"keep-me")
    with _plan(source) as plan:
        original_write = plan.write

        def failing(destination: Path) -> None:
            destination.write_bytes(b"partial")
            raise archive.ArchiveError("synthetic writer failure")

        plan.write = failing
        with pytest.raises(archive.ArchiveError, match="synthetic"):
            convert.write_conversion(plan, output, force=True)
    assert output.read_bytes() == b"keep-me"
    assert not list(tmp_path.glob(".rikkelivo-*.tmp"))


def test_race_before_publish_does_not_overwrite(tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "out.zip"
    with _plan(source) as plan:
        original_write = plan.write

        def racing(destination: Path) -> None:
            original_write(destination)
            output.write_bytes(b"racer")

        plan.write = racing
        with pytest.raises(archive.ArchiveError, match="发布前出现"):
            convert.write_conversion(plan, output)
    assert output.read_bytes() == b"racer"
    assert not list(tmp_path.glob(".rikkelivo-*.tmp"))


def test_unsupported_hardlink_fails_explicitly(tmp_path, monkeypatch):
    source = _source(tmp_path)
    output = tmp_path / "out.zip"

    def no_link(*_args, **_kwargs):
        raise OSError(errno.EOPNOTSUPP, "hardlinks unsupported")

    monkeypatch.setattr(convert.os, "link", no_link)
    with _plan(source) as plan:
        with pytest.raises(archive.ArchiveError, match="hardlink"):
            convert.write_conversion(plan, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".rikkelivo-*.tmp"))


def test_missing_parent_directory_is_rejected(runner, tmp_path):
    source = _source(tmp_path)
    result = _invoke(runner, [str(source), "--to", "kelivo",
                              "-o", str(tmp_path / "missing" / "out.zip")])
    assert result.exit_code == 1
    assert "目录" in result.stderr or "directory" in result.stderr.lower()


def test_directory_output_is_rejected(runner, tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "folder"
    output.mkdir()
    result = _invoke(runner, [str(source), "--to", "kelivo",
                              "-o", str(output)])
    assert result.exit_code == 2
    assert output.is_dir()


def test_successful_publish_leaves_no_temporary_files(tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "out.zip"
    with _plan(source) as plan:
        convert.write_conversion(plan, output)
    assert output.exists()
    assert not list(tmp_path.glob(".rikkelivo-*.tmp"))
