"""gateway/models/kmsctl.py -- the KMS control and data planes.

Same test method as logsctl/secretsctl/ssmctl: every request is a REAL
boto3-signed capture (tests/gateway/harness.py's CaptureSink + the `kms` client
fixture) and every response round-trips through botocore's OWN parser for the
REAL KMS service model -- proof the wire bytes are real-AWS-shaped, not
string-matched. Every call ALSO routes through classify() ->
await synth.pure_answer(), exercising the `kms` branch of the dispatch pipeline
end to end.

The WHY of this model is in kmsctl.py's own docstring (short version: the key
encrypts odin's secrets/SSM sidecars, which is the one thing odin really holds;
it does NOT claim to encrypt S3/RDS/DynamoDB, none of whose substrates encrypt
anything). The proof that it does so is `test_kms_at_rest.py` -- this file is
about the API being real.
"""
from __future__ import annotations

import json
from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.gateway import synth
from odin.gateway.classify import classify
from odin.gateway.models import kmsctl
from odin.gateway.stores import SynthStores

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "default"
LABEL = "app-key"


def _parse(operation: str, response: Response, *, error: bool = False, service: str = "kms"):
    model = _SESSION.get_service_model(service)
    operation_model = model.operation_model(operation)
    parser = create_parser(model.protocol)
    raw = {"status_code": response.status_code, "headers": dict(response.headers), "body": response.body}
    parsed = parser.parse(raw, operation_model.output_shape)
    if error:
        assert response.status_code >= 300
    return parsed


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


async def _answer(stores: SynthStores, req, service: str = "kms") -> Response:
    """One request through the REAL pipeline. `service` is here because two
    tests below reach through Secrets Manager to prove a KMS property (a key
    kept across a re-apply is only observable as a secret that still reads
    back), and classify() must be told which wire it is looking at."""
    path, query = split_url(req.url)
    classified = classify(service, req.method, path, query, req.headers, req.body)
    assert classified is not None, f"a {service} request must never be unmappable"
    action, resource = classified
    response = await synth.pure_answer(action, resource, ENV, req.body, stores, 0.0)
    assert response is not None, f"{service} is all-synth: pure_answer must never fall through"
    return response


async def _create(stores, sink, kms, label=LABEL, **kwargs) -> Response:
    return await _answer(stores, sink.call(lambda: kms.create_key(
        Tags=[{"TagKey": kmsctl.NODE_TAG, "TagValue": label}], **kwargs,
    )))


# --- control plane ----------------------------------------------------------


async def test_create_key_takes_its_id_from_the_odin_node_tag(stores, sink, kms):
    """Deviation 1, and the load-bearing one: real `CreateKey` carries no name,
    so the canvas label rides in on the tag `iac/hcl.py` stamps. Without
    this, every key is a uuid and no IAM edge or `kms_key_id` reference can
    ever name one."""
    parsed = _parse("CreateKey", await _create(stores, sink, kms, Description="the app key"))

    assert parsed["KeyMetadata"]["KeyId"] == LABEL
    assert parsed["KeyMetadata"]["Arn"] == kmsctl.key_arn(LABEL)
    assert parsed["KeyMetadata"]["Description"] == "the app key"
    assert parsed["KeyMetadata"]["Enabled"] is True


async def test_kms_tags_use_TagKey_not_Key(stores, sink, kms):
    """KMS spells a tag `{"TagKey":..,"TagValue":..}` while every other service
    modeled here uses `{"Key":..,"Value":..}` -- both read off botocore's own
    models, not memory. Reading the common spelling would make every CreateKey
    fall through to a uuid, and the failure would be invisible until someone
    drew an edge. This is the `ResourceARN` trap `_events_resource` records,
    one service over."""
    await _create(stores, sink, kms)
    parsed = _parse("ListResourceTags", await _answer(
        stores, sink.call(lambda: kms.list_resource_tags(KeyId=LABEL))))

    assert parsed["Tags"] == [{"TagKey": kmsctl.NODE_TAG, "TagValue": LABEL}]


