"""CLI contract: dispatch, exit codes, deprecated syntax and the wizard shell."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from backup_fixtures import make_backup, read_observables

from rikkelivo import archive, cli, convert, model


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _invoke(runner: CliRunner, args, **kwargs):
    return runner.invoke(cli.main, args, catch_exceptions=False, **kwargs)


# ---------------------------------------------------------------------------
# Help and version never touch the source
# ---------------------------------------------------------------------------

def test_root_help_shows_source_first_usage(runner):
    result = _invoke(runner, ["--help"])
    assert result.exit_code == 0
    assert "Usage: rikkelivo SOURCE [OPTIONS]" in result.output
    assert "--to" in result.output
    assert "to-rikkahub" not in result.output
    assert "to-kelivo" not in result.output


def test_version_does_not_require_source(runner):
    result = _invoke(runner, ["--version"])
    assert result.exit_code == 0
    assert "rikkelivo" in result.output
    assert "version" in result.output


def test_help_does_not_validate_missing_source(runner):
    result = _invoke(runner, ["missing.zip", "--help"])
    assert result.exit_code == 0
    assert "Usage: rikkelivo SOURCE" in result.output
    assert "does not exist" not in result.output


def test_empty_invocation_is_usage_error(runner):
    result = _invoke(runner, [])
    assert result.exit_code == 2


def test_missing_source_argument_is_usage_error(runner):
    result = _invoke(runner, ["--to", "kelivo"])
    assert result.exit_code == 2
    assert "SOURCE" in result.output


def test_missing_target_non_tty_is_usage_error(runner, tmp_path):
    source = make_backup(tmp_path / "src.zip", app="kelivo", kind="json",
                         version=1)
    result = _invoke(runner, [str(source)])
    assert result.exit_code == 2
    assert "--to" in result.output


# ---------------------------------------------------------------------------
# Pure parameter mode
# ---------------------------------------------------------------------------

def test_parameter_mode_converts_without_prompts(runner, tmp_path):
    source = make_backup(tmp_path / "rh.zip", app="rikkahub", kind="rh",
                         version=31)
    output = tmp_path / "out.zip"
    result = _invoke(runner, [str(source), "--to", "kelivo",
                              "-o", str(output)])
    assert result.exit_code == 0
    assert output.exists()
    assert "转换完成" in result.output
    view = read_observables(output)
    assert (view["conversations"], view["messages"]) == (2, 6)
    assert view["selected_text"] == "answer-v2"


def test_lossy_success_is_exit_zero_and_stderr(runner, tmp_path):
    source = make_backup(tmp_path / "rh.zip", app="rikkahub", kind="rh",
                         version=31)
    output = tmp_path / "out.zip"
    result = _invoke(runner, [str(source), "--to", "kelivo",
                              "-o", str(output)])
    assert result.exit_code == 0
    assert "转换完成" in result.output
    assert "未迁移" in result.stderr or "丢弃" in result.stderr or \
        "工作区" in result.stderr


def test_default_output_latest_and_legacy_names(runner, tmp_path):
    source = make_backup(tmp_path / "src.zip", app="cuplivo", kind="json",
                         version=2)
    result = _invoke(runner, [str(source), "--to", "kelivo"])
    assert result.exit_code == 0
    latest = tmp_path / "src.kelivo.zip"
    assert latest.exists()
    result = _invoke(runner, [str(source), "--to", "kelivo", "--legacy"])
    assert result.exit_code == 0
    assert (tmp_path / "src.kelivo.legacy.zip").exists()


def test_output_is_relative_to_caller_cwd(runner, tmp_path, monkeypatch):
    source = make_backup(tmp_path / "src.zip", app="kelivo", kind="json",
                         version=1)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    result = _invoke(runner, [str(source), "--to", "rikkahub",
                              "-o", "relative.zip"])
    assert result.exit_code == 0
    assert (work / "relative.zip").exists()


def test_failure_prints_no_success_summary(runner, tmp_path):
    source = tmp_path / "broken.zip"
    source.write_bytes(b"not a zip")
    result = _invoke(runner, [str(source), "--to", "kelivo"])
    assert result.exit_code == 1
    assert "转换完成" not in result.output
    assert "ZIP" in result.stderr or "无法" in result.stderr


def test_readable_unicode_and_markup_paths(runner, tmp_path):
    source = make_backup(tmp_path / "[备份] 中文.zip", app="kelivo",
                         kind="json", version=1)
    output = tmp_path / "[输出] 中文.zip"
    result = _invoke(runner, [str(source), "--to", "rikkahub",
                              "-o", str(output)])
    assert result.exit_code == 0
    assert output.exists()


# ---------------------------------------------------------------------------
# Deprecated syntax
# ---------------------------------------------------------------------------

def test_deprecated_commands_are_hidden(runner):
    result = _invoke(runner, ["--help"])
    assert "to-rikkahub" not in result.output
    assert "to-kelivo" not in result.output


def test_deprecated_to_rikkahub_defaults_to_latest(runner, tmp_path):
    source = make_backup(tmp_path / "src.zip", app="kelivo", kind="json",
                         version=1)
    output = tmp_path / "old.zip"
    result = _invoke(runner, ["to-rikkahub", str(source), str(output)])
    assert result.exit_code == 0
    assert output.exists()
    assert "已弃用" in result.stderr
    assert "rikkelivo SOURCE" in result.stderr
    assert "rikkahub" in result.stderr
    view = read_observables(output)
    assert view["schema_version"] == 31


def test_deprecated_to_kelivo_legacy_is_json1(runner, tmp_path):
    source = make_backup(tmp_path / "src.zip", app="rikkahub", kind="rh",
                         version=31)
    output = tmp_path / "old.zip"
    result = _invoke(runner, ["to-kelivo", str(source), str(output),
                              "--legacy"])
    assert result.exit_code == 0
    view = read_observables(output)
    assert view["kind"] == "kelivo-json"
    assert view["format_version"] == 1


def test_source_named_like_command_via_double_dash(runner, tmp_path,
                                                   monkeypatch):
    source = make_backup(tmp_path / "to-kelivo", app="kelivo", kind="json",
                         version=1)
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "out.zip"
    result = _invoke(runner, ["--", "to-kelivo", "--to", "rikkahub",
                              "-o", str(output)])
    assert result.exit_code == 0
    assert output.exists()


def test_output_named_like_command_is_not_dispatched(runner, tmp_path,
                                                     monkeypatch):
    source = make_backup(tmp_path / "src.zip", app="kelivo", kind="json",
                         version=1)
    monkeypatch.chdir(tmp_path)
    result = _invoke(runner, [str(source), "--output", "to-kelivo",
                              "--to", "rikkahub"])
    assert result.exit_code == 0
    assert (tmp_path / "to-kelivo").exists()


# ---------------------------------------------------------------------------
# Wizard shell (questionary interaction is exercised separately in a PTY)
# ---------------------------------------------------------------------------

class _Answer:
    def __init__(self, value):
        self.value = value

    def unsafe_ask(self):
        if self.value is _CANCEL:
            raise KeyboardInterrupt
        return self.value


_CANCEL = object()


class _FakeQuestionary:
    Choice = staticmethod(lambda **_kwargs: None)
    select_answers: list = []
    text_answers: list = []
    confirm_answers: list = []
    calls: list = []

    def select(self, message, choices, default=None):
        self.calls.append(("select", message, default))
        return _Answer(self.select_answers.pop(0))

    def text(self, message, default=None):
        self.calls.append(("text", message, default))
        return _Answer(self.text_answers.pop(0))

    def confirm(self, message, default=None):
        self.calls.append(("confirm", message, default))
        return _Answer(self.confirm_answers.pop(0))


def _fake_questionary(select=None, text=None, confirm=None):
    answer = _FakeQuestionary()
    answer.select_answers = list(select or [])
    answer.text_answers = list(text or [])
    answer.confirm_answers = list(confirm or [])
    answer.calls = []
    return answer, answer.calls


def test_wizard_accepts_default_path_and_writes(runner, tmp_path, monkeypatch):
    source = make_backup(tmp_path / "src.zip", app="kelivo", kind="json",
                         version=1)
    fake, calls = _fake_questionary(
        select=["kelivo", "latest"], text=[""], confirm=[True])
    monkeypatch.setattr(cli, "_interactive_available", lambda: True)
    monkeypatch.setattr(cli, "_wizard_library", lambda: fake)
    result = _invoke(runner, [str(source)])
    assert result.exit_code == 0
    assert (tmp_path / "src.kelivo.zip").exists()
    assert any(call[0] == "select" for call in calls)
    assert any(call[0] == "confirm" for call in calls)


def test_wizard_decline_final_confirmation_publishes_nothing(runner, tmp_path,
                                                             monkeypatch):
    source = make_backup(tmp_path / "src.zip", app="kelivo", kind="json",
                         version=1)
    fake, _calls = _fake_questionary(
        select=["rikkahub", "latest"], text=[""], confirm=[False])
    monkeypatch.setattr(cli, "_interactive_available", lambda: True)
    monkeypatch.setattr(cli, "_wizard_library", lambda: fake)
    result = _invoke(runner, [str(source)])
    assert result.exit_code == 130
    assert not (tmp_path / "src.rikkahub.zip").exists()


def test_wizard_overwrite_decline_keeps_existing(runner, tmp_path, monkeypatch):
    source = make_backup(tmp_path / "src.zip", app="kelivo", kind="json",
                         version=1)
    output = tmp_path / "src.rikkahub.zip"
    output.write_bytes(b"keep me")
    fake, _calls = _fake_questionary(
        select=["rikkahub", "latest"], text=[""], confirm=[False])
    monkeypatch.setattr(cli, "_interactive_available", lambda: True)
    monkeypatch.setattr(cli, "_wizard_library", lambda: fake)
    result = _invoke(runner, [str(source)])
    assert result.exit_code == 130
    assert output.read_bytes() == b"keep me"


def test_wizard_legacy_skips_format_question(runner, tmp_path, monkeypatch):
    source = make_backup(tmp_path / "src.zip", app="kelivo", kind="json",
                         version=1)
    fake, calls = _fake_questionary(
        select=["cuplivo"], text=[""], confirm=[True])
    monkeypatch.setattr(cli, "_interactive_available", lambda: True)
    monkeypatch.setattr(cli, "_wizard_library", lambda: fake)
    result = _invoke(runner, [str(source), "--legacy"])
    assert result.exit_code == 0
    assert (tmp_path / "src.cuplivo.legacy.zip").exists()
    assert [call[0] for call in calls].count("select") == 1


def test_wizard_ctrl_c_cancels(runner, tmp_path, monkeypatch):
    source = make_backup(tmp_path / "src.zip", app="kelivo", kind="json",
                         version=1)
    fake, _calls = _fake_questionary(
        select=[_CANCEL], text=[], confirm=[])
    monkeypatch.setattr(cli, "_interactive_available", lambda: True)
    monkeypatch.setattr(cli, "_wizard_library", lambda: fake)
    result = _invoke(runner, [str(source)])
    assert result.exit_code == 130
