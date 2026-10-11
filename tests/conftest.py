from pathlib import Path

import pytest

import fixtures


@pytest.fixture(scope="session")
def fixture_root(tmp_path_factory) -> Path:
    """All fixture sets, generated once per test session."""
    root = tmp_path_factory.mktemp("fixtures")
    fixtures.generate_all(root)
    return root