@pytest.mark.parametrize("label", ["alias/prod-key", f"arn:aws:kms:us-east-1:000000000000:key/{LABEL}"])
async def test_a_node_tag_that_would_not_survive_lookup_is_REFUSED(stores, sink, kms, label):
    """CREATE and LOOKUP must key by the same string. This line stored the RAW
    tag while `DescribeKey`/`Encrypt`/`Decrypt`/`ScheduleKeyDeletion` all
    resolve through `bare_key_id` -- identical only while `bare_key_id` is the
    identity, which it is not for an alias or an ARN. MEASURED before the fix:

        CreateKey   -> 200, KeyId 'alias/prod-key'
        DescribeKey -> 400 NotFoundException "Key 'prod-key' does not exist"

    A green create for a key dead on arrival, and a secret naming it then fails
    quoting an id the user never typed.

    REFUSED rather than normalised, deliberately: rewriting it to `prod-key`
    would make create and lookup agree and move the defect one layer out -- the
    canvas would show `alias/prod-key`, an IAM edge would emit
    `.../key/alias/prod-key`, and the grant would deny silently. `iac/hcl.py`
    declines the LABEL on the canvas, but a direct SDK call and `odin
    import-tf` both arrive here without passing it."""
    response = await _answer(stores, sink.call(lambda: kms.create_key(
        Tags=[{"TagKey": kmsctl.NODE_TAG, "TagValue": label}])))

    parsed = _parse("CreateKey", response, error=True)
    assert parsed["Error"]["Code"] == "TagException"
    assert label in parsed["Error"]["Message"]
    assert stores.kmsctl.items(ENV) == {}, "a refused CreateKey left a record behind"
    assert stores.kms.key_ids(ENV) == [], "a refused CreateKey minted key material"


async def test_a_label_that_merely_CONTAINS_a_slash_is_still_fine(stores, sink, kms):
    """The guard is `bare_key_id(x) != x`, not a substring test for `alias/`.
    `my-alias/key` survives `bare_key_id` unchanged, so it is a perfectly good
    key id and refusing it would be its own wrong answer -- declining a name
    that works."""
    parsed = _parse("CreateKey", await _create(stores, sink, kms, label="my-alias/key"))

    assert parsed["KeyMetadata"]["KeyId"] == "my-alias/key"
    describe = _parse("DescribeKey", await _answer(
        stores, sink.call(lambda: kms.describe_key(KeyId="my-alias/key"))))
    assert describe["KeyMetadata"]["KeyId"] == "my-alias/key"


async def test_create_key_without_the_tag_still_works_and_is_not_addressable(stores, sink, kms):
    """A raw client that sends no `odin:node` tag gets a uuid, exactly as real
    AWS would. It is simply not reachable from a canvas edge -- stated rather
    than treated as an error, because it is a legitimate API call."""
    parsed = _parse("CreateKey", await _answer(stores, sink.call(lambda: kms.create_key())))

    key_id = parsed["KeyMetadata"]["KeyId"]
    assert key_id != LABEL
    assert len(key_id) == 32


async def test_describe_key_accepts_every_id_form(stores, sink, kms):
    await _create(stores, sink, kms)
    for key_id in (LABEL, kmsctl.key_arn(LABEL), f"alias/{LABEL}"):
        parsed = _parse("DescribeKey", await _answer(
            stores, sink.call(lambda: kms.describe_key(KeyId=key_id))))
        assert parsed["KeyMetadata"]["KeyId"] == LABEL


async def test_describe_an_unknown_key_is_not_found(stores, sink, kms):
    parsed = _parse("DescribeKey", await _answer(
        stores, sink.call(lambda: kms.describe_key(KeyId="ghost"))), error=True)

    assert parsed["Error"]["Code"] == "NotFoundException"
    assert "ghost" in parsed["Error"]["Message"]


