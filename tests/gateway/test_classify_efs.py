"""classify.py's `elasticfilesystem` branch -- odin's SECOND rest-json service.

Every request below is a REAL boto3-signed capture, so the method, path and
query string are the ones the wire really carries rather than ones this file
made up. That matters more here than for a query-protocol service: rest-json
routing IS the path, so a hand-written path would test the regex against
itself.

The route table is deliberately SEVEN entries -- exactly the operations a real
`tofu apply` + `plan` + `destroy` over `aws_efs_file_system` +
`aws_efs_access_point` was measured calling. `test_the_route_table_is_the_
measured_call_sequence` is the ratchet on that: a route added for an operation
the provider never calls is a permission nothing can exercise.
"""
from __future__ import annotations

import pytest

from odin.gateway import sigv4
from odin.gateway.classify import _EFS_ROUTES, classify
from odin.gateway.models import efsctl

from .conftest import split_url

SCOPE = "elasticfilesystem"


def _classify(req):
    path, query = split_url(req.url)
    return classify(SCOPE, req.method, path, query, req.headers, req.body)


def test_create_file_system_is_named_by_its_creation_token(sink, efs):
    """The creation token is the canvas LABEL (`agent/hcl.py` emits
    `creation_token = "<label>"`), and a create carries no id yet -- so the
    token is the only thing on the wire that names the resource."""
    req = sink.call(lambda: efs.create_file_system(CreationToken="shared-data"))
    assert _classify(req) == ("elasticfilesystem:CreateFileSystem", "shared-data")


def test_describe_file_systems_is_named_by_the_query_string(sink, efs):
    req = sink.call(lambda: efs.describe_file_systems(FileSystemId="fs-00000000000000001"))
    assert _classify(req) == ("elasticfilesystem:DescribeFileSystems", "fs-00000000000000001")


def test_an_unfiltered_list_is_the_wildcard_not_a_resource(sink, efs):
    """An unscoped list is a DIFFERENT permission from reading the one file
    system an edge points at -- the exact distinction field test 2 cost an
    engineer an hour over on rds. `*` is what makes a statement granting one
    resource fail to match it."""
    assert _classify(sink.call(lambda: efs.describe_file_systems())) == ("elasticfilesystem:DescribeFileSystems", "*")
    assert _classify(sink.call(lambda: efs.describe_access_points())) == ("elasticfilesystem:DescribeAccessPoints", "*")


def test_delete_and_lifecycle_are_named_by_the_path(sink, efs):
    delete = sink.call(lambda: efs.delete_file_system(FileSystemId="fs-00000000000000001"))
    assert _classify(delete) == ("elasticfilesystem:DeleteFileSystem", "fs-00000000000000001")

    lifecycle = sink.call(lambda: efs.describe_lifecycle_configuration(FileSystemId="fs-00000000000000001"))
    assert _classify(lifecycle) == ("elasticfilesystem:DescribeLifecycleConfiguration", "fs-00000000000000001")


def test_the_lifecycle_path_does_not_collide_with_the_delete_path(sink, efs):
    """`/file-systems/{id}` and `/file-systems/{id}/lifecycle-configuration`
    share a prefix, and both regexes are anchored -- so a lifecycle GET must
    never classify as a DeleteFileSystem, whatever order the table is in."""
    lifecycle = sink.call(lambda: efs.describe_lifecycle_configuration(FileSystemId="fs-00000000000000001"))
    action, _resource = _classify(lifecycle)
    assert action == "elasticfilesystem:DescribeLifecycleConfiguration"


def test_access_point_operations_classify(sink, efs):
    create = sink.call(lambda: efs.create_access_point(ClientToken="t", FileSystemId="fs-00000000000000001"))
    assert _classify(create) == ("elasticfilesystem:CreateAccessPoint", "fs-00000000000000001")

    describe = sink.call(lambda: efs.describe_access_points(AccessPointId="fsap-00000000000000001"))
    assert _classify(describe) == ("elasticfilesystem:DescribeAccessPoints", "fsap-00000000000000001")

    delete = sink.call(lambda: efs.delete_access_point(AccessPointId="fsap-00000000000000001"))
    assert _classify(delete) == ("elasticfilesystem:DeleteAccessPoint", "fsap-00000000000000001")


