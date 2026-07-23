"""V5a -- classify's ecs branch: one test per action asserting the exact
(action, resource) tuple against a REAL captured boto3 request. ECS is
JSON-target protocol (like ecr) and shares ecr/ec2/iam's OPERATOR-only
reasoning -- extraction never returns None, falling back to "*" for
resource-agnostic calls (see classify.py's module docstring).
"""
from __future__ import annotations

from odin.gateway.classify import classify

from .conftest import split_url


def _classified(req):
    path, query = split_url(req.url)
    return classify("ecs", req.method, path, query, req.headers, req.body)


def test_create_cluster_resolves_its_own_name(sink, ecs):
    req = sink.call(lambda: ecs.create_cluster(clusterName="odin"))
    assert _classified(req) == ("ecs:CreateCluster", "odin")


def test_describe_clusters_by_name_list(sink, ecs):
    req = sink.call(lambda: ecs.describe_clusters(clusters=["odin"]))
    assert _classified(req) == ("ecs:DescribeClusters", "odin")


def test_describe_clusters_unfiltered_has_no_id(sink, ecs):
    req = sink.call(lambda: ecs.describe_clusters())
    assert _classified(req) == ("ecs:DescribeClusters", "*")


def test_delete_cluster(sink, ecs):
    req = sink.call(lambda: ecs.delete_cluster(cluster="odin"))
    assert _classified(req) == ("ecs:DeleteCluster", "odin")


def test_register_task_definition_resolves_family(sink, ecs):
    req = sink.call(lambda: ecs.register_task_definition(
        family="app", containerDefinitions=[{"name": "app", "image": "nginx:alpine"}],
    ))
    assert _classified(req) == ("ecs:RegisterTaskDefinition", "app")


def test_describe_task_definition_resolves_family_and_revision(sink, ecs):
    req = sink.call(lambda: ecs.describe_task_definition(taskDefinition="app:3"))
    assert _classified(req) == ("ecs:DescribeTaskDefinition", "app:3")


def test_deregister_task_definition(sink, ecs):
    req = sink.call(lambda: ecs.deregister_task_definition(taskDefinition="app:1"))
    assert _classified(req) == ("ecs:DeregisterTaskDefinition", "app:1")


def test_create_service_resolves_its_own_name(sink, ecs):
    req = sink.call(lambda: ecs.create_service(cluster="odin", serviceName="app", taskDefinition="app:1"))
    assert _classified(req) == ("ecs:CreateService", "app")


def test_describe_services_by_name_list(sink, ecs):
    req = sink.call(lambda: ecs.describe_services(cluster="odin", services=["app"]))
    assert _classified(req) == ("ecs:DescribeServices", "app")


def test_update_service(sink, ecs):
    req = sink.call(lambda: ecs.update_service(cluster="odin", service="app", desiredCount=2))
    assert _classified(req) == ("ecs:UpdateService", "app")


def test_delete_service(sink, ecs):
    req = sink.call(lambda: ecs.delete_service(cluster="odin", service="app", force=True))
    assert _classified(req) == ("ecs:DeleteService", "app")


def test_list_tasks_resolves_service_name(sink, ecs):
    req = sink.call(lambda: ecs.list_tasks(cluster="odin", serviceName="app"))
    assert _classified(req) == ("ecs:ListTasks", "app")


def test_list_tasks_unfiltered_has_no_id(sink, ecs):
    req = sink.call(lambda: ecs.list_tasks(cluster="odin"))
    assert _classified(req) == ("ecs:ListTasks", "*")


def test_describe_tasks_resolves_first_arn(sink, ecs):
    task_arn = "arn:aws:ecs:us-east-1:000000000000:task/odin/deadbeef"
    req = sink.call(lambda: ecs.describe_tasks(cluster="odin", tasks=[task_arn]))
    assert _classified(req) == ("ecs:DescribeTasks", "deadbeef")
