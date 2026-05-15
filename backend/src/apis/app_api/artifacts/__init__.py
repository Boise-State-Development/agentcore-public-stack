"""Artifact render-token minter (app-api).

Issues short-lived HS256 JWTs that the artifact render Lambda (a
separate deployable) verifies before serving a sandboxed iframe. The
claim shape and DDB lookup keys here are a cross-PR contract with that
Lambda — see `backend/src/lambdas/artifact_render/handler.py`.
"""
