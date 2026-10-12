"""Context-bound tools that author the invoking user's own skills (skill-creator spec).

:mod:`.tools` holds the seven ``my_skill_*`` factories, built per request by
``_build_skill_authoring_tools`` in ``apis/inference_api/chat/routes.py`` and
injected as ``extra_tools``. They are deliberately NOT registered in
``builtin_tools/__init__`` because they need the request's identity.

:mod:`.approval` shapes the approval card for the four writes and is imported by
``base_agent`` for every agent, so this package imports neither eagerly.
"""