async def test_list_keys_reports_what_was_created(stores, sink, kms):
    await _create(stores, sink, kms)
    await _create(stores, sink, kms, label="other-key")
    parsed = _parse("ListKeys", await _answer(stores, sink.call(lambda: kms.list_keys())))

    assert [k["KeyId"] for k in parsed["Keys"]] == [LABEL, "other-key"]
    assert parsed["Truncated"] is False


async def test_the_env_default_key_is_a_real_listable_key(stores, sink, kms, secretsmanager):
    """It is created implicitly (the first unkeyed secret mints it), and that
    is exactly why it must be VISIBLE: a key odin uses and never reports would
    be a piece of the trust chain the user cannot inspect or delete."""
    await _answer(stores, sink.call(lambda: secretsmanager.create_secret(
        Name="s", SecretString="v")), "secretsmanager")
    parsed = _parse("ListKeys", await _answer(stores, sink.call(lambda: kms.list_keys())))

    assert [k["KeyId"] for k in parsed["Keys"]] == [kmsctl.DEFAULT_KEY_ID]


async def test_key_rotation_round_trips_as_a_flag(stores, sink, kms):
    """Deviation 3: `EnableKeyRotation` stores true so terraform's
    `enable_key_rotation` does not drift, and NOTHING RE-KEYS. Asserted rather
    than implied, so the limit is visible where the behaviour is."""
    await _create(stores, sink, kms)
    before = _parse("GetKeyRotationStatus", await _answer(
        stores, sink.call(lambda: kms.get_key_rotation_status(KeyId=LABEL))))
    assert before["KeyRotationEnabled"] is False

    await _answer(stores, sink.call(lambda: kms.enable_key_rotation(KeyId=LABEL)))
    after = _parse("GetKeyRotationStatus", await _answer(
        stores, sink.call(lambda: kms.get_key_rotation_status(KeyId=LABEL))))

    assert after["KeyRotationEnabled"] is True
    # The material is IDENTICAL: a rotation odin pretended to perform would be
    # a claim it had replaced something it had not.
    assert stores.kms.key_ids(ENV) == [LABEL]


async def test_get_key_policy_answers_a_document_even_when_none_was_put(stores, sink, kms):
    """The provider's read path calls this on every refresh; an unmodeled-action
    error there fails the whole plan."""
    await _create(stores, sink, kms)
    parsed = _parse("GetKeyPolicy", await _answer(
        stores, sink.call(lambda: kms.get_key_policy(KeyId=LABEL))))

    assert json.loads(parsed["Policy"])["Version"] == "2012-10-17"


async def test_put_key_policy_round_trips(stores, sink, kms):
    document = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow"}]})
    await _create(stores, sink, kms)
    await _answer(stores, sink.call(lambda: kms.put_key_policy(KeyId=LABEL, Policy=document)))
    parsed = _parse("GetKeyPolicy", await _answer(
        stores, sink.call(lambda: kms.get_key_policy(KeyId=LABEL))))

    assert parsed["Policy"] == document


async def test_disable_key_shows_in_describe(stores, sink, kms):
    await _create(stores, sink, kms)
    await _answer(stores, sink.call(lambda: kms.disable_key(KeyId=LABEL)))
    parsed = _parse("DescribeKey", await _answer(
        stores, sink.call(lambda: kms.describe_key(KeyId=LABEL))))

    assert parsed["KeyMetadata"]["Enabled"] is False
    assert parsed["KeyMetadata"]["KeyState"] == "Disabled"


