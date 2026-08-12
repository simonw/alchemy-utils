from __future__ import annotations

import importlib.metadata
import subprocess
import sys

from click.testing import CliRunner

from alchemy_utils import Database
from alchemy_utils.cli import cli


def test_distribution_and_import_package_are_named_alchemy_utils():
    assert importlib.metadata.version("alchemy-utils") == "0.1.0"
    assert Database.__module__ == "alchemy_utils.db"


def test_console_program_name_and_version():
    result = CliRunner().invoke(cli, ["--version"], prog_name="alchemy-utils")

    assert result.exit_code == 0
    assert result.output == "alchemy-utils, version 0.1.0\n"


def test_python_module_entry_point_uses_new_package_name():
    result = subprocess.run(
        [sys.executable, "-m", "alchemy_utils", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "create-table" in result.stdout
