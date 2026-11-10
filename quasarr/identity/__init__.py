# Quasarr — Maja fork (x13ijuu)
"""Fork-owned identity logic.

Lives in its own package for the same reason ``providers/host_bans.py`` does:
an upstream rebase never touches a file upstream does not have. Modules here
own their own DB tables, take an explicit ``now``, fail open, and are imported
lazily at each call site.
"""