async def test_schedule_key_deletion_is_immediate_and_removes_the_material(stores, sink, kms):
    """Deviation 2. `PendingWindowInDays` is accepted and IGNORED, and the
    material is gone when the call returns -- so a re-Apply after a teardown
    never wedges on a key scheduled for deletion, the same reason
    `DeleteSecret` is immediate."""
    await _create(stores, sink, kms)
    assert stores.kms.exists(ENV, LABEL)

    parsed = _parse("ScheduleKeyDeletion", await _answer(
        stores, sink.call(lambda: kms.schedule_key_deletion(KeyId=LABEL, PendingWindowInDays=30))))

    assert parsed["KeyId"] == LABEL
    assert not stores.kms.exists(ENV, LABEL)
    describe = _parse("DescribeKey", await _answer(
        stores, sink.call(lambda: kms.describe_key(KeyId=LABEL))), error=True)
    assert describe["Error"]["Code"] == "NotFoundException"


async def test_recreating_a_key_with_the_same_label_keeps_its_material(stores, sink, kms, secretsmanager):
    """Re-applying a canvas must not orphan ciphertext written by the previous
    apply. `ensure_key` is idempotent for exactly this."""
    await _create(stores, sink, kms)
    await _answer(stores, sink.call(lambda: secretsmanager.create_secret(
        Name="s", SecretString="v", KmsKeyId=LABEL)), "secretsmanager")
    material_before = (stores.root / ENV / "kms.json").read_text()

    await _create(stores, sink, kms, Description="edited")

    assert (stores.root / ENV / "kms.json").read_text() == material_before
    parsed = _parse("GetSecretValue", await _answer(
        stores, sink.call(lambda: secretsmanager.get_secret_value(SecretId="s")), "secretsmanager"),
        service="secretsmanager")
    assert parsed["SecretString"] == "v"


# --- data plane -------------------------------------------------------------


async def test_encrypt_then_decrypt_round_trips(stores, sink, kms):
    """The three data-plane ops are REAL, on the same material the sidecars
    use, which is what makes `kms:Encrypt`/`kms:Decrypt`/`kms:GenerateDataKey`
    the only permissions the catalog offers on a kms node -- the `ecr`
    precedent: a tickable action no handler answers is decorative."""
    await _create(stores, sink, kms)
    encrypted = _parse("Encrypt", await _answer(
        stores, sink.call(lambda: kms.encrypt(KeyId=LABEL, Plaintext=b"hello world"))))

    assert encrypted["KeyId"] == kmsctl.key_arn(LABEL)
    decrypted = _parse("Decrypt", await _answer(stores, sink.call(
        lambda: kms.decrypt(CiphertextBlob=encrypted["CiphertextBlob"]))))

    assert decrypted["Plaintext"] == b"hello world"


async def test_generate_data_key_returns_a_matching_pair(stores, sink, kms):
    await _create(stores, sink, kms)
    parsed = _parse("GenerateDataKey", await _answer(
        stores, sink.call(lambda: kms.generate_data_key(KeyId=LABEL, KeySpec="AES_256"))))

    decrypted = _parse("Decrypt", await _answer(stores, sink.call(
        lambda: kms.decrypt(CiphertextBlob=parsed["CiphertextBlob"]))))
    assert decrypted["Plaintext"] == parsed["Plaintext"]


async def test_decrypt_after_the_key_is_gone_names_the_key(stores, sink, kms):
    await _create(stores, sink, kms)
    encrypted = _parse("Encrypt", await _answer(
        stores, sink.call(lambda: kms.encrypt(KeyId=LABEL, Plaintext=b"x"))))
    await _answer(stores, sink.call(lambda: kms.schedule_key_deletion(KeyId=LABEL)))

    parsed = _parse("Decrypt", await _answer(stores, sink.call(
        lambda: kms.decrypt(CiphertextBlob=encrypted["CiphertextBlob"]))), error=True)

    assert parsed["Error"]["Code"] == "InvalidCiphertextException"
    assert LABEL in parsed["Error"]["Message"]


# --- dispatch contract ------------------------------------------------------


