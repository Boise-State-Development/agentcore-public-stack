"""Shared image handling.

Square-icon validation, normalization and object storage, used by anything that
lets an admin or author attach a small square image to a record (Agents, managed
models). Format sniffing, EXIF stripping and the size ladder live here exactly
once — a second copy would drift, and the one that drifted would be the one that
published somebody's GPS coordinates.
"""
