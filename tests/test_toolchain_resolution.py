"""Project-venv-first Mojo toolchain resolution.

Regression guard for the version-divergence bug: when a project pins a Mojo
version *and* ships its own uv-managed venv (``.venv/bin/mojo[x]``), the MCP must
run *that* binary — the exact toolchain ``run_tests.sh`` / ``uv run mojox`` use —
not a parallel ``uvx mojo-compiler==<pin>`` install that can silently diverge.

It also guards the reporting fix: ``mojo_version`` must surface the *effective*
compiler for a path (what ``execute`` will actually run), not just the global
binary, and warn when the venv's version disagrees with the ``.mojo-version`` pin.
"""

import json
import stat
from pathlib import Path
from unittest.mock import patch

from mojo_mcp import sandbox as s


def _make_venv(root: Path, *, mojox: bool = False, mojo: bool = False,
               version_line: str = "mojo 1.0.0b2 (fake)") -> None:
    """Create a fake project venv whose mojo[x] binaries echo `version_line`."""
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = f"#!/bin/sh\necho '{version_line}'\n"
    for name, want in (("mojox", mojox), ("mojo", mojo)):
        if want:
            p = bin_dir / name
            p.write_text(script)
            p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestSelectMojoPriority:
    def test_venv_mojox_wins_over_pin(self, tmp_path):
        # The dixi-serde scenario: pin present AND venv present -> use the venv,
        # never a duplicate uvx install.
        _make_venv(tmp_path, mojox=True, mojo=True)
        cmd, source = s._select_mojo("1.0.0b2", str(tmp_path))
        assert cmd == [str(tmp_path / ".venv" / "bin" / "mojox")]
        assert source == "project-venv-mojox"
        assert "uvx" not in cmd

    def test_venv_mojo_used_when_no_mojox(self, tmp_path):
        _make_venv(tmp_path, mojo=True)
        cmd, source = s._select_mojo("1.0.0b2", str(tmp_path))
        assert cmd == [str(tmp_path / ".venv" / "bin" / "mojo")]
        assert source == "project-venv-mojo"

    def test_pin_uses_uvx_when_no_venv(self, tmp_path):
        cmd, source = s._select_mojo("1.0.0b2", str(tmp_path))
        assert cmd == ["uvx", "--from", "mojo-compiler==1.0.0b2",
                       "--prerelease=allow", "mojo"]
        assert source == "uvx-pin"

    def test_no_pin_no_venv_is_system(self, tmp_path):
        cmd, source = s._select_mojo(None, str(tmp_path))
        assert cmd == ["mojo"]
        assert source == "system"

    def test_no_pin_prefers_venv_mojox(self, tmp_path):
        _make_venv(tmp_path, mojox=True)
        cmd, source = s._select_mojo(None, str(tmp_path))
        assert cmd == [str(tmp_path / ".venv" / "bin" / "mojox")]
        assert source == "project-venv-mojox"


class TestMojoCmdBackCompat:
    """`_mojo_cmd(version)` with no cwd must keep its old contract."""

    def test_no_cwd_prerelease_pin_is_uvx(self):
        assert s._mojo_cmd("1.0.0b1") == [
            "uvx", "--from", "mojo-compiler==1.0.0b1", "--prerelease=allow", "mojo"]

    def test_no_cwd_no_pin_is_system(self):
        assert s._mojo_cmd(None) == ["mojo"]


class TestEffectiveVersionReporting:
    def test_reports_effective_venv_and_warns_on_mismatch(self, tmp_path):
        # Venv reports b3 but the pin file says b2 -> effective reflects the venv
        # (what actually runs) and a warning surfaces the divergence.
        (tmp_path / ".mojo-version").write_text("1.0.0b2\n")
        _make_venv(tmp_path, mojo=True, version_line="mojo 1.0.0b3 (fake)")
        with patch("mojo_mcp.sandbox.shutil.which", return_value=None):
            r = json.loads(s.run_mojo_version(str(tmp_path)))
        assert r["pinned_version"] == "1.0.0b2"
        assert r["effective_source"] == "project-venv-mojo"
        assert "1.0.0b3" in r["effective_version"]
        assert "warning" in r
        assert ".venv" in r["effective_command"]

    def test_no_warning_when_versions_agree(self, tmp_path):
        (tmp_path / ".mojo-version").write_text("1.0.0b2\n")
        _make_venv(tmp_path, mojo=True, version_line="mojo 1.0.0b2 (fake)")
        with patch("mojo_mcp.sandbox.shutil.which", return_value=None):
            r = json.loads(s.run_mojo_version(str(tmp_path)))
        assert "warning" not in r
        assert r["effective_source"] == "project-venv-mojo"
        assert r["effective_version"] == "1.0.0b2"

    def test_uvx_pin_effective_is_pin_without_shellout(self, tmp_path):
        # Pin, no venv -> effective source is uvx-pin; the effective version is
        # the pin itself (uvx installs exactly that) with no download.
        (tmp_path / ".mojo-version").write_text("1.0.0b2\n")
        with patch("mojo_mcp.sandbox.shutil.which", return_value=None):
            r = json.loads(s.run_mojo_version(str(tmp_path)))
        assert r["effective_source"] == "uvx-pin"
        assert r["effective_version"] == "1.0.0b2"
        assert "warning" not in r


class TestExecuteReportsSource:
    def test_execute_reports_project_venv_source(self, tmp_path, monkeypatch):
        _make_venv(tmp_path, mojo=True)
        monkeypatch.setattr(s, "_supports_json_diagnostics", lambda prefix: False)

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(s.subprocess, "run", lambda *a, **k: _Result())
        r = json.loads(s.run_execute("def main(): pass", cwd=str(tmp_path)))
        assert r["mojo_source"] == "project-venv-mojo"
