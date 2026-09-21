import pytest
import sys
import os


def pytest_addoption(parser):
    parser.addoption(
        "--run-browser",
        action="store_true",
        default=False,
        help="Run tests marked with @pytest.mark.browser",
    )
    parser.addoption(
        "--run-private-live",
        action="store_true",
        default=False,
        help="Run tests marked with @pytest.mark.private_live",
    )
    parser.addoption(
        "--run-invasive-live",
        action="store_true",
        default=False,
        help="Run tests marked with @pytest.mark.invasive_live",
    )


def pytest_configure(config):
    pass


def pytest_runtest_setup(item):
    marker_names = {m.name for m in item.iter_markers()}

    if "browser" in marker_names:
        if not item.config.getoption("--run-browser"):
            pytest.skip("Use --run-browser to run browser tests")

    if "private_live" in marker_names:
        if not item.config.getoption("--run-private-live"):
            pytest.skip("Use --run-private-live to run private live tests")

    if "invasive_live" in marker_names:
        if not item.config.getoption("--run-invasive-live"):
            pytest.skip("Use --run-invasive-live to run invasive live tests")
