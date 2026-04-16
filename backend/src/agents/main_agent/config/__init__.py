"""
Centralized configuration constants for the main_agent package.

All environment variable names, default values, and shared string constants
are defined here. Modules should import from this package instead of using
inline os.getenv() calls with hardcoded strings.
"""

from agents.main_agent.config.constants import EnvVars, Defaults, Prefixes
