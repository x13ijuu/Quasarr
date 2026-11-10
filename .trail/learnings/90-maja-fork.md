---
title: Maja fork (x13ijuu) - behaviour that differs from upstream
date: 2026-10-24
author: Maja (x13ijuu fork)
touches:
  - quasarr/identity/refusals.py
  - quasarr/search/__init__.py
  - quasarr/providers/sonarr_api.py
  - quasarr/downloads/sources/al.py
  - quasarr/downloads/__init__.py
---

Carried over from the fork's additions to the per-directory AGENTS.md files, which upstream replaced with Trail learnings in 4.7.7. The fork is frozen (FROZEN.md): these notes describe what exists, they do not invite new behaviour.

## Decided

- quasarr/identity/refusals.py is fork-only: the refusal ledger records a release whose delivery disproved its own title, so the same release is not grabbed again.
- Absolute-numbered requests reach season-only sources too. A request carrying an episode without a season (the form Sonarr uses for anime series) used to skip every source that cannot search by absolute number. quasarr/search/__init__.py resolves the absolute number once per search via quasarr/providers/sonarr_api.py and sends the translated season and episode to those sources, while absolute-capable sources (AL, AT) still receive the absolute form. Fail-open: an unresolvable number restores the old skip.
- AL feed grabs carry no release id, and one details page can list the same episode under several releases that differ only in audio. quasarr/downloads/sources/al.py resolves the tab by exact title first, then by the advertised audio combined with the tab actually offering that episode; both halves are required. A title advertising an audio language no tab can satisfy is refused; only a title with nothing to match on falls back to tab 1.
- Episode membership on AL reads the row labels first and falls back to the zero-based position only when no usable label exists, so search, the audio resolver and the refusal guard agree with the label-based download selection.
- quasarr/downloads/__init__.py keeps a unique package id per grab (SABnzbd contract: Sonarr tracks downloads by that id). The source identity enters the hash as well, and download() keeps the pre-minted id instead of resetting it.

## Watch out

Why the absolute-number fan-out exists: AL was once the only source asked for absolute-numbered anime, and a multi-day AL outage blinded the whole pipeline while SF had more matching German-audio hits for the same episodes.
