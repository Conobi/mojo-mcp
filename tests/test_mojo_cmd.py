"""Tests for `_mojo_cmd` version normalization.

`mojo-compiler` on PyPI used a `0.`-prefixed scheme for the calver-style
24.x/25.x/26.x releases (e.g. PyPI `0.25.6.0` ↔ Mojo `25.6.0`). Mojo 1.0+
uses true semver (`1.0.0b1`, `1.0.0`, …) and is published verbatim. The
prefix step must only fire for the legacy calver range.
"""

from mojo_mcp.sandbox import _mojo_cmd


class TestMojoCmdVersion:
    def test_no_version_uses_system_mojo(self):
        # No pin, no cwd → fall back to system `mojo`
        assert _mojo_cmd(None) == ["mojo"]

    def test_legacy_calver_gets_zero_prefix(self):
        # 25.6.0 → uvx --from mojo-compiler==0.25.6.0
        cmd = _mojo_cmd("25.6.0")
        assert cmd == ["uvx", "--from", "mojo-compiler==0.25.6.0", "mojo"]

    def test_legacy_calver_26(self):
        cmd = _mojo_cmd("26.3.0")
        assert cmd == ["uvx", "--from", "mojo-compiler==0.26.3.0", "mojo"]

    def test_already_prefixed_legacy_unchanged(self):
        # 0.25.6.0 → uvx --from mojo-compiler==0.25.6.0 (no double prefix)
        cmd = _mojo_cmd("0.25.6.0")
        assert cmd == ["uvx", "--from", "mojo-compiler==0.25.6.0", "mojo"]

    def test_modern_semver_beta_not_prefixed(self):
        # Regression: 1.0.0b1 must NOT become 0.1.0.0b1 (PyPI has no such version)
        cmd = _mojo_cmd("1.0.0b1")
        assert cmd == ["uvx", "--from", "mojo-compiler==1.0.0b1", "mojo"]

    def test_modern_semver_release_not_prefixed(self):
        cmd = _mojo_cmd("1.0.0")
        assert cmd == ["uvx", "--from", "mojo-compiler==1.0.0", "mojo"]

    def test_modern_semver_alpha_not_prefixed(self):
        cmd = _mojo_cmd("1.0.0a2")
        assert cmd == ["uvx", "--from", "mojo-compiler==1.0.0a2", "mojo"]
