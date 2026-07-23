"""G1 -- KeyStore: stable per-(env, node) credential issuance + persistence."""
from __future__ import annotations

import stat
from pathlib import Path

from odin.gateway.keys import KeyStore, Principal, workload_env


def test_issue_returns_ak_sk_with_expected_shapes(tmp_path: Path):
    store = KeyStore(tmp_path)
    access_key, secret_key = store.issue("default", "api")

    assert access_key.startswith("AKODIN")
    assert len(access_key) == len("AKODIN") + 14
    assert len(secret_key) == 40
    urlsafe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    assert set(access_key[len("AKODIN"):]) <= urlsafe
    assert set(secret_key) <= urlsafe


def test_issue_is_stable_across_repeated_calls(tmp_path: Path):
    store = KeyStore(tmp_path)
    first = store.issue("default", "api")
    second = store.issue("default", "api")
    assert first == second


def test_issue_differs_per_node(tmp_path: Path):
    store = KeyStore(tmp_path)
    api_keys = store.issue("default", "api")
    db_keys = store.issue("default", "db")
    assert api_keys != db_keys


def test_issue_differs_per_env(tmp_path: Path):
    store = KeyStore(tmp_path)
    a_keys = store.issue("env-a", "api")
    b_keys = store.issue("env-b", "api")
    assert a_keys != b_keys


def test_lookup_resolves_principal(tmp_path: Path):
    store = KeyStore(tmp_path)
    access_key, _ = store.issue("default", "api")
    assert store.lookup(access_key) == Principal(env="default", node_id="api")


def test_lookup_unknown_key_returns_none(tmp_path: Path):
    store = KeyStore(tmp_path)
    store.issue("default", "api")
    assert store.lookup("AKODINnotarealkey0000") is None


def test_revoke_env_clears_lookup(tmp_path: Path):
    store = KeyStore(tmp_path)
    access_key, _ = store.issue("default", "api")
    store.revoke_env("default")
    assert store.lookup(access_key) is None


def test_revoke_env_allows_reissue_of_new_pair(tmp_path: Path):
    store = KeyStore(tmp_path)
    before = store.issue("default", "api")
    store.revoke_env("default")
    after = store.issue("default", "api")
    assert before != after


def test_revoke_env_does_not_affect_other_envs(tmp_path: Path):
    store = KeyStore(tmp_path)
    a_before = store.issue("env-a", "api")
    store.issue("env-b", "api")
    store.revoke_env("env-b")
    assert store.issue("env-a", "api") == a_before


def test_persists_to_keys_json_file(tmp_path: Path):
    store = KeyStore(tmp_path)
    store.issue("default", "api")
    keys_file = tmp_path / "default" / "keys.json"
    assert keys_file.exists()
    assert "api" in keys_file.read_text()


def test_persisted_keys_file_is_0600(tmp_path: Path):
    # Release finding #2: real access/secret key pairs -- never briefly
    # world-readable, and never left world-readable.
    store = KeyStore(tmp_path)
    store.issue("default", "api")
    keys_file = tmp_path / "default" / "keys.json"
    assert stat.S_IMODE(keys_file.stat().st_mode) == 0o600


def test_reload_from_disk_preserves_stability_via_issue(tmp_path: Path):
    first_store = KeyStore(tmp_path)
    before = first_store.issue("default", "api")

    second_store = KeyStore(tmp_path)
    after = second_store.issue("default", "api")
    assert before == after


# --- workload_env (fix-wave 2b finding #2): the 4 env vars every workload
# substrate (EC2 cloud-init, an ECS task container, a Lambda RIE container)
# gets injected with, so it can call the gateway AS ITSELF. ------------------


def test_workload_env_has_the_four_expected_keys(tmp_path: Path):
    store = KeyStore(tmp_path)
    env_vars = workload_env(store, "default", "server", 4266)
    assert set(env_vars) == {
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL", "AWS_DEFAULT_REGION",
    }


def test_workload_env_endpoint_uses_container_reachable_host_and_the_given_port(tmp_path: Path):
    store = KeyStore(tmp_path)
    env_vars = workload_env(store, "default", "server", 4266)
    assert env_vars["AWS_ENDPOINT_URL"] == "http://host.docker.internal:4266"


def test_workload_env_issues_stable_keystore_creds_for_env_and_label(tmp_path: Path):
    store = KeyStore(tmp_path)
    env_vars = workload_env(store, "default", "server", 4266)
    access_key, secret_key = store.issue("default", "server")
    assert env_vars["AWS_ACCESS_KEY_ID"] == access_key
    assert env_vars["AWS_SECRET_ACCESS_KEY"] == secret_key


def test_workload_env_differs_per_node_label(tmp_path: Path):
    store = KeyStore(tmp_path)
    server_env = workload_env(store, "default", "server", 4266)
    worker_env = workload_env(store, "default", "worker", 4266)
    assert server_env["AWS_ACCESS_KEY_ID"] != worker_env["AWS_ACCESS_KEY_ID"]


def test_reload_lookup_without_prior_issue_call(tmp_path: Path):
    """A fresh KeyStore (e.g. after a server restart) must resolve a key
    that was persisted by a previous instance, even if lookup() is the
    very first call made against it (no issue() call primes the cache)."""
    first_store = KeyStore(tmp_path)
    access_key, _ = first_store.issue("default", "api")

    second_store = KeyStore(tmp_path)
    assert second_store.lookup(access_key) == Principal(env="default", node_id="api")
