"""An env name too long to make a valid Lima VM name -- derived, not guessed.

The trap (found while building the mesh proof): every EC2 node is a real Lima
VM named `odin-ec2-{env}-{instance_id}`, and `limactl` refuses a name whose
SSH control-socket path would not fit a unix socket address. Past a certain
env-name length EVERY ec2 boot fails, with a raw

    instance name "..." too long: ".../ssh.sock.1234567890123456" must be
    less than UNIX_PATH_MAX=104 characters, but is 107

that names nothing the user controls -- and only after they have waited for a
boot that was never going to work.

The limit is NOT a constant: it is `104` minus the length of `$LIMA_HOME`
(default `~/.lima`, so it varies with the username), minus the two separators,
minus Lima's socket filename, minus odin's own `odin-ec2-` prefix and the
19-char instance id. Every test below re-derives the boundary by CONSTRUCTING
the path Lima would build and measuring it, so the numbers can't drift from
Lima's own arithmetic by being restated.
"""
from __future__ import annotations

from pathlib import Path

from odin.compute.instances import (
    INSTANCE_ID_LEN,
    LIMA_SSH_SOCK,
    LIMA_UNIX_PATH_MAX,
    lima_home,
    max_env_name_len,
    vm_name,
)
from odin.gateway.models.ec2compute import _mint

_HOME = Path("/Users/somebody/.lima")


def _socket_path(home: Path, env: str) -> str:
    """The exact path Lima checks: `{LIMA_HOME}/{instance}/ssh.sock.<16>`,
    for the LONGEST VM name this env can mint."""
    return f"{home}/{vm_name(env, 'i' * INSTANCE_ID_LEN)}/{LIMA_SSH_SOCK}"


def test_the_instance_id_length_matches_what_ec2compute_actually_mints():
    """The derivation is only right if this constant tracks the real id
    shape (`i-` + 17 hex). Minted ids are random, so pin the LENGTH --
    `"i"` is the prefix `RunInstances` itself passes."""
    assert len(_mint("i")) == INSTANCE_ID_LEN


def test_the_derived_limit_is_the_largest_env_whose_socket_path_still_fits():
    limit = max_env_name_len(_HOME)
    assert len(_socket_path(_HOME, "e" * limit)) < LIMA_UNIX_PATH_MAX
    assert len(_socket_path(_HOME, "e" * (limit + 1))) >= LIMA_UNIX_PATH_MAX


def test_a_longer_lima_home_tightens_the_limit_by_exactly_that_much():
    """The reason this cannot be hardcoded: the same canvas is fine for one
    user and impossible for another, purely because of their home path."""
    longer = Path("/Users/somebody-with-a-long-name/.lima")
    assert max_env_name_len(longer) == max_env_name_len(_HOME) - (len(str(longer)) - len(str(_HOME)))


def test_the_limit_matches_the_value_measured_against_a_real_limactl():
    """The empirical anchor. Against limactl 1.x on a home of
    `/Users/fimbulwinter/.lima` (25 chars), a 55-char VM name was refused for
    a 107-byte path -- so 51 is the longest VM name, and 51 - 9 ("odin-ec2-")
    - 1 (separator) - 19 (instance id) = 22 the longest env. That is exactly
    the ~22 the mesh work hit by hand."""
    assert max_env_name_len(Path("/Users/fimbulwinter/.lima")) == 22


def test_lima_home_honours_the_environment_variable(monkeypatch):
    monkeypatch.setenv("LIMA_HOME", "/tmp/somewhere/lima")
    assert lima_home() == Path("/tmp/somewhere/lima")


def test_lima_home_defaults_under_the_users_home(monkeypatch):
    monkeypatch.delenv("LIMA_HOME", raising=False)
    assert lima_home() == Path.home() / ".lima"
