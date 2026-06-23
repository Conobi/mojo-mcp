"""classify_origin unit tests (pure; roots are pre-normalized by the shell)."""

from mojo_mcp.diagnostics import classify_origin

ROOTS = frozenset({"/proj", "/proj/main.mojo", "/tmp/mojo-mcp-x/main.mojo"})


def test_summary_file_none_is_non_suppressible():
    assert classify_origin(None, ROOTS) == "non_suppressible"


def test_wrapper_is_project():
    assert classify_origin("/tmp/mojo-mcp-x/main.mojo", ROOTS) == "project"


def test_file_inside_cwd_root_is_project():
    assert classify_origin("/proj/sub/util.mojo", ROOTS) == "project"


def test_exact_supplied_source_is_project():
    assert classify_origin("/proj/main.mojo", ROOTS) == "project"


def test_outside_all_roots_is_dependency():
    assert classify_origin("/usr/local/mojo-packages/mojix/errno.mojo", ROOTS) == "dependency"


def test_cwd_containment_beats_lookalike_prefix():
    # /projector is NOT inside /proj despite the string prefix
    assert classify_origin("/projector/x.mojo", ROOTS) == "dependency"
