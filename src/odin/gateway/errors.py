"""Protocol-correct error responses per AWS service wire format, so
boto3/aws-cli raise the SAME `ClientError` shape a real AWS deny would
produce (PRD R1: "Errors are protocol-correct per service"). The v1
service set spans three wire protocols:

- s3:            REST-XML, a bare ``<Error>`` document.
- sns:            query-XML, wrapped in ``<ErrorResponse><Error>...`` --
                  SNS is botocore's "query" protocol, whose error parser
                  looks one level deeper than S3's REST-XML.
- dynamodb/sqs:   AWS JSON, ``{"__type": "...#XException", "message": ...}``
                  -- botocore's JSON error parser derives `Code` from the
                  part of `__type` after `#`, so it must carry the
                  conventional `...Exception` suffix (real AWS: DynamoDB/
                  SQS permission denials surface as `AccessDeniedException`,
                  not the bare `AccessDenied` S3/SNS use).
- ec2:            the EC2 protocol's OWN envelope,
                  ``<Response><Errors><Error>...</Error></Errors>
                  <RequestID>...</RequestID></Response>`` -- distinct from
                  SNS's query shape (verified against botocore's
                  `EC2QueryParser._do_error_parse`, incl. the EC2-specific
                  capital-D ``RequestID``).

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

_STATUS = {
    "InvalidClientTokenId": 401,
    "SignatureDoesNotMatch": 401,
    "AccessDenied": 403,
    "ServiceUnavailable": 503,
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


def _json_body_raw(service: str, code: str, message: str) -> str:
    prefix = _JSON_TYPE_PREFIX.get(service, "")
    return json.dumps({"__type": f"{prefix}{code}", "message": message})


def _json_body(service: str, code: str, message: str) -> str:
    return _json_body_raw(service, f"{code}Exception", message)


def _respond(service: str, code: str, message: str) -> Response:
    status = _STATUS[code]
    if service == "s3":
        return Response(_s3_xml(code, message), status_code=status, media_type="application/xml")
    if service == "sns":
        return Response(_sns_xml(code, message), status_code=status, media_type="text/xml")
    if service == "ec2":
        return Response(_ec2_xml(code, message), status_code=status, media_type="text/xml")
    return Response(_json_body(service, code, message), status_code=status, media_type="application/x-amz-json-1.0")


def access_denied(service: str, action: str) -> Response:
    """The default-deny outcome: no statement allowed `action`, or the
    request couldn't even be classified into an action (v1's clean-fail
    path for unmappable requests -- see classify.py's module docstring)."""
    return _respond(service, "AccessDenied", f"User is not authorized to perform: {action}")


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
    if service == "sns":
        return Response(_sns_xml(code, message), status_code=status, media_type="text/xml")
    if service == "ec2":
        return Response(_ec2_xml(code, message), status_code=status, media_type="text/xml")
    return Response(_json_body_raw(service, code, message), status_code=status, media_type="application/x-amz-json-1.0")
