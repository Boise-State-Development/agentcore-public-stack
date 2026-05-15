"""Built-in tools powered by AWS Bedrock services

This package contains tools that leverage AWS Bedrock capabilities:
- Code Interpreter: Execute Python code for diagrams and charts
- Spreadsheet Analysis: Analyze tabular data via Code Interpreter (factory-produced, not in registry)
"""

from .code_interpreter_diagram_tool import generate_diagram_and_validate
from .spreadsheet_analysis import make_list_spreadsheets_tool, make_analyze_tool

# Only static tools go in __all__ (registered in ToolRegistry at startup).
# Factory-produced tools (make_list_spreadsheets_tool, make_analyze_tool) are created
# per-request with context and injected via extra_tools — not registered here.
__all__ = [
    'generate_diagram_and_validate',
]
