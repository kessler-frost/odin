"""Terraform/OpenTofu integration: generate HCL provider config and drive `tofu`."""
from odin.terraform.provider import render_provider
from odin.terraform.runner import PlanResult, TofuRunner

__all__ = ["render_provider", "PlanResult", "TofuRunner"]
