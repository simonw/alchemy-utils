from __future__ import annotations

import importlib.metadata
import subprocess
import sys

from alchemy_utils import Database


def test_distribution_and_import_package_are_named_alchemy_utils():
    distribution = importlib.metadata.distribution("alchemy-utils")

    assert distribution.metadata["Name"] == "alchemy-utils"
    assert Database.__module__ == "alchemy_utils.db"


def test_python_module_entry_point_uses_new_package_name():
    result = subprocess.run(
        [sys.executable, "-m", "alchemy_utils", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "create-table" in result.stdout
