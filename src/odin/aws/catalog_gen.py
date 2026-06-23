"""Generate canvas node types from MiniStack's service registry (UI parity).

The hand-curated catalog covers the workload kinds + the most-used AWS services
with rich config. This fills in the long tail so the palette matches everything
MiniStack emulates. Run: `python -m odin.aws.catalog_gen` -> writes
`ui/src/lib/catalog.generated.ts`. The AWS-service nodes are control-plane
(MiniStack serves their API; app containers reach it via the injected endpoint).
"""
from __future__ import annotations

import os
from pathlib import Path

# Internal / non-user-facing, or already curated by hand in catalog.ts.
# (efs/elbv2 are emitted by MiniStack as elasticfilesystem/elasticloadbalancing,
#  which fall through to the generated catalog — that's intended.)
_SKIP = {
    "account", "sts", "imds", "resource-groups", "appconfigdata",
    # already hand-curated:
    "sqs", "sns", "kinesis", "rds", "secretsmanager", "kms", "iam",
    "route53", "apigateway", "ecs", "ssm", "logs", "events",
    "s3", "dynamodb", "ec2", "lambda",
}

_COLORS = ["cyan", "pink", "rose", "indigo", "lime", "amber", "teal", "sky", "fuchsia"]

_LABELS = {
    "elasticache": "ElastiCache", "eks": "EKS", "ecr": "ECR", "cognito-idp": "Cognito",
    "cloudformation": "CloudFormation", "cloudfront": "CloudFront", "cloudtrail": "CloudTrail",
    "stepfunctions": "Step Functions", "eventbridge": "EventBridge", "firehose": "Firehose",
    "opensearch": "OpenSearch", "appsync": "AppSync", "wafv2": "WAF v2", "athena": "Athena",
    "glue": "Glue", "emr": "EMR", "mwaa": "MWAA", "transfer": "Transfer", "iot": "IoT Core",
    "ses": "SES", "sesv2": "SES v2", "acm": "ACM", "backup": "Backup", "batch": "Batch",
    "organizations": "Organizations", "appconfig": "AppConfig", "amazonmq": "Amazon MQ",
}


def _label(name: str) -> str:
    return _LABELS.get(name, name.replace("-", " ").replace("_", " ").title())


def generate_catalog_ts() -> str:
    os.environ.setdefault("MINISTACK_HOST", "localhost")
    import ministack.app as ministack_app

    registry = getattr(ministack_app, "SERVICE_REGISTRY", {})
    stale = _SKIP - set(registry)
    assert not stale, f"stale _SKIP entries (no longer MiniStack services): {sorted(stale)}"
    services = sorted(n for n in registry if n not in _SKIP)

    entries = []
    for i, name in enumerate(services):
        type_id = f"aws_{name.replace('-', '_')}"
        abbr = name.replace("-", "")[:4].upper()
        color = _COLORS[i % len(_COLORS)]
        entries.append(
            f"""  {{
    type: '{type_id}', abbr: '{abbr}', label: '{_label(name)}', sublabel: 'AWS {name}',
    category: 'AWS', color: '{color}', width: 200,
    fields: [{{ key: 'label', label: 'Name', editable: true }}],
    defaultData: {{ label: '{name}' }},
  }},"""
        )

    body = "\n".join(entries)
    return (
        "// AUTO-GENERATED from MiniStack's SERVICE_REGISTRY by "
        "`python -m odin.aws.catalog_gen`. Do not edit by hand.\n"
        "import type { ServiceDef } from './catalog';\n\n"
        f"// {len(services)} AWS services for canvas parity with the embedded emulator.\n"
        "export const GENERATED_CATALOG: ServiceDef[] = [\n"
        f"{body}\n"
        "];\n"
    )


def main() -> None:
    out = Path(__file__).resolve().parents[3] / "ui" / "src" / "lib" / "catalog.generated.ts"
    out.write_text(generate_catalog_ts())
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
