"""Shared fixtures and markers for mojo-mcp tests."""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-mojo",
        action="store_true",
        default=False,
        help="Run tests that require a working Mojo installation",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-mojo"):
        return
    skip_mojo = pytest.mark.skip(reason="needs --run-mojo option to run")
    for item in items:
        if "mojo" in item.keywords:
            item.add_marker(skip_mojo)
