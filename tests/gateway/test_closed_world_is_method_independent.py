"""odin's closed world must not depend on the HTTP VERB.

`classify.py`'s docstring states the posture twice: a request the gateway
cannot map to an (action, resource) pair is DENIED, never guessed at. Until
v0.8.19 that property was silently method-limited -- `create_gateway_app`'s
route table listed five verbs, so a PATCH or an OPTIONS was answered by
STARLETTE with a bare `405 Method Not Allowed` before any odin code ran. No
signature check, no policy evaluation, no `on_deny` event, no
`AccessDeniedException`.

MEASURED, on the real app, before the fix:

    PATCH http://127.0.0.1:62976/v2/apis/api123
    status: 405
    body  : Method Not Allowed

It was latent -- no modeled service used PATCH at the time -- and that is
precisely the shape honesty rule 1 exists for: a guarantee that has never been
exercised at its edge is a guarantee nobody has checked. apigateway makes it
live (`UpdateApi` is `PATCH /v2/apis/{apiId}`).

WHY THIS TEST IS THE DELIVERABLE AND THE ONE-LINE FIX IS NOT. The fix is one
list; anyone could shorten it back by accident, and nothing would fail.

The parametrization is over `_CLIENT_VERBS` -- a list stated HERE -- and NOT
over the app's own route table, and that is the whole design of this file. It
was written the other way first, reading the verbs off the real router, which is
strictly worse and the mutation test proved it: deleting PATCH from the table
made the parametrized case simply STOP EXISTING (5 passed where 6 had), so the
property test went green on the very regression it exists to catch. A test
derived from the thing under test cannot fail when that thing shrinks.

So the two directions are asserted separately:

  - `_CLIENT_VERBS` -> a denial, for each. A verb missing from the route table
    fails HERE, with the 405-vs-403 message that names the real cause.
  - the route table -> `_CLIENT_VERBS`. A verb ADDED to the router without being
    checked here fails `test_the_route_table_holds_no_verb_this_file_does_not_check`.

Together those make the property method-INDEPENDENT rather than
method-checked-once.

Requests are signed with botocore's own `SigV4Auth` rather than captured from a
boto3 client, because no boto3 client will emit an OPTIONS request for an
arbitrary path -- and an UNSIGNED request would pass this test for the wrong
reason (it would deny at `unknown-key`, several steps before classification).
Every request here is a genuinely valid signature from a real issued principal,
so the only thing left to deny it is the closed world itself.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from starlette.routing import Route

from odin.gateway.app import GatewayState, create_gateway_app
from odin.gateway.keys import OPERATOR_NODE_ID, KeyStore
from odin.gateway.stores import SynthStores

ENV = "closedworld"
# A service odin models NOTHING for, so `classify()` returns None for every
# verb. `elasticbeanstalk` is deliberately a real AWS service name: the point is
# that odin does not know it, not that the string is nonsense.
UNKNOWN_SERVICE = "elasticbeanstalk"

# Every verb an ordinary HTTP client can reach the gateway with and that odin
# must answer for itself. Stated here rather than read off the router -- see the
# module docstring for the mutation result that forced it. HEAD is excluded
# because Starlette derives it from GET automatically and an httpx HEAD carries
# no body to assert the error document on.
_CLIENT_VERBS = ("GET", "PUT", "POST", "DELETE", "PATCH", "OPTIONS")


@pytest.fixture
def keystore(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path)


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


def _app_and_denials(keystore: KeyStore, stores: SynthStores):
    denials: list[tuple] = []

    async def on_deny(principal, action, resource, reason) -> None:
        denials.append((principal.node_id if principal else None, action, resource, reason))

    state = GatewayState()
    state.update(ENV, {}, {})
    return create_gateway_app(state, keystore, stores, on_deny), denials


def routed_methods(app) -> list[str]:
    """Every verb the gateway's OWN router accepts, read off the real app.

    Used ONLY for the reverse direction (does the router hold a verb this file
    never checks?). The forward direction deliberately does not read it -- see
    the module docstring. `HEAD` is dropped because Starlette adds it implicitly
    for GET routes."""
    for route in app.routes:
        if isinstance(route, Route) and route.path == "/{path:path}":
            return sorted(route.methods - {"HEAD"})
    raise AssertionError("the gateway app no longer has a catch-all route to read methods from")


def _signed(keystore: KeyStore, method: str, url: str) -> AWSRequest:
    access_key, secret_key = keystore.issue(ENV, OPERATOR_NODE_ID)
    request = AWSRequest(method=method, url=url, data=b"", headers={"host": "127.0.0.1"})
    SigV4Auth(Credentials(access_key, secret_key), UNKNOWN_SERVICE, "us-east-1").add_auth(request)
    return request


async def _drive(app, request: AWSRequest) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        return await client.request(
            request.method, request.url, headers=dict(request.headers.items()), content=b"",
        )


@pytest.mark.parametrize("method", _CLIENT_VERBS)
async def test_an_unmappable_call_is_denied_whatever_the_verb(method, keystore, stores):
    """The property itself: a verified principal making a call odin cannot
    classify gets 403 AccessDenied, for EVERY verb a client can send.

    The `!= 405` assertion is stated separately from the `== 403` one so a
    regression reads as what it is. A bare 405 says "the router refused it", and
    that sentence -- not "the policy denied it" -- is the bug."""
    app, denials = _app_and_denials(keystore, stores)
    response = await _drive(app, _signed(keystore, method, "http://127.0.0.1/some/unmodelled/path"))

    assert response.status_code != 405, (
        f"{method} was refused by the ROUTER (405 Method Not Allowed), so odin's closed world "
        f"never ran: no signature check, no policy evaluation, no deny event. "
        f"Body: {response.text!r}"
    )
    assert response.status_code == 403, f"{method} answered {response.status_code}: {response.text!r}"
    assert "AccessDenied" in response.text
    assert denials == [(OPERATOR_NODE_ID, None, None, "unmappable-action")], (
        f"{method} produced denial events {denials!r}"
    )


def test_the_route_table_holds_no_verb_this_file_does_not_check(keystore, stores):
    """The REVERSE direction. The parametrized test above proves every verb this
    file names is denied properly; it cannot notice a verb somebody ADDS to the
    router later. This does, and it fails by name so the fix is obvious: add it
    to `_CLIENT_VERBS`."""
    app, _ = _app_and_denials(keystore, stores)
    unchecked = sorted(set(routed_methods(app)) - set(_CLIENT_VERBS))
    assert not unchecked, (
        f"the gateway's route table accepts {unchecked}, which no case in this file exercises -- "
        "add them to _CLIENT_VERBS so the closed world is proven for them too"
    )


async def test_a_signed_but_unknown_key_is_still_refused_on_the_new_verbs(stores, tmp_path):
    """The verbs are open to the PIPELINE, not open. A PATCH signed with a key
    the keystore never issued must still be refused -- otherwise "we route PATCH
    now" would have widened the door rather than moved it."""
    keystore = KeyStore(tmp_path)
    app, denials = _app_and_denials(keystore, stores)
    request = AWSRequest(method="PATCH", url="http://127.0.0.1/v2/apis/x", data=b"", headers={"host": "127.0.0.1"})
    SigV4Auth(Credentials("AKIDNOTISSUED0001", "nope"), UNKNOWN_SERVICE, "us-east-1").add_auth(request)

    response = await _drive(app, request)

    assert response.status_code == 401, response.text
    assert denials == [(None, None, None, "unknown-key")]
