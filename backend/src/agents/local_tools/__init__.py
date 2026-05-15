"""Local tools for general-purpose tasks

This package contains tools that don't require specific AWS services:
- URL fetching and content extraction
- Data visualization
"""

from .url_fetcher import fetch_url_content
from .visualization import create_visualization

__all__ = [
    'fetch_url_content',
    'create_visualization',
]
