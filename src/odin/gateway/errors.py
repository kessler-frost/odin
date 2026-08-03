"""Protocol-correct error responses per AWS service wire format, so
boto3/aws-cli raise the SAME `ClientError` shape a real AWS deny would
produce (PRD R1: "Errors are protocol-correct per service"). The v1
service set spans four wire protocols:

- s3:            REST-XML, a bare ``<Error>`` document.
- sns/iam/rds/
  elasticache/
  elbv2/sts:      query-XML, wrapped in ``<ErrorResponse><Error>...`` --
                  all five are botocore's "query" protocol (verified against
                  each service's own model), whose error parser looks one
                  level deeper than S3's REST-XML. IAM, RDS (task W2.7),
                  ElastiCache and elbv2 (`elasticloadbalancing`, task W2.5)
                  share SNS's exact envelope shape, so all of them route
                  through `_sns_xml`.
- dynamodb/sqs/ecr: AWS JSON, ``{"__type": "...#XException", "message": ...}``
                  -- botocore's JSON error parser derives `Code` from the
                  part of `__type` after `#`, so it must carry the
                  conventional `...Exception` suffix (real AWS: DynamoDB/
                  SQS permission denials surface as `AccessDeniedException`,
                  not the bare `AccessDenied` S3/SNS use). ECR falls into
                  this same default JSON branch -- no dedicated case needed.
- ec2:            the EC2 protocol's OWN envelope,
                  ``<Response><Errors><Error>...</Error></Errors>
                  <RequestID>...</RequestID></Response>`` -- distinct from
                  SNS's query shape (verified against botocore's
                  `EC2QueryParser._do_error_parse`, incl. the EC2-specific
                  capital-D ``RequestID``).
- lambda/
  elasticfilesystem: rest-json -- botocore's `RestJSONParser
                  ._inject_error_code` (verified against botocore's own
                  parsers.py) derives `Code` from the response HEADER
                  `x-amzn-errortype` FIRST, only falling back to a body
                  `code`/`Code` field if that header is absent -- so unlike
                  every other branch here (which encodes the code purely in
                  the body), these two carry it in both places: the
                  header (what botocore actually reads) and a body in the
                  service's own shape (`Type`/`Message` for Lambda,
                  `ErrorCode`/`Message` for EFS) for a human reading the raw
                  response. For EFS that split was MEASURED rather than
                  assumed, and the header turned out to be load-bearing --
                  see `_efs_response`.

Status codes follow the brief's literal choices (401 for the two SigV4
auth failures, 403 for AccessDenied, 503 for a dead backing) rather than
chasing exact real-AWS status-per-service fidelity -- botocore's error
parsers trigger on any status >= 300, so this doesn't affect `Code`
extraction, and a single mapping keeps this module branching-free.
"""
from __future__ import annotations

import json

from starlette.responses import Response

_JSON_TYPE_PREFIX = {
    "dynamodb": "com.amazonaws.dynamodb.v20120810#",
    "sqs": "com.amazonaws.sqs#",
}

# Services whose errors ride botocore's "query" protocol envelope
# (``<ErrorResponse><Error>...``) -- see the module docstring.
_QUERY_XML_SERVICES = ("sns", "iam", "rds", "elasticache", "elasticloadbalancing", "sts")

_STATUS = {
    "InvalidClientTokenId": 401,
    "SignatureDoesNotMatch": 401,
    "AccessDenied": 403,
    "ServiceUnavailable": 503,
    "InternalFailure": 500,
}


def _s3_xml(code: str, message: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Error><Code>{code}</Code><Message>{message}</Message></Error>"
    )


def _sns_xml(code: str, message: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<ErrorResponse><Error>"
        f"<Type>Sender</Type><Code>{code}</Code><Message>{message}</Message>"
        "</Error></ErrorResponse>"
    )


def _ec2_xml(code: str, message: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Errors><Error>"
        f"<Code>{code}</Code><Message>{message}</Message>"
        "</Error></Errors>"
        "<RequestID>00000000-0000-0000-0000-000000000000</RequestID></Response>"
    )


# The rest-json services. Their errors carry the code in the `x-amzn-errortype`
# HEADER, which is what botocore's `RestJSONParser._inject_error_code` reads
# FIRST (verified against botocore's own parsers.py) and what aws-sdk-go-v2's
# restjson decoder reads first too -- so a body-only code is a coin flip.
# `apigateway` joined in v0.8.19: BOTH API Gateway v1 and v2 sign under that
# credential scope, and both are rest-json.
_RESTJSON_SERVICES = ("lambda", "apigateway")

# The error body each rest-json service really sends. Lambda's is
# `{"Type", "Message"}`; apigatewayv2's is `{"message"}` alone (lowercase),
# which is also what its `NotFoundException` shape names. Neither is what
# botocore reads the CODE from -- that is the header above -- but a human
# reading a raw response, and any client that falls back to the body, gets the
# real thing rather than a plausible-looking one from another service.
_RESTJSON_BODY = {
    "lambda": lambda message: {"Type": "User", "Message": message},
    "apigateway": lambda message: {"message": message},
}


