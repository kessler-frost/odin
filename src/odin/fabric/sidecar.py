"""Backing containers as REAL Nebula mesh members (W2.6 piece 2).

The gap: RDS/goaws/RustFS/dynalite run as host containers, so a drawn
`db-sg` was decorative -- DB access rode the raw published host port,
ungoverned, while SG enforcement existed only for EC2 VMs on the mesh.

## The join mechanism, and why this one

A `nebula` daemon runs in a COMPANION container that SHARES the backing
container's network namespace (`--network container:<backing>`), so the tun
device nebula creates lands in the BACKING's namespace. The upstream process
(unmodified `postgres:16-alpine`, `rustfs`, `goaws`, `dynalite`,
`registry:2`) already listens on `0.0.0.0`, so it starts answering on the
overlay address without knowing the mesh exists.

Alternatives considered and rejected:

- **nebula inside each backing image** -- would mean rebuilding five upstream
  images (and re-doing it on every upstream bump) plus an init system per
  image to run two processes. The sidecar needs ONE small image, built once
  per machine, and every backing image stays stock.
- **one per-env overlay gateway that L4-proxies to the backings** -- one
  container instead of N, but every backing in the env would share ONE
  overlay identity, so a per-backing SG could not gate anything (the whole
  point), and it needs exactly the same tun/NET_ADMIN as this does.
- **making the macOS HOST a data-plane mesh member** (one tun on the host,
  route everything through it) -- the only design that could REPLACE the raw
  host port rather than adding to it, and it is exactly the design R4
  rejected: a host tun device needs root, i.e. a sudoers grant. Off the
  table (owner rule: a root requirement is adoption poison).

## Privileges: a container capability, never host root

`nebula` needs `NET_ADMIN` + `/dev/net/tun` INSIDE the sidecar container.
That is a container capability granted by the container runtime, not a host
privilege: verified live on this dev machine's stock Colima
(`docker run --cap-add NET_ADMIN --device /dev/net/tun` yields a working
`/dev/net/tun`), with no `sudo`, no sudoers entry, and no change to the
user's host. The Colima VM's kernel owns that device; the user's Mac never
grants anything.

## Additive, not a replacement

The overlay is a SECOND path to the same listener. Every existing consumer
keeps working byte-for-byte: the published host port stays published (the
gateway forwards AWS calls to it, the reconciler probes it, tests use
host-side boto3/psycopg2 clients, and `${{node.VAR}}` facts keep publishing
host-reachable endpoints). What the mesh adds is a gated path -- reachable
only by mesh members whose certificate satisfies the backing's compiled SG
firewall. The honest limit that follows: the raw host port remains reachable
from the host itself and from any container that can dial it, and SGs do not
gate THAT path (see ROADMAP). Closing it would require the rejected
host-tun design.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from odin.compute.cloud_init import NEBULA_VERSION
from odin.fabric.models import FirewallRules
from odin.fabric.nebula import DEFAULT_FIREWALL, LighthouseManager, NebulaManager
from odin.runtime.colima import ContainerSpec
from odin.util import atomic_write_text

log = logging.getLogger("odin.fabric.sidecar")

# The sidecar image: the SAME nebula version the host lighthouse (brew) and
# the VM daemons (compute/cloud_init.py) run -- one wire protocol across every
# member of the mesh. Baked locally, once per machine, from the upstream
# release tarball (the same technique BackingAws uses for `odin-dynalite:1`):
# after the first build every boot is instant and fully offline.
NEBULA_IMAGE = f"odin-nebula:{NEBULA_VERSION}"
NEBULA_DOCKERFILE = f"""\
FROM alpine:3.20
RUN apk add --no-cache ca-certificates
RUN set -eux; \\
    arch="$(uname -m)"; \\
    case "$arch" in aarch64|arm64) a=arm64 ;; x86_64) a=amd64 ;; esac; \\
    wget -qO /tmp/nebula.tar.gz \\
      "https://github.com/slackhq/nebula/releases/download/v{NEBULA_VERSION}/nebula-linux-${{a}}.tar.gz"; \\
    tar -xzf /tmp/nebula.tar.gz -C /usr/local/bin nebula; \\
    rm /tmp/nebula.tar.gz; \\
    chmod +x /usr/local/bin/nebula
