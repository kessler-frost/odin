"""Tier-1 Phase 5 services applied to Moto via tofu.

RDS is split into its own test because Moto simulates its creation slowly
(~80s); the rest apply together in one fast `tofu apply`. If a resource's HCL
isn't Moto-compatible the apply fails and `result.raw` names the offender.
"""
import pytest

pytestmark = pytest.mark.tofu

SERVICES_HCL = """
resource "aws_secretsmanager_secret" "secret" {
  name = "odin-secret"
}

resource "aws_kms_key" "key" {
  description = "odin key"
}

resource "aws_iam_role" "role" {
  name = "odin-role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" } }]
  })
}

resource "aws_route53_zone" "zone" {
  name = "odin.example.com"
}

resource "aws_api_gateway_rest_api" "api" {
  name = "odin-api"
}

resource "aws_efs_file_system" "fs" {
  creation_token = "odin-fs"
}

resource "aws_ssm_parameter" "param" {
  name  = "/odin/param"
  type  = "String"
  value = "hello"
}

resource "aws_kinesis_stream" "stream" {
  name        = "odin-stream"
  shard_count = 1
}

resource "aws_ecs_cluster" "cluster" {
  name = "odin-cluster"
}
"""

RDS_HCL = """
resource "aws_db_instance" "db" {
  identifier          = "odin-db"
  engine              = "postgres"
  instance_class      = "db.t3.micro"
  allocated_storage   = 20
  username            = "admin"
  password            = "password123"
  skip_final_snapshot = true
}
"""


async def _apply(tofu_runner, hcl):
    tofu_runner.work_dir.mkdir(parents=True, exist_ok=True)
    (tofu_runner.work_dir / "main.tf").write_text(hcl)
    result = await tofu_runner.apply()
    assert result.ok, result.raw


async def test_tier1_services_apply_to_moto(moto_engine, tofu_runner):
    await _apply(tofu_runner, SERVICES_HCL)

    c = moto_engine.get_client
    assert any("odin-secret" in s["Name"] for s in c("secretsmanager").list_secrets()["SecretList"])
    assert c("kms").list_keys()["Keys"]
    assert c("iam").get_role(RoleName="odin-role")["Role"]["RoleName"] == "odin-role"
    assert any(z["Name"] == "odin.example.com." for z in c("route53").list_hosted_zones()["HostedZones"])
    assert any(a["name"] == "odin-api" for a in c("apigateway").get_rest_apis()["items"])
    assert c("efs").describe_file_systems()["FileSystems"]
    assert c("ssm").get_parameter(Name="/odin/param")["Parameter"]["Value"] == "hello"
    assert "odin-stream" in c("kinesis").list_streams()["StreamNames"]
    assert c("ecs").list_clusters()["clusterArns"]

    await tofu_runner.destroy()


async def test_rds_applies_to_moto(moto_engine, tofu_runner):
    await _apply(tofu_runner, RDS_HCL)
    dbs = moto_engine.get_client("rds").describe_db_instances()["DBInstances"]
    assert any(i["DBInstanceIdentifier"] == "odin-db" for i in dbs)
    await tofu_runner.destroy()