def _restjson_response(service: str, code: str, message: str, status: int) -> Response:
    body = json.dumps(_RESTJSON_BODY[service](message))
    return Response(body, status_code=status, media_type="application/json", headers={"x-amzn-errortype": code})


def _efs_response(code: str, message: str, status: int) -> Response:
    """EFS's own error shape, plus the header both SDKs actually read.

    THE HEADER IS LOAD-BEARING AND THE BODY MEMBER NAME IS NOT ENOUGH -- probed
    against a real `tofu destroy` before this was written, because EFS's model
    makes `ErrorCode` a REQUIRED member and it would be natural to assume that
    is what a client parses. It is not. Measured, both forms, same recorder:

        404 + `x-amzn-errortype` + body -> tofu destroy exit 0
        404 + body only                 -> tofu destroy exit 1,
            "waiting for EFS Access Point (fsap-...) delete: ... api error
             UnknownError: Access point fsap-... does not exist."

    and botocore's own `RestJSONParser` agrees: the header form parses
    `Code='FileSystemNotFound'`, the body-only form parses `Code='404'`. Both
    clients derive the code from `x-amzn-errortype` (or a lowercase `code` /
    `__type`), and neither one looks at `ErrorCode`.

    So both go out: the header is what a client reads, and `{"ErrorCode",
    "Message"}` is what real EFS puts in the body for a human reading the raw
    response. Same split `_lambda_response` above makes, for the same reason --
    these are odin's only two rest-json services."""
    body = json.dumps({"ErrorCode": code, "Message": message})
    return Response(body, status_code=status, media_type="application/json", headers={"x-amzn-errortype": code})
def _lambda_response(code: str, message: str, status: int) -> Response:
    """Kept as a name because tests and other modules reference it; the shared
    builder above is the implementation."""
    return _restjson_response("lambda", code, message, status)


def _json_body_raw(service: str, code: str, message: str) -> str:
    prefix = _JSON_TYPE_PREFIX.get(service, "")
    return json.dumps({"__type": f"{prefix}{code}", "message": message})


def _json_body(service: str, code: str, message: str) -> str:
    return _json_body_raw(service, f"{code}Exception", message)


def _wire(service: str, code: str, message: str, status: int, json_body: str) -> Response:
    """The one per-service format dispatch, shared by `_respond` and
    `synth_error`.

    It was TWO identical chains until efs became the sixth format, and the
    duplication had already cost something real: `synth_error`'s copy was
    missing the `s3` branch for eleven releases, so a synth-authored S3 error
    fell through to the AWS-JSON body, which botocore parses with `RestXMLParser`
    and cannot read -- the caller got a parse failure instead of the error odin
    wrote. Two chains, one of them wrong, is the shape of that bug; one chain
    cannot drift from itself.

    The callers still differ in exactly the way they always did, and that is now
    the ONLY thing they express: `_respond` derives the status from `_STATUS` and
    sends the `...Exception`-suffixed JSON `__type`, `synth_error` takes an
    explicit status and the exact wire code."""
    if service == "s3":
        return Response(_s3_xml(code, message), status_code=status, media_type="application/xml")
    if service in _QUERY_XML_SERVICES:
        return Response(_sns_xml(code, message), status_code=status, media_type="text/xml")
    if service == "ec2":
        return Response(_ec2_xml(code, message), status_code=status, media_type="text/xml")
    # BOTH rest-json services, not lambda by name. `apigateway` joined the tuple
    # in v0.8.19 and a merge left this branch naming only lambda, so every
    # apigateway error went out as json-1.0 with NO `x-amzn-errortype` -- the
    # one header both botocore and aws-sdk-go-v2 read FIRST. Caught by
    # `test_a_missing_api_is_a_rest_json_NotFoundException`.
    if service in _RESTJSON_SERVICES:
        return _restjson_response(service, code, message, status)
    if service == "elasticfilesystem":
        return _efs_response(code, message, status)
    return Response(json_body, status_code=status, media_type="application/x-amz-json-1.0")


def _respond(service: str, code: str, message: str) -> Response:
    status = _STATUS[code]
    return _wire(service, code, message, status, _json_body(service, code, message))


_MAX_RESOURCE_CHARS = 200


def access_denied(service: str, action: str, resource: str | None = None) -> Response:
    """The default-deny outcome: no statement allowed `action`, or the
    request couldn't even be classified into an action (v1's clean-fail
    path for unmappable requests -- see classify.py's module docstring).

    Naming the RESOURCE is what makes the denial actionable. Field test 2
    cost an engineer an hour on an unscoped `DescribeDBInstances()`: an edge
    grants an action on the one resource it points at, so the wildcard list
    is a different permission -- but the message said only "not authorized
    to perform: rds:DescribeDBInstances", which reads as "the edge didn't
    work" rather than "you asked about a different resource". Real AWS names
    both. The unmappable-action path has no resource and passes None."""
    subject = f"{action} on resource: {resource[:_MAX_RESOURCE_CHARS]!r}" if resource else action
    return _respond(service, "AccessDenied", f"User is not authorized to perform: {subject}")