async def test_an_unmodeled_action_is_a_protocol_error_never_a_silent_success(stores, sink, kms):
    """Aliases are the likeliest one a user reaches for. `InvalidAction` says
    so; a 200 would leave them believing an alias exists."""
    await _create(stores, sink, kms)
    parsed = _parse("CreateAlias", await _answer(stores, sink.call(
        lambda: kms.create_alias(AliasName="alias/app", TargetKeyId=LABEL))), error=True)

    assert parsed["Error"]["Code"] == "InvalidAction"
    assert "CreateAlias" in parsed["Error"]["Message"]


async def test_a_missing_key_id_says_so_instead_of_blaming_an_empty_name(stores, sink, kms):
    """secretsctl's `_REQUIRED` lesson: nine handlers there answered
    "can't find the specified secret: " -- blaming a name the caller never
    sent. A raw HTTP client is the only way to reach this (botocore refuses to
    send the request at all), so it is built by hand."""
    response = await synth.pure_answer(
        "kms:DescribeKey", "*", ENV, b"{}", stores, 0.0,
    )

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["__type"] == "ValidationException"
    assert body["message"] == "KeyId is required"


# --- the cross-module lock-step --------------------------------------------


def test_classify_and_the_model_agree_on_every_key_id_form():
    """`classify.py::_kms_resource` and `kmsctl.bare_key_id` must reduce a
    KeyId identically: classify's answer is what an IAM statement is matched
    against, and the model's is what the store is keyed by. If they disagreed,
    an edge drawn to a kms node would authorize one string and the record would
    live under another -- a grant that draws, applies, and gates nothing.

    Pinned here rather than asserted in a comment, because the two live in
    different modules and a lock-step kept only in prose is the thing this
    repo's honesty rule 1 is about."""
    from odin.gateway.classify import _kms_resource

    for form in (
        LABEL,
        kmsctl.key_arn(LABEL),
        f"alias/{LABEL}",
        "arn:aws:kms:us-east-1:000000000000:key/1234abcd",
        "1234abcd",
    ):
        assert _kms_resource({"KeyId": form}) == kmsctl.bare_key_id(form), form


def test_classify_resolves_create_key_through_the_odin_node_tag():
    """CreateKey carries no KeyId, so its resource comes from the tag -- and it
    must be the KMS spelling. Reading `Key`/`Value` here would classify every
    CreateKey to `"*"`, which the operator's wildcard still allows, so nothing
    would break until an iam edge existed."""
    from odin.gateway.classify import _kms_resource

    tagged = {"Tags": [{"TagKey": kmsctl.NODE_TAG, "TagValue": LABEL}]}
    assert _kms_resource(tagged) == LABEL
    assert _kms_resource({"Tags": [{"Key": kmsctl.NODE_TAG, "Value": LABEL}]}) == "*"
    assert _kms_resource({}) == "*"


def test_every_kms_action_the_catalog_offers_has_a_handler():
    """The `ecr` defect, pre-empted for kms. `catalog.ts` offers `kms:*`
    permissions; a permission whose op `_HANDLERS` does not answer is
    DECORATIVE -- it draws, it applies, and the gateway replies
    `InvalidAction`. `tests/gateway/test_ecr_vocabulary_has_handlers.py` exists
    because that shipped once already."""
    import re

    repo = Path(__file__).resolve().parents[2]
    catalog = (repo / "ui" / "src" / "lib" / "catalog.ts").read_text()
    offered = set(re.findall(r"'kms:([A-Za-z*]+)'", catalog))
    assert offered, "catalog.ts offers no kms actions -- the extraction is wrong, not the catalog"

    unanswerable = sorted(op for op in offered if op != "*" and op not in kmsctl._HANDLERS)
    assert unanswerable == [], (
        f"catalog.ts offers kms actions the gateway answers with InvalidAction: {unanswerable}. "
        "Either add a handler in gateway/models/kmsctl.py or stop offering the permission."
    )
