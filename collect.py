"""GitHub open-source project collector for SCA build-analyzer testing.
@author: Hyungsuk Choi, University of Maryland, 2026

Thin launcher. The implementation lives in the ``scraper`` package (see
``scraper/__init__.py`` for the module map). This file is kept so the documented
entry point still works:

    $env:GITHUB_TOKEN = "<your_personal_access_token>"   # PowerShell
    python collect.py --pm npm --count 5      # collect 5 npm repos
    python collect.py --pm all --count 5      # every PM collects 5 repos

Requires the ``dotnet`` CLI on PATH for the dotnet special handling.
"""

from __future__ import annotations

import sys

from scraper.cli import main
# Re-exported for backward compatibility with any code doing `import collect`.
from scraper.collector import collect_for_pm  # noqa: F401

if __name__ == "__main__":
    sys.exit(main())
