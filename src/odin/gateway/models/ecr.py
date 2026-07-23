"""The gateway's ECR control-plane model (task V2b), built to the captured
`aws_ecr_repository` surface in
docs/superpowers/research/research-coverage.md §2c (Create/Describe/Delete/
ListTagsForResource, JSON protocol, `X-Amz-Target:
AmazonEC2ContainerRegistry_V20150921.*`) and MiniStack's own ECR shape
(§2.6: "metadata only -- no image layers, that's registry:2's job").

Like ec2net/iamctl, ECR's CONTROL PLANE has no backing to forward to: this
module is the whole answer for every `ecr:*` action (CreateRepository,
DescribeRepositories, DeleteRepository, Tag*/ListTagsForResource,
GetAuthorizationToken). The DATA plane -- actual image bytes -- is a real
CNCF Distribution `registry:2` container (Apache-2.0) that joins
`aws.backings.BACKINGS` (kind `"ecr"`), booted/gc'd through the exact same
per-env lifecycle as rustfs/goaws/dynalite. The gateway does NOT proxy the
registry's v2 HTTP protocol in this slice (research: "control-plane only");
a real `docker push`/`pull` dials the registry's published port directly,
`repositoryUri` merely making that address discoverable.

`repositoryUri` DEVIATES from the brief's literal `host.docker.internal:
{port}/{name}` in favor of `127.0.0.1:{port}/{name}` -- verified empirically
(V2 implementation) and matching what research §2c's own evidence trail
actually used (`127.0.0.1:PORT/covres-app`, its "verified here" claim):
`docker push host.docker.internal:{port}/...` fails outright under Colima
(`server gave HTTP response to HTTPS client` -- Docker's daemon only
auto-trusts 127.0.0.0/8 as insecure-without-TLS; `host.docker.internal`
would need a per-port `insecure-registries` daemon.json entry, impossible
for a port that's freshly randomized every env). `host.docker.internal`
remains exactly right for facts published INTO other containers (Postgres/
goaws/rustfs all do this via `runtime.colima.CONTAINER_HOST`); ECR's
`repositoryUri` is instead consumed by a HOST-side `docker` CLI (this
slice's only real consumer), so `127.0.0.1` is the correct host for it.

`backing_port` (the registry's live published port) is threaded down from
`app.py`'s existing `GatewayState.backing_port` lookup -- through
`synth.pure_answer`'s optional last parameter -- rather than this module
reaching for a runtime driver itself; ecr.py stays as side-effect-free as
ec2net.py, just parameterized on the one live fact (a port number) it can't
derive from its own JSON sidecar.

Persistence: one `JsonStore` at `.odin/{env}/gateway/ecr.json`
(`stores.ecr`), flat keys `"repo:{name}"`. Tags share the shared
`stores.tags` store, keyed `"ecr:{repositoryArn}"` -- the same convention
ec2net/iamctl use for their own resource families.
"""
from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.gateway import errors
from odin.gateway.stores import SynthStores

_TOKEN_TTL_SECONDS = 12 * 60 * 60  # matches real ECR's GetAuthorizationToken lifetime


def _json(payload: dict) -> Response:
    return Response(json.dumps(payload), media_type="application/x-amz-json-1.0")


def _not_found(name: str) -> Response:
    return errors.synth_error("ecr", "RepositoryNotFoundException", f"The repository with name '{name}' does not exist", 400)


def _already_exists(name: str) -> Response:
    return errors.synth_error("ecr", "RepositoryAlreadyExistsException", f"The repository with name '{name}' already exists", 400)


def _key(name: str) -> str:
    return f"repo:{name}"


def _repo(stores: SynthStores, env: str, name: str) -> dict | None:
    return stores.ecr.get(env, _key(name))


def _repos(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.ecr.items(env).items() if k.startswith("repo:")]


def _tags_for(stores: SynthStores, env: str, arn: str) -> dict[str, str]:
    return stores.tags.get(env, f"ecr:{arn}", {})


def _set_tags(stores: SynthStores, env: str, arn: str, tags: dict[str, str]) -> None:
    stores.tags.set(env, f"ecr:{arn}", tags)


def _wire(repo: dict) -> dict:
    """The `repository` object exactly as the JSON response serializes it
    (verbatim field shape, verified against botocore's ecr model + research
    §2c's captured MiniStack bytes)."""
    return {
        "repositoryArn": repo["repository_arn"],
        "registryId": repo["registry_id"],
        "repositoryName": repo["repository_name"],
        "repositoryUri": repo["repository_uri"],
        "createdAt": repo["created_at"],
        "imageTagMutability": repo["image_tag_mutability"],
        "imageScanningConfiguration": {"scanOnPush": repo["scan_on_push"]},
        "encryptionConfiguration": {"encryptionType": "AES256"},
    }


