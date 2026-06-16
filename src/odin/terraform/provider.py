"""Generate the fixed AWS provider config that points Terraform at Moto."""
from __future__ import annotations

# Services the canvas can target. Moto serves them all on one endpoint.
SERVICES = ("ec2", "s3", "iam", "lambda", "sts")


def render_provider(endpoint: str, region: str = "us-east-1") -> str:
    """Render `provider.tf`: the AWS provider aimed at the local Moto server.

    Odin owns this file; the agent only writes resource HCL. Credentials are
    dummy and all validation/metadata lookups are skipped so `tofu` talks only
    to Moto.
    """
    endpoints = "\n".join(f'    {svc:<6} = "{endpoint}"' for svc in SERVICES)
    return f"""terraform {{
  required_providers {{
    aws = {{
      source = "hashicorp/aws"
    }}
  }}
}}

provider "aws" {{
  region                      = "{region}"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true

  endpoints {{
{endpoints}
  }}
}}
"""