ENTRYPOINT ["/usr/local/bin/nebula", "-config", "/etc/nebula/config.yml"]
"""

# The macOS host as seen from INSIDE a container -- the numeric address
# `host.docker.internal` resolves to on Colima/Lima's user-mode network
# (verified live: `getent hosts host.docker.internal` -> 192.168.5.2, and the
# host lighthouse listens on 0.0.0.0:4242, so a container reaches it there).
# It cannot be discovered from inside the sidecar itself: `--add-host` is
# incompatible with `--network container:`, which is how the sidecar gets into
# the backing's namespace. `ODIN_MESH_UNDERLAY` overrides for a host whose
# user-mode gateway differs.
HOST_GATEWAY_IP = "192.168.5.2"

# Mesh membership for backings is OFF unless this env actually has a Nebula
# network (`ensure_network` ran, i.e. the canvas has a VPC) -- an env of bare
# s3/sqs nodes with no network drawn pays nothing. `ODIN_BACKING_MESH=0`
# disables it outright.
_DISABLE_ENV = "ODIN_BACKING_MESH"


def underlay_ip() -> str:
    return os.environ.get("ODIN_MESH_UNDERLAY") or HOST_GATEWAY_IP


def _shares_namespace(network_mode: str, target_id: str) -> bool:
    """`container:<id>` vs the target's live id. Compared by prefix in both
    directions because a container id is legitimately written short (12 hex)
    or full (64) depending on who recorded it."""
    joined = network_mode.partition("container:")[2].strip()
    return bool(joined) and (joined.startswith(target_id) or target_id.startswith(joined))


class MeshSidecar:
    """Joins one env's backing containers to that env's Nebula overlay.

    Constructed per (env, runtime) by whoever owns the backing containers
    (`aws/backings.py::BackingAws`, `aws/rds.py::PostgresRds`); every method
    is a no-op when the env has no Nebula network, so the AWS-substitute path
    is completely unchanged for a canvas with no VPC drawn.
    """

    def __init__(
        self, runtime, env: str, root: Path = Path(".odin"),
        lighthouse: LighthouseManager | None = None, runner=None,
    ) -> None:
        self._rt = runtime
        self._env = env
        self._root = Path(root)
        self._lighthouse = lighthouse or LighthouseManager()
        # The `nebula-cert` subprocess seam (same convention as
        # `NebulaManager`/`InstanceVm`): None means the real CLI.
        self._run = runner

    # ---- paths / naming ----
    def _nebula_dir(self) -> Path:
        return self._root / self._env / "nebula"

    def _member_dir(self, member: str) -> Path:
        return self._nebula_dir() / "members" / member

    def sidecar_name(self, target: str) -> str:
        """`<backing>-mesh` -- one sidecar per backing container, named off it
        so teardown/cleanup finds it by the same convention (`odin=1` label
        included, so the repo's blanket container cleanup sweeps it too)."""
        return f"{target}-mesh"

    # ---- state ----
    def enabled(self) -> bool:
        return os.environ.get(_DISABLE_ENV, "1") != "0" and (self._nebula_dir() / "ca.crt").exists()

    def overlay_ip(self, member: str) -> str | None:
        """This member's sticky overlay address, or None if it never joined.
        Read-only: no signing, no allocation, no mkdir (so facts/status reads
        stay side-effect free)."""
        overlay = NebulaManager(self._nebula_dir()).load_overlay()
        hosts = overlay.subnets.get("hosts") if overlay else None
        return hosts.assignments.get(member) if hosts else None

    def running(self, target: str) -> bool:
        return self._rt.status(self.sidecar_name(target)) == "running"

    def attached_to(self, target: str) -> bool | None:
        """Is the sidecar in the CURRENT `target`'s network namespace?

        True/False, or None for "no evidence either way" (the runtime can't
        report an id for `target` -- it was never started, or docker itself
        hiccuped; churning a daemon on no evidence would be a self-inflicted
        restart loop).

        This is the field-test HIGH-2 check. `--network container:<name>` is
        resolved to an ID at creation, so a target that was killed and
        re-created (a `docker kill`ed Postgres + the Apply that brings it
        back) leaves the sidecar pinned to a namespace that no longer exists:
        nebula keeps running, logging `sendto: network is unreachable` on
        every handshake, while the config file is byte-identical and the
        sidecar container is still `running` -- so "unchanged + running" said
        "nothing to do" forever and no Apply could heal it."""
        target_id = self._rt.container_id(target)
        if not target_id:
            return None
        return _shares_namespace(self._rt.network_mode(self.sidecar_name(target)), target_id)

    # ---- join / leave ----
    def ensure(
        self, target: str, member: str, *,
        groups: tuple[str, ...] = (), firewall: FirewallRules | None = None,
    ) -> str | None:
        """Put `target` (a running backing container) on the mesh as `member`,
        gated by `firewall` (the drawn SG's compiled rules; `None` means
        allow-all, matching how a VM with no compiled SG behaves). Returns the
        overlay IP, or None when the env has no mesh.

        Idempotent by config AND by namespace: an already-running sidecar
        whose config file is byte-identical AND which is in the CURRENT
        target's network namespace is left alone; a CHANGED firewall (the
        canvas edited the SG) replaces it, since nebula reads its firewall
        only at start, and so does a REPLACED target (see `attached_to` --
        the field-test HIGH-2 bug: without that half, a killed-and-recreated
        database was never re-joined by any number of Applies).
        Never raises -- like `InstanceVm._activate_nebula`, mesh wiring must
        not fail an otherwise-healthy backing (the host path still works)."""
        if not self.enabled():
            return None
        try:
            return self._join(target, member, groups, firewall or DEFAULT_FIREWALL)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
            log.warning("mesh join failed for %s (env %r): %s", target, self._env, exc)
            return None

    def _join(self, target: str, member: str, groups: tuple[str, ...], firewall: FirewallRules) -> str | None:
        underlay = underlay_ip()
        # The env's lighthouse may not be up yet: backings can be the FIRST
        # mesh members (no EC2 instance drawn at all). Idempotent -- a
        # lighthouse an instance already started is left alone.
        self._lighthouse.ensure_started(self._root, self._env, underlay)
        manager = NebulaManager(self._nebula_dir(), runner=self._run)
        overlay = manager.load_overlay()
        if overlay is None:
            return None
        cert_ip = manager.allocate_host_ip(member)  # sticky: same IP every join
        # Signed ONCE per member (this runs on every ensure_backing / every
        # reconciler tick for a live rds -- a `nebula-cert` subprocess each
        # time would be pure waste). A member's groups are fixed at first
        # join; changing them means revoking the cert, which nothing does yet.
        cert = manager.cert_paths(member)
        if not cert.crt.exists():
            cert = manager.sign_cert(member, cert_ip, groups=["backing", *groups])
        config = manager.generate_config(
            lighthouse_ip=overlay.lighthouse_ip, lighthouse_underlay=underlay,
            firewall=firewall, is_lighthouse=False, relay_enabled=True,
        )
        directory = self._member_dir(member)
        directory.mkdir(parents=True, exist_ok=True)
        for name, text in (
            ("ca.crt", cert.ca_crt.read_text()),
            ("host.crt", cert.crt.read_text()),
            ("host.key", cert.key.read_text()),
        ):
            atomic_write_text(directory / name, text)
        config_path = directory / "config.yml"
        unchanged = config_path.exists() and config_path.read_text() == config
        atomic_write_text(config_path, config)
        # `attached_to(...) is not False`: a definite NO (the target was
        # replaced) is the one case that must re-join; None (no evidence) keeps
        # the no-churn contract -- see `attached_to`.
        if unchanged and self.running(target) and self.attached_to(target) is not False:
            return cert_ip.split("/")[0]
        self._start(target, directory)
        return cert_ip.split("/")[0]

    def _start(self, target: str, config_dir: Path) -> None:
        name = self.sidecar_name(target)
        self._rt.stop(name)  # clear a remnant / an old config's daemon
        if not self._rt.image_exists(NEBULA_IMAGE):
            self._rt.build(NEBULA_IMAGE, NEBULA_DOCKERFILE)
        self._rt.run_container(ContainerSpec(
            name=name, image=NEBULA_IMAGE, labels={"odin-env": self._env},
            # The whole mechanism: the sidecar has NO network of its own -- it
            # lives in `target`'s namespace, so the tun it creates is the
            # backing's. `config_dir` must sit under $HOME (Colima only shares
            # that tree into its VM) -- it does: `.odin/{env}/nebula/members/`.
            network=f"container:{target}",
            cap_add=("NET_ADMIN",), devices=("/dev/net/tun",),
            volumes={str(config_dir.resolve()): "/etc/nebula"},
        ))
        log.info("joined %s to the %r mesh (sidecar %s)", target, self._env, name)

    def stop(self, target: str) -> None:
        """Take `target` off the mesh. Idempotent, and safe to call for a
        container that never joined (`stop` on an absent name is a no-op)."""
        self._rt.stop(self.sidecar_name(target))