def auth_error(service: str, code: str, message: str) -> Response:
    """A SigV4 identification/verification failure -- `code` is
    "InvalidClientTokenId" (unknown access key) or "SignatureDoesNotMatch"
    (signature didn't verify)."""
    return _respond(service, code, message)


def service_unavailable(service: str) -> Response:
    """The env's backing for `service` isn't registered/running -- R6's
    "dead backing" failure mode: fail closed with a distinct error rather
    than hang or silently drop the request."""
    return _respond(service, "ServiceUnavailable", "The backing service is not currently available")


def internal_failure(service: str, exc_name: str) -> Response:
    """The handler of last resort: something raised that no path anticipated.

    It exists because this app is what tofu's AWS provider and every workload
    SDK talk to, and a bare-text 500 handed to botocore or aws-sdk-go-v2 is not
    an error those clients can interpret -- they surface a parse failure, or
    retry an unretryable fault, instead of reporting what went wrong. So even a
    total surprise leaves here wearing the wire shape of the service that was
    asked for.

    Names the exception TYPE and nothing else. The full detail goes to the
    server log, where the operator reads it; this response can reach an
    unauthenticated caller (the gateway binds 0.0.0.0, and a request can fail
    before SigV4 verification runs), so it must not carry paths or values. Real
    AWS's own InternalFailure is opaque for the same reason."""
    return _respond(service, "InternalFailure", f"odin's gateway failed to handle this request ({exc_name})")


def synth_error(service: str, code: str, message: str, status: int) -> Response:
    """A synth-authored error (gateway.synth) with an explicit status and
    the EXACT wire code -- unlike `access_denied`/`auth_error`'s fixed
    status+`...Exception` convention, synth errors must match the REAL wire
    code a caller's SDK checks against, which varies per exception and does
    NOT always match botocore's shape name: SNS's subscription NotFound
    uses that shape's explicit `code` override, `NotFound` (verified
    against botocore's own parser); SQS's "queue doesn't exist" is the
    legacy `AWS.SimpleQueueService.NonExistentQueue` -- botocore's own
    model calls the shape `QueueDoesNotExist`, but that friendlier name is
    NOT what SQS (one of AWS's oldest services) actually sends over the
    wire, and a real `tofu destroy`'s Go-SDK-based delete-waiter checks the
    literal legacy string (S2, verified against terraform-provider-aws's
    own source), not botocore's shape name. EC2's per-kind NotFound codes
    (`InvalidVpcID.NotFound` & co., gateway/models/ec2net.py) ride this same
    exact-wire-code path in the EC2 error envelope."""
    # The s3 branch was MISSING from this function for eleven releases while
    # `_respond` had it all along -- so a synth-authored S3 error fell through to
    # the AWS-JSON body, which botocore parses with RestXMLParser and cannot
    # read, and the caller saw a parse failure instead of the error odin wrote.
    # Found by an agent building S3 notifications, who was about to hand-build
    # REST-XML locally rather than touch this file. `_wire` is that fix taken one
    # step further: there is no second chain left to forget a service in.
    return _wire(service, code, message, status, _json_body_raw(service, code, message))
    # s3 first, and it was MISSING here while `_respond` above has had it all
    # along -- so a synth-authored S3 error fell through to the AWS-JSON body at
    # the bottom, which botocore parses with RestXMLParser and cannot read. The
    # caller sees a parse failure instead of the error odin wrote. Found by an
    # agent building S3 notifications, who was about to hand-build REST-XML
    # locally rather than touch this file; one shared builder is the point of
    # this module.
    if service == "s3":
        return Response(_s3_xml(code, message), status_code=status, media_type="application/xml")
    if service in _QUERY_XML_SERVICES:
        return Response(_sns_xml(code, message), status_code=status, media_type="text/xml")
    if service == "ec2":
        return Response(_ec2_xml(code, message), status_code=status, media_type="text/xml")
    if service in _RESTJSON_SERVICES:
        return _restjson_response(service, code, message, status)
    return Response(_json_body_raw(service, code, message), status_code=status, media_type="application/x-amz-json-1.0")


def exc_text(exc: BaseException) -> str:
    """`ClassName: message`, and something true when there IS no message.

    THE one wording for "an exception is the reason". It lives here because
    this module is a leaf -- it imports nothing from odin -- so every writer
    that records a failure can import it without closing the
    `reconcile.reconciler` -> `reconcile.drift` -> `gateway.models.*` cycle
    that made three of them keep private copies.

    `str(exc)` is `''` for any exception constructed with no arguments, and
    two really do reach these paths: an interpreter-raised `MemoryError`
    (`args == ()`), and `httpx.PoolTimeout`, which httpcore raises bare and
    httpx's own mapping preserves. A blank is not merely ugly -- lambdactl's
    `_configuration_json` renders `state_reason or None` and `_json` drops
    every None, so an empty reason made `StateReason` vanish from the wire and
    GetFunction answered `State: Failed` with no reason at all."""
    return f"{type(exc).__name__}: {exc}" if str(exc) else (
        f"{type(exc).__name__} (raised with no message, so the class is the whole of it)"
    )
