"""Unit tests for ``apis.shared.security.python_ast_policy``.

The policy gate decides which user-supplied Python is permitted to execute
in the diagram / spreadsheet-analysis sandbox. These tests assert the
positive invariants:

* Imports are restricted to a plotting / data-analysis allowlist.
* Names that lead to host-process escape (``__import__``, ``eval``,
  ``subprocess``, ...) are rejected wherever they appear.
* Dunder attribute access (``__class__``, ``__bases__``, ...) is rejected
  so the standard "walk the type hierarchy" obfuscation cannot reach
  ``__builtins__``.
* Realistic chart and dataframe code is accepted unchanged.
"""

from __future__ import annotations

import textwrap

import pytest

from apis.shared.security.python_ast_policy import (
    PolicyError,
    validate_diagram_code,
)

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "import subprocess",
        "import os",
        "import sys",
        "import socket",
        "import shutil",
        "import pickle",
        "import ctypes",
        "import importlib",
        "import urllib.request",
        "import http.client",
        "import requests",
        "from os import system",
        "from subprocess import run",
        "from importlib import import_module",
        "from . import sibling",
    ],
)
def test_forbidden_imports_rejected(src: str) -> None:
    with pytest.raises(PolicyError):
        validate_diagram_code(src)


@pytest.mark.parametrize(
    "src",
    [
        "import matplotlib",
        "import matplotlib.pyplot as plt",
        "from matplotlib import pyplot",
        "from matplotlib.pyplot import figure",
        "import numpy as np",
        "from numpy import linspace, array",
        "import pandas as pd",
        "from pandas.api.types import is_numeric_dtype",
        "import seaborn as sns",
        "import math",
        "from datetime import datetime, timedelta",
        "import json",
        "import statistics",
        "from collections import Counter",
        "import itertools",
    ],
)
def test_allowed_imports_pass(src: str) -> None:
    validate_diagram_code(src)


# ---------------------------------------------------------------------------
# Forbidden bare names — caught even with no ``import`` statement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "__import__('os').system('id')",
        "__import__('subprocess').run(['id'])",
        "eval('1+1')",
        "exec('print(1)')",
        "compile('1+1', '<s>', 'eval')",
        "open('/etc/hostname').read()",
        "input()",
        "globals()",
        "locals()",
        "vars()",
        "getattr(object, '__class__')",
        "setattr(object, 'x', 1)",
    ],
)
def test_forbidden_names_rejected(src: str) -> None:
    with pytest.raises(PolicyError):
        validate_diagram_code(src)


def test_dunder_attribute_access_rejected() -> None:
    # The classic "walk the MRO to reach builtins" pattern.
    src = "().__class__.__bases__[0].__subclasses__()"
    with pytest.raises(PolicyError):
        validate_diagram_code(src)


def test_dunder_access_on_user_object_rejected() -> None:
    src = textwrap.dedent("""
        class A:
            pass
        a = A()
        a.__dict__
        """)
    with pytest.raises(PolicyError):
        validate_diagram_code(src)


# ---------------------------------------------------------------------------
# Obfuscation attempts
# ---------------------------------------------------------------------------


def test_string_concatenation_does_not_smuggle_import() -> None:
    """Building ``__import__`` from substrings is rejected because the bare
    name still appears."""
    src = "f = __import__\nf('os')"
    with pytest.raises(PolicyError):
        validate_diagram_code(src)


def test_assigning_forbidden_to_alias_still_rejected() -> None:
    src = "evil = eval\nevil('1+1')"
    with pytest.raises(PolicyError):
        validate_diagram_code(src)


def test_attribute_chain_to_dunder_rejected() -> None:
    src = "x = (1).__class__.__mro__"
    with pytest.raises(PolicyError):
        validate_diagram_code(src)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_source_rejected() -> None:
    with pytest.raises(PolicyError):
        validate_diagram_code("")


def test_whitespace_only_source_rejected() -> None:
    with pytest.raises(PolicyError):
        validate_diagram_code("   \n\t\n")


def test_syntax_error_rejected() -> None:
    with pytest.raises(PolicyError):
        validate_diagram_code("def foo(:")


def test_oversize_source_rejected() -> None:
    src = "x = 1\n" * 10000  # > 32 KiB
    with pytest.raises(PolicyError):
        validate_diagram_code(src)


def test_non_string_input_rejected() -> None:
    with pytest.raises(PolicyError):
        validate_diagram_code(None)  # type: ignore[arg-type]
    with pytest.raises(PolicyError):
        validate_diagram_code(b"import os")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Realistic chart code passes
# ---------------------------------------------------------------------------


def test_simple_matplotlib_line_chart_passes() -> None:
    src = textwrap.dedent("""
        import matplotlib.pyplot as plt
        import numpy as np

        x = np.linspace(0, 10, 100)
        y = np.sin(x)

        plt.figure(figsize=(10, 6))
        plt.plot(x, y, 'b-', linewidth=2)
        plt.title('Sine Wave')
        plt.xlabel('x')
        plt.ylabel('sin(x)')
        plt.grid(True, alpha=0.3)
        plt.savefig('sine.png', dpi=300, bbox_inches='tight')
        """)
    validate_diagram_code(src)


def test_pandas_dataframe_analysis_passes() -> None:
    src = textwrap.dedent("""
        import pandas as pd
        import matplotlib.pyplot as plt

        df = pd.read_csv('data.csv')
        summary = df.groupby('category').agg({'amount': 'sum'}).reset_index()
        ax = summary.plot(kind='bar', x='category', y='amount', figsize=(10, 6))
        ax.set_title('Spend by Category')
        plt.tight_layout()
        plt.savefig('summary.png', dpi=300)
        print(summary.to_string())
        """)
    validate_diagram_code(src)


def test_seaborn_heatmap_passes() -> None:
    src = textwrap.dedent("""
        import seaborn as sns
        import matplotlib.pyplot as plt
        import numpy as np

        data = np.random.RandomState(42).rand(10, 12)
        plt.figure(figsize=(12, 8))
        sns.heatmap(data, annot=True, cmap='viridis')
        plt.title('Heatmap')
        plt.savefig('heatmap.png', dpi=300, bbox_inches='tight')
        """)
    validate_diagram_code(src)


def test_multiline_with_comments_and_lambdas_passes() -> None:
    src = textwrap.dedent("""
        # plot a transformation
        import numpy as np
        import matplotlib.pyplot as plt

        f = lambda v: v ** 2 + 1  # quadratic
        xs = np.linspace(-5, 5, 100)
        ys = [f(v) for v in xs]
        plt.plot(xs, ys)
        plt.savefig('q.png')
        """)
    validate_diagram_code(src)
