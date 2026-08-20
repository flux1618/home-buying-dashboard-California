"""Where the rulebook is found, and why the order matters.

This file exists because the original implementation located `buyer_profile.toml` by walking
up from its own source file, which works perfectly in a checkout and fails the moment the
package is installed — the path then resolves inside `site-packages`. That single assumption
broke two things in sequence: the container started cleanly and failed on its first request,
and then CI failed across ~200 tests with a `FileNotFoundError` pointing at a directory
nobody had ever put config in.

Both failures were the same bug wearing different clothes, so the resolution order is now
explicit and tested rather than implied by a path expression.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from analyzer.core.profile import default_profile_path, load_profile

REPO_PROFILE = Path(__file__).resolve().parents[1] / "buyer_profile.toml"


class TestResolutionOrder:
    def test_env_var_wins(self, monkeypatch, tmp_path):
        elsewhere = tmp_path / "custom.toml"
        elsewhere.write_text("")
        monkeypatch.setenv("HBA_PROFILE", str(elsewhere))
        assert default_profile_path() == elsewhere

    def test_env_var_wins_even_when_it_does_not_exist(self, monkeypatch, tmp_path):
        """An explicit answer is honoured, and then fails loudly.

        Silently falling through to a different profile because the requested one was
        missing would be much worse than an error: the run would succeed against the wrong
        rulebook, and every score would be quietly computed from someone else's preferences.
        """
        missing = tmp_path / "nope.toml"
        monkeypatch.setenv("HBA_PROFILE", str(missing))
        assert default_profile_path() == missing
        with pytest.raises(FileNotFoundError, match="HBA_PROFILE"):
            load_profile()

    def test_falls_back_to_the_checkout_when_no_env_var(self, monkeypatch):
        monkeypatch.delenv("HBA_PROFILE", raising=False)
        assert default_profile_path() == REPO_PROFILE

    def test_falls_back_to_the_working_directory_when_installed(self, monkeypatch, tmp_path):
        """The installed-package case, simulated by hiding the beside-package copy.

        This is the path CI takes. `pip install .` puts `analyzer/` in site-packages with no
        profile beside it, so without this fallback the entire suite dies on import of the
        first fixture that needs a profile.
        """
        import analyzer.core.profile as module

        monkeypatch.delenv("HBA_PROFILE", raising=False)
        # Point the module at a fake location with no profile beside it, so the
        # beside-package branch misses exactly as it does in site-packages.
        fake_pkg = tmp_path / "site-packages" / "analyzer" / "core"
        fake_pkg.mkdir(parents=True)
        monkeypatch.setattr(module, "__file__", str(fake_pkg / "profile.py"))

        cwd_profile = tmp_path / "workdir" / "buyer_profile.toml"
        cwd_profile.parent.mkdir()
        cwd_profile.write_text("")
        monkeypatch.chdir(cwd_profile.parent)

        assert default_profile_path() == cwd_profile

    def test_missing_profile_error_says_what_to_do(self, monkeypatch, tmp_path):
        """A bare FileNotFoundError pointing into site-packages sends people hunting for a
        packaging bug. The real problem is almost always that the config lives elsewhere."""
        import analyzer.core.profile as module

        monkeypatch.delenv("HBA_PROFILE", raising=False)
        fake_pkg = tmp_path / "sp" / "analyzer" / "core"
        fake_pkg.mkdir(parents=True)
        monkeypatch.setattr(module, "__file__", str(fake_pkg / "profile.py"))
        monkeypatch.chdir(tmp_path)

        with pytest.raises(FileNotFoundError, match="HBA_PROFILE"):
            load_profile()


class TestResolutionIsNotFrozenAtImport:
    def test_changing_the_env_var_changes_the_answer(self, monkeypatch, tmp_path):
        """Resolution happens at call time, deliberately.

        As a module-level constant this was computed once at import, so setting the variable
        in a fixture or an entrypoint had no effect — the value had been frozen several
        imports earlier. That is a hard failure to reason about, because the code doing the
        setting looks correct.
        """
        first = tmp_path / "a.toml"
        second = tmp_path / "b.toml"
        first.write_text("")
        second.write_text("")

        monkeypatch.setenv("HBA_PROFILE", str(first))
        assert default_profile_path() == first

        monkeypatch.setenv("HBA_PROFILE", str(second))
        assert default_profile_path() == second


def test_the_shipped_profile_actually_loads():
    """The repo's own rulebook must parse, on every push.

    Cheap, and it catches the mistake that is otherwise found at the worst time: a hand-edit
    to a scoring weight that leaves the TOML valid but the schema wrong.
    """
    profile = load_profile(REPO_PROFILE)
    assert profile.verdict_take_min > profile.verdict_watch_min
    assert profile.anchors, "at least one commute anchor is required"
