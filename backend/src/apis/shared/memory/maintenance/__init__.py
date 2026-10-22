"""Memory maintenance (Shared Projects 2.6, §4.6): snapshot, plan, verify, propose.

The pure pieces (``ops``, ``verify``) are imported by the memory service; the
planner and runner by the maintenance worker. Nothing is re-exported here, so
importing one never drags in the others.
"""
