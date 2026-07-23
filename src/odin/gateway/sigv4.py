"""Verify inbound SigV4 requests and re-sign outbound ones.

Both directions go through botocore's own canonicalization -- never
`add_auth` on the INCOMING request (that re-stamps `X-Amz-Date` to now(),
which can never match a signature computed against the original
timestamp). Ported from the research prototype
(.superpowers/sdd/research-iam-gateway.md §Q1): only the headers named in
the Authorization header's `SignedHeaders` are placed on the `AWSRequest`,
so botocore canonicalizes exactly what the client signed; the service (and
hence `S3SigV4Auth` vs `SigV4Auth` -- S3 skips path normalization) is read
from the credential scope, which is also how one gateway port routes every
service (no URL-based routing needed).
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable

from botocore.auth import S3SigV4Auth, SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

_ALGORITHM = "AWS4-HMAC-SHA256"
_ALGORITHM_PREFIX = _ALGORITHM + " "


def _lower_headers(headers: dict[str, str]) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items()}


def _parse_credential(fields: dict[str, str]) -> tuple[str, str, str] | None:
    """(access_key, region, service) from `Credential=AK/date/region/service/aws4_request`."""
    parts = fields["Credential"].split("/")
    if len(parts) != 5 or parts[4] != "aws4_request":
        return None
    access_key, _date, region, service, _suffix = parts
    return access_key, region, service


def _parse_authorization(headers: dict[str, str]) -> dict[str, str] | None:
    value = _lower_headers(headers).get("authorization")
    if value is None or not value.startswith(_ALGORITHM_PREFIX):
        return None
    fields: dict[str, str] = {}
    for chunk in value[len(_ALGORITHM_PREFIX):].split(", "):
        name, sep, val = chunk.partition("=")
        if not sep:
            return None
        fields[name.strip()] = val.strip()
    if not {"Credential", "SignedHeaders", "Signature"} <= fields.keys():
        return None
    return fields


def identify(headers: dict[str, str]) -> tuple[str, str, str] | None:
    """(access_key, region, service) parsed from the Authorization header's
    credential scope, WITHOUT verifying the signature -- lets a caller (the
    gateway) know who's asking and for which service even when
    verification is about to fail or hasn't run yet, so an auth-failure
    response can still be shaped for the right protocol."""
    fields = _parse_authorization(headers)
    if fields is None:
        return None
    return _parse_credential(fields)


def scope(headers: dict[str, str]) -> tuple[str, str] | None:
    """(service, region) read from the Authorization header's credential scope."""
    identified = identify(headers)
    if identified is None:
        return None
    _access_key, region, service = identified
    return service, region


def verify(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    secret_for: Callable[[str], str | None],
) -> str | None:
    """Recompute the SigV4 signature against the request's ORIGINAL
    X-Amz-Date and return the access key on a match, else None.

    Rejects `STREAMING-*` payloads outright (v1 cut -- callers 501 them).
    """
    fields = _parse_authorization(headers)
    if fields is None:
        return None
    credential = _parse_credential(fields)
    if credential is None:
        return None
    access_key, region, service = credential
    lower = _lower_headers(headers)
    content_sha256 = lower.get("x-amz-content-sha256", "")
    if content_sha256.startswith("STREAMING-"):
        return None
    # When present, botocore's canonical_request uses this header's VALUE
    # verbatim rather than hashing the actual body, so a valid signature
    # alone doesn't prove the body wasn't swapped -- cross-check it against
    # the real bytes independently.
    if content_sha256 and content_sha256 != "UNSIGNED-PAYLOAD" and content_sha256 != hashlib.sha256(body).hexdigest():
        return None
    timestamp = lower.get("x-amz-date")
    if timestamp is None:
        return None
    secret = secret_for(access_key)
    if secret is None:
        return None
    signed_names = fields["SignedHeaders"].split(";")
    signed_headers = {name: lower[name] for name in signed_names if name in lower}
    request = AWSRequest(method=method, url=url, data=body, headers=signed_headers)
    request.context["timestamp"] = timestamp
    auth_cls = S3SigV4Auth if service == "s3" else SigV4Auth
    signer = auth_cls(Credentials(access_key, secret), service, region)
    canonical_request = signer.canonical_request(request)
    string_to_sign = signer.string_to_sign(request, canonical_request)
    computed = signer.signature(string_to_sign, request)
    if not hmac.compare_digest(computed, fields["Signature"]):
        return None
    return access_key


def resign(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    access_key: str,
    secret: str,
    service: str,
    region: str,
) -> dict[str, str]:
    """Fresh SigV4 headers for forwarding `headers`/`body` under the
    backing's own credentials -- ordinary fresh-timestamp `add_auth` on an
    OUTBOUND copy of the request; the caller's original request is never
    touched or re-signed in place.
    """
    request = AWSRequest(method=method, url=url, data=body, headers=dict(headers))
    auth_cls = S3SigV4Auth if service == "s3" else SigV4Auth
    auth_cls(Credentials(access_key, secret), service, region).add_auth(request)
    return dict(request.headers.items())
