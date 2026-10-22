"""Shared Projects (docs/specs/shared-projects.md).

Import from the submodules directly. This package deliberately re-exports
nothing: ``access`` is imported by the assistants service, and an eager
``service`` import here would complete an import cycle through the harness.
"""
