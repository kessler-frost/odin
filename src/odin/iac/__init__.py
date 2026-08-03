"""Deterministic infrastructure-as-code translation, both directions.

`odin.iac.hcl` builds Terraform/OpenTofu HCL from a canvas Stack; `odin.iac.
import_tf` parses HCL back into a Stack. Neither imports `claude_agent_sdk`
and neither ever calls a model: given the same input they emit the same
output, and that is the point — per NORTHSTAR.md's 2026-07-30 amendment,
where a deterministic function exists we write it, and intelligence is
reserved for the places where one provably does not. The model-calling code
lives in `odin.agent` (chat, debugger, the off-by-default refine pass); this
package deliberately does not.
"""
from __future__ import annotations
