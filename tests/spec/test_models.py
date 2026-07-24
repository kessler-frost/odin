"""S1.1 — Stack/World models round-trip and preserve field provenance."""
from __future__ import annotations

from odin.spec.models import (
    FieldValue,
    Ref,
    ResourceDesired,
    Stack,
    World,
    WorldDelta,
    is_sensitive_field_name,
    scrub,
)


def _sample_stack() -> Stack:
    return Stack(
        env="default",
        resources=(
            ResourceDesired(
                id="db",
                kind="rds",
                fields={
                    "engine": FieldValue(value="postgres", provenance="user"),
                    "port": FieldValue(value=5432, provenance="ai"),
                },
            ),
            ResourceDesired(
                id="api",
                kind="service",
                fields={"image": FieldValue(value="myapp:latest")},
                refs=(Ref(var="DATABASE_URL", target_id="db", target_attr="DATABASE_URL"),),
            ),
        ),
    )


def test_stack_round_trips_with_provenance():
    stack = _sample_stack()
    again = Stack.model_validate_json(stack.model_dump_json())
    assert again == stack
    db = next(r for r in again.resources if r.id == "db")
    assert db.fields["engine"].provenance == "user"
    assert db.fields["port"].provenance == "ai"


def test_ref_is_carried():
    stack = _sample_stack()
    api = next(r for r in stack.resources if r.id == "api")
    assert api.refs[0].target_id == "db"
    assert api.refs[0].var == "DATABASE_URL"


def test_world_get_and_delta_type():
    world = World(env="default")
    assert world.get("db") is None
    delta = WorldDelta(env="default", resource_id="db", kind="rds", phase="healthy")
    assert delta.type == "world_delta"


# --- security finding #3: FieldValue.sensitive + scrubbing ----------------


def test_field_value_sensitive_defaults_false():
    assert FieldValue(value="x").sensitive is False


def test_field_value_sensitive_round_trips():
    fv = FieldValue(value="s3cr3t", sensitive=True)
    again = FieldValue.model_validate_json(fv.model_dump_json())
    assert again.sensitive is True


def test_is_sensitive_field_name_matches_known_hints():
    assert is_sensitive_field_name("password")
    assert is_sensitive_field_name("db_password")
    assert is_sensitive_field_name("apiToken")
    assert is_sensitive_field_name("secret_key")
    assert is_sensitive_field_name("KEY_NAME")


def test_is_sensitive_field_name_rejects_ordinary_fields():
    assert not is_sensitive_field_name("region")
    assert not is_sensitive_field_name("bucket")
    assert not is_sensitive_field_name("image")
    assert not is_sensitive_field_name("cidr")


def test_scrub_replaces_every_occurrence():
    text = "user=admin password=hunter2, again hunter2"
    assert scrub(text, frozenset({"hunter2"})) == "user=admin password=[REDACTED], again [REDACTED]"


def test_scrub_with_no_secrets_is_a_no_op():
    text = "nothing sensitive here"
    assert scrub(text, frozenset()) == text


def test_resource_sensitive_values_collects_flagged_scalar_fields():
    res = ResourceDesired(
        id="db", kind="rds",
        fields={
            "password": FieldValue(value="hunter2", sensitive=True),
            "engine": FieldValue(value="postgres", sensitive=False),
        },
    )
    assert res.sensitive_values() == frozenset({"hunter2"})


def test_resource_sensitive_values_inspects_env_dict_key_by_key():
    # A dict-valued `env` field is scrubbed per-key, independent of whether
    # the field itself was marked sensitive -- one secret-looking entry must
    # not blanket-redact its non-secret siblings.
    res = ResourceDesired(
        id="api", kind="ecs",
        fields={
            "env": FieldValue(
                value={"DB_PASSWORD": "hunter2", "PORT": "8080", "LOG_LEVEL": "info"},
                sensitive=True,
            ),
        },
    )
    assert res.sensitive_values() == frozenset({"hunter2"})


def test_resource_sensitive_values_ignores_short_values():
    # Too short to safely scrub without mangling unrelated text.
    res = ResourceDesired(id="db", kind="rds", fields={"password": FieldValue(value="ab", sensitive=True)})
    assert res.sensitive_values() == frozenset()


def test_stack_sensitive_values_unions_all_resources():
    stack = Stack(resources=(
        ResourceDesired(id="db", kind="rds", fields={"password": FieldValue(value="hunter2", sensitive=True)}),
        ResourceDesired(id="cache", kind="rds", fields={"password": FieldValue(value="swordfish", sensitive=True)}),
    ))
    assert stack.sensitive_values() == frozenset({"hunter2", "swordfish"})
