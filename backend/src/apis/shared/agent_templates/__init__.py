"""Admin-managed catalog of agent templates.

A template is a curated, Save-able starting point for a new agent — a subset of
the ``Agent`` shape (name, emoji, description, instructions, starters, model and
bindings) plus catalog-management metadata (enabled, sort order, pitch). Users
pick one on the create-agent page and the form opens pre-filled.

Mirrors the ``system_prompts`` admin-catalog layout: models / repository /
service here, admin CRUD under ``app_api/admin/agent_templates``, and a public
read endpoint under ``app_api/agent_templates`` that the picker consumes.
"""
