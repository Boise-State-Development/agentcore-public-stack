"""Pytest configuration for test suite."""

import sys
from pathlib import Path

# Add backend/src to Python path for imports
# This file is in backend/tests/, so we need to go up one level to backend/
BACKEND_DIR = Path(__file__).parent.parent
SRC_DIR = BACKEND_DIR / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