def test_describe_access_points_by_file_system_reports_the_file_system(sink, efs):
    req = sink.call(lambda: efs.describe_access_points(FileSystemId="fs-00000000000000001"))
    assert _classify(req) == ("elasticfilesystem:DescribeAccessPoints", "fs-00000000000000001")


@pytest.mark.parametrize("arn_form", [
    "arn:aws:elasticfilesystem:us-east-1:000000000000:file-system/fs-00000000000000001",
])
def test_an_arn_reduces_to_the_bare_id(sink, efs, arn_form):
    """Every EFS id member accepts an ARN as well as a bare id -- its own
    botocore patterns spell the alternative out -- so a policy written against
    one form has to match the other. `classify` reports the bare id, which is
    what `policy.py`'s reducer and every store key already use."""
    req = sink.call(lambda: efs.describe_file_systems(FileSystemId=arn_form))
    assert _classify(req) == ("elasticfilesystem:DescribeFileSystems", "fs-00000000000000001")


def test_an_unmodeled_efs_path_is_unmappable_and_therefore_denied(sink, efs):
    """`CreateMountTarget` is real EFS and odin models none of it. classify
    answering None is what makes it a clean default-deny in `app.py` rather
    than a route that half-works."""
    req = sink.call(lambda: efs.create_mount_target(
        FileSystemId="fs-00000000000000001", SubnetId="subnet-00000000000000001",
    ))
    assert _classify(req) is None


def test_the_route_table_is_the_measured_call_sequence():
    """SEVEN operations, and they are exactly the ones a real
    `tofu apply`/`plan`/`destroy` calls -- verified by recording the wire, not
    by reading the API reference. `DescribeBackupPolicy`,
    `DescribeFileSystemPolicy`, `ListTagsForResource`, `DescribeMountTargets`
    and `TagResource` are NEVER called by the provider, so a route for one of
    them would be dead code carrying a permission nothing can exercise.

    Pinned against `efsctl._HANDLERS` in both directions, so a route with no
    handler (a 400 on a call tofu really makes) and a handler with no route
    (unreachable code) are both failures."""
    routed = {op for _method, _pattern, op in _EFS_ROUTES}
    assert routed == {
        "CreateFileSystem", "DescribeFileSystems", "DeleteFileSystem",
        "DescribeLifecycleConfiguration",
        "CreateAccessPoint", "DescribeAccessPoints", "DeleteAccessPoint",
    }
    assert routed == set(efsctl._HANDLERS), (
        "classify and efsctl disagree about which EFS operations exist"
    )


def test_efs_requests_are_not_classified_under_another_services_scope(sink, efs):
    """`classify` dispatches on the SigV4 credential scope, and EFS's is
    `elasticfilesystem` -- not botocore's model name `efs`. Getting that wrong
    would make every EFS call unmappable and silently denied."""
    req = sink.call(lambda: efs.create_file_system(CreationToken="shared-data"))
    path, query = split_url(req.url)
    assert classify("efs", req.method, path, query, req.headers, req.body) is None
    assert classify(SCOPE, req.method, path, query, req.headers, req.body) is not None


def test_a_real_signed_efs_request_really_scopes_to_elasticfilesystem(sink, efs):
    """THE signal `app.py` actually reads, taken off a REAL signature.

    `app.py` picks BOTH the classifier branch and the error wire format from
    `sigv4.identify`'s credential scope. Every other test in this file passes
    that string in by hand, which proves the routing and not the input -- so
    this one asks boto3 what it really signs. A guard keyed on a service name
    nothing sends is this repo's most-repeated bug, and the whole EFS surface
    hangs off this one token."""
    req = sink.call(lambda: efs.create_file_system(CreationToken="shared-data"))
    _access_key, region, service = sigv4.identify(dict(req.headers))
    assert service == SCOPE, f"boto3 signs EFS for {service!r}, so app.py would route it there"
    assert region == "us-east-1"