def _create_repository(payload: dict, env: str, stores: SynthStores, backing_port: int | None) -> Response:
    name = payload.get("repositoryName", "")
    if _repo(stores, env, name) is not None:
        return _already_exists(name)
    port = backing_port or 0
    repo = {
        "repository_name": name,
        "registry_id": ACCOUNT,
        "repository_arn": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{name}",
        "repository_uri": f"127.0.0.1:{port}/{name}",
        "created_at": int(time.time()),
        "image_tag_mutability": payload.get("imageTagMutability", "MUTABLE"),
        "scan_on_push": bool((payload.get("imageScanningConfiguration") or {}).get("scanOnPush", False)),
    }
    stores.ecr.set(env, _key(name), repo)
    tags = {t["Key"]: t.get("Value", "") for t in payload.get("tags", [])}
    _set_tags(stores, env, repo["repository_arn"], tags)
    return _json({"repository": _wire(repo)})


def _describe_repositories(payload: dict, env: str, stores: SynthStores, backing_port: int | None) -> Response:
    names = payload.get("repositoryNames")
    repos = _repos(stores, env)
    if names:
        missing = [n for n in names if n not in {r["repository_name"] for r in repos}]
        if missing:
            return _not_found(missing[0])
        repos = [r for r in repos if r["repository_name"] in names]
    return _json({"repositories": [_wire(r) for r in repos], "nextToken": None})


def _delete_repository(payload: dict, env: str, stores: SynthStores, backing_port: int | None) -> Response:
    name = payload.get("repositoryName", "")
    repo = _repo(stores, env, name)
    if repo is None:
        return _not_found(name)
    stores.ecr.delete(env, _key(name))
    _set_tags(stores, env, repo["repository_arn"], {})
    return _json({"repository": _wire(repo)})


def _resource_repo(stores: SynthStores, env: str, arn: str) -> dict | None:
    name = arn.rsplit("/", 1)[-1]
    return _repo(stores, env, name)


def _list_tags_for_resource(payload: dict, env: str, stores: SynthStores, backing_port: int | None) -> Response:
    arn = payload.get("resourceArn", "")
    if _resource_repo(stores, env, arn) is None:
        return _not_found(arn)
    tags = _tags_for(stores, env, arn)
    return _json({"tags": [{"Key": k, "Value": v} for k, v in tags.items()]})


def _tag_resource(payload: dict, env: str, stores: SynthStores, backing_port: int | None) -> Response:
    arn = payload.get("resourceArn", "")
    if _resource_repo(stores, env, arn) is None:
        return _not_found(arn)
    new_tags = {t["Key"]: t.get("Value", "") for t in payload.get("tags", [])}
    _set_tags(stores, env, arn, {**_tags_for(stores, env, arn), **new_tags})
    return _json({})


def _untag_resource(payload: dict, env: str, stores: SynthStores, backing_port: int | None) -> Response:
    arn = payload.get("resourceArn", "")
    if _resource_repo(stores, env, arn) is None:
        return _not_found(arn)
    remove = set(payload.get("tagKeys", []))
    tags = {k: v for k, v in _tags_for(stores, env, arn).items() if k not in remove}
    _set_tags(stores, env, arn, tags)
    return _json({})


def _get_authorization_token(payload: dict, env: str, stores: SynthStores, backing_port: int | None) -> Response:
    """A synthetic docker-login-compatible token (research §2c: "registry:2
    can run auth-less locally", so the token's CONTENT is never checked --
    this exists purely so `GetAuthorizationToken` is protocol-answerable)."""
    token = base64.b64encode(b"AWS:odin").decode()
    port = backing_port or 0
    return _json({
        "authorizationData": [{
            "authorizationToken": token,
            "expiresAt": time.time() + _TOKEN_TTL_SECONDS,
            "proxyEndpoint": f"http://127.0.0.1:{port}",
        }],
    })


_Handler = Callable[[dict, str, SynthStores, int | None], Response]

_HANDLERS: dict[str, _Handler] = {
    "CreateRepository": _create_repository,
    "DescribeRepositories": _describe_repositories,
    "DeleteRepository": _delete_repository,
    "ListTagsForResource": _list_tags_for_resource,
    "TagResource": _tag_resource,
    "UntagResource": _untag_resource,
    "GetAuthorizationToken": _get_authorization_token,
}


def pure_answer(
    action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float,
    backing_port: int | None = None,
) -> Response | None:
    """The whole ECR control-plane answer -- same no-backing contract as
    ec2net/iamctl. `backing_port` is the registry:2 container's live
    published port (None only if it hasn't booted yet -- callers should
    `ensure_backing("ecr")` before Apply, same as every other AWS-shaped
    kind, so this is a defensive default, not the expected path)."""
    op = action.removeprefix("ecr:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return errors.synth_error("ecr", "InvalidAction", f"The action {op} is not valid.", 400)
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    return handler(payload, env, stores, backing_port)
