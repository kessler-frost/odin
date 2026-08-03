"""Odin: a local-first AWS-compatible cloud.

Design AWS architectures on a drag-drop canvas; apply them for real onto local
open-source substitutes (RustFS, goaws, dynalite, Postgres) on Colima/Lima.

Word-for-word `pyproject.toml`'s `description`, which is the accurate one. This
line used to read "AI-controlled local AWS simulator" and neither half was true:
odin's translation path is a DETERMINISTIC compiler (`iac/hcl.py`) that runs
with `ODIN_AI=0`, and the substrates are real processes, not a simulator.
"""
