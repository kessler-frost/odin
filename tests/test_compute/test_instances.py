"""V3b -- compute/instances.py: InstanceVm, the Lima VM substrate binding for
gateway/models/ec2compute.py's EC2 instances.

Unit-level (injected subprocess), the same method tests/runtime/test_lima.py
uses for LimaRuntime -- deterministic, no real `limactl`/`nebula-cert`
involved. Nebula-cert calls use tests/fabric/test_nebula.py's own
file-writing FakeRunner trick (a real `sign_cert`/`create_ca` writes files at
`-out-crt`/`-out-key`, and `InstanceVm._nebula_files` reads them back), so
`sign_cert`'s real file I/O still round-trips against a fake CLI.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from odin.compute import instances
from odin.compute.instances import (
    InstanceVm,
    NebulaJoin,
    _BOOT_SEMAPHORE,
    _default_max_concurrent_boots,
    _Proc,
    _pick_shared_ip,
    instance_config_path,
    membership_changed,
    vm_name,
)
from odin.compute.models import get_instance_type
from odin.fabric.models import FirewallRule, FirewallRules
from odin.fabric.nebula import FIREWALL_REVISION_KEY, LIGHTHOUSE_PORT, NebulaManager
from odin.runtime import lima
from odin.runtime.colima import _failure_reason as canonical_failure_reason

NAME = vm_name("default", "i-0123456789abcdef0")


@pytest.fixture(autouse=True)
def pinned_lighthouse_port(monkeypatch):
    """Port allocation PROBES the real machine (`fabric/nebula.py::_port_free`
    binds a UDP socket), so a rendered `static_host_map` asserted against a
    literal 4342 fails whenever any live env on this Mac happens to hold that
    port -- a unit test failing on the state of somebody else's running
    server. `ODIN_LIGHTHOUSE_PORT` is the seam that already exists for
    exactly this ("honoured verbatim: no probing, no reallocation"), so
    pinning it makes these assertions about the RENDERER again."""
    monkeypatch.setenv("ODIN_LIGHTHOUSE_PORT", str(LIGHTHOUSE_PORT))


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.responses: dict[str, _Proc] = {}
        self.hostname_i_queue: list[str] = []

    async def __call__(self, args: list[str], input: str | None = None) -> _Proc:
        self.calls.append(args)
        # nebula-cert writes -out-crt/-out-key files for real -- create them
        # so InstanceVm._nebula_files' later read_text() calls succeed.
        for flag in ("-out-crt", "-out-key"):
            if flag in args:
                Path(args[args.index(flag) + 1]).write_text(f"FAKE {flag}")
        if args[-2:] == ["hostname", "-I"]:
            if self.hostname_i_queue:
                return _Proc(0, self.hostname_i_queue.pop(0))
            return self.responses.get("hostname -I", _Proc(0, ""))
        joined = " ".join(args)
        for key, resp in self.responses.items():
            if key in joined:
                return resp
        return _Proc(0, "")


def _yaml_path_from_create_call(runner: FakeRunner) -> Path:
    create_call = next(c for c in runner.calls if "create" in c)
    return Path(create_call[-1])


# --- _pick_shared_ip (pure) ---------------------------------------------------


def test_pick_shared_ip_excludes_loopback_and_slirp():
    assert _pick_shared_ip("127.0.0.1 192.168.5.15 192.168.64.12") == "192.168.64.12"


def test_pick_shared_ip_none_when_only_slirp_or_loopback():
    assert _pick_shared_ip("127.0.0.1 192.168.5.15") is None
    assert _pick_shared_ip("") is None


# --- boot() --------------------------------------------------------------------


async def test_boot_builds_create_and_start_commands():
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner)

    ip = await vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-0123456789abcdef0")
    assert ip == "192.168.64.20"

    create_call = next(c for c in runner.calls if "create" in c)
    assert create_call[:4] == ["limactl", "create", "--tty=false", f"--name={NAME}"]
    start_call = next(c for c in runner.calls if "start" in c)
    assert start_call == ["limactl", "start", "--timeout=300s", NAME]


async def test_boot_yaml_has_shared_network():
    # The yaml is written, read by `create`, then deleted in boot()'s
    # `finally` -- so capture its content via the runner call itself before
    # cleanup, not by re-reading the path afterward (it's gone by then).
    class _CapturingRunner(FakeRunner):
        async def __call__(self, args, input=None):
            if "create" in args:
                self.doc = yaml.safe_load(Path(args[-1]).read_text())
            return await super().__call__(args, input=input)

    runner = _CapturingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner)
    await vm.boot(NAME, get_instance_type("t3.micro"), hostname="ec2-test")
    assert runner.doc["networks"] == [{"vzNAT": True}]  # vzNAT, not socket_vmnet -- see lima_yaml.py


async def test_boot_temp_yaml_is_cleaned_up_after_start():
    class _TrackingRunner(FakeRunner):
        async def __call__(self, args, input=None):
            if "create" in args:
                self.yaml_path = Path(args[-1])
                self.yaml_existed_during_create = self.yaml_path.exists()
            return await super().__call__(args, input=input)

    runner = _TrackingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner)
    await vm.boot(NAME, get_instance_type("t3.micro"), hostname="ec2-test")
    assert runner.yaml_existed_during_create is True
    assert not runner.yaml_path.exists()  # cleaned up after start returns


async def test_boot_ssh_pubkey_and_user_data_land_in_the_cloud_init_script():
    class _TrackingRunner(FakeRunner):
        async def __call__(self, args, input=None):
            if "create" in args:
                self.script = yaml.safe_load(Path(args[-1]).read_text())["provision"][0]["script"]
            return await super().__call__(args, input=input)

    runner = _TrackingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner)
    await vm.boot(
        NAME, get_instance_type("t3.micro"), hostname="ec2-test",
        ssh_pubkey="ssh-ed25519 AAAA test", user_data="echo hello-from-user-data",
    )
    assert "ssh-ed25519 AAAA test" in runner.script
    assert "echo hello-from-user-data" in runner.script


async def test_boot_env_vars_land_in_the_cloud_init_script():
    class _TrackingRunner(FakeRunner):
        async def __call__(self, args, input=None):
            if "create" in args:
                self.script = yaml.safe_load(Path(args[-1]).read_text())["provision"][0]["script"]
            return await super().__call__(args, input=input)

    runner = _TrackingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner)
    await vm.boot(
        NAME, get_instance_type("t3.micro"), hostname="ec2-test",
        env_vars={"AWS_ACCESS_KEY_ID": "AKboot", "AWS_SECRET_ACCESS_KEY": "bootsec"},
    )
    assert "AWS_ACCESS_KEY_ID=AKboot" in runner.script
    assert "aws_access_key_id=AKboot" in runner.script


async def test_boot_polls_until_the_shared_ip_appears():
    runner = FakeRunner()
    runner.hostname_i_queue = ["192.168.5.9", "192.168.5.9", "192.168.64.30"]
    vm = InstanceVm(runner=runner, poll_interval=0.0)
    ip = await vm.boot(NAME, get_instance_type("t3.micro"), hostname="ec2-test", timeout=5.0)
    assert ip == "192.168.64.30"


async def test_boot_raises_timeout_when_ip_never_appears():
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.5.9")  # only slirp, forever
    vm = InstanceVm(runner=runner, poll_interval=0.0)
    with pytest.raises(TimeoutError):
        await vm.boot(NAME, get_instance_type("t3.micro"), hostname="ec2-test", timeout=0.05)


async def test_boot_raises_when_create_fails():
    runner = FakeRunner()
    runner.responses["create"] = _Proc(1, "", "boom")
    vm = InstanceVm(runner=runner)
    with pytest.raises(RuntimeError, match="boom"):
        await vm.boot(NAME, get_instance_type("t3.micro"), hostname="ec2-test")


# --- stop / start / delete / status --------------------------------------------


async def test_stop_never_raises_even_on_failure():
    runner = FakeRunner()
    runner.responses["stop"] = _Proc(1, "", "no such vm")
    await InstanceVm(runner=runner).stop(NAME)  # must not raise -- check=False


async def test_start_reboots_and_rediscovers_ip():
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.55")
    ip = await InstanceVm(runner=runner).start(NAME)
    assert ip == "192.168.64.55"
    start_call = next(c for c in runner.calls if "start" in c)
    assert start_call == ["limactl", "start", "--timeout=300s", NAME]


async def test_delete_stops_then_deletes_by_exact_name_only():
    runner = FakeRunner()
    await InstanceVm(runner=runner).delete(NAME)
    # Exactly these two calls, both naming this VM verbatim -- never a
    # wildcard/`--all` that could touch a VM outside this convention.
    assert runner.calls == [["limactl", "stop", "--force", NAME], ["limactl", "delete", "--force", NAME]]


async def test_status_filters_by_exact_name_from_json_lines():
    runner = FakeRunner()
    runner.responses["list"] = _Proc(
        0,
        '{"name": "veronica", "status": "Running"}\n'
        f'{{"name": "{NAME}", "status": "Running"}}\n',
    )
    assert await InstanceVm(runner=runner).status(NAME) == "running"
    assert await InstanceVm(runner=runner).status("veronica-typo") == "absent"


async def test_status_absent_when_vm_is_gone():
    runner = FakeRunner()
    runner.responses["list"] = _Proc(0, "")
    assert await InstanceVm(runner=runner).status(NAME) == "absent"


async def test_list_names_returns_every_name_from_json_lines():
    runner = FakeRunner()
    runner.responses["list"] = _Proc(
        0,
        '{"name": "veronica", "status": "Running"}\n'
        f'{{"name": "{NAME}", "status": "Stopped"}}\n',
    )
    assert await InstanceVm(runner=runner).list_names() == ["veronica", NAME]


async def test_list_names_empty_when_no_vms():
    runner = FakeRunner()
    runner.responses["list"] = _Proc(0, "")
    assert await InstanceVm(runner=runner).list_names() == []


# --- logs (w1 observability: the VM's journal, the container-runtime's
# closest honest equivalent since there's no single process to attach to) --


async def test_logs_reads_the_vms_journal_tail():
    runner = FakeRunner()
    runner.responses["journalctl"] = _Proc(0, "boot line 1\nboot line 2\n")
    out = await InstanceVm(runner=runner).logs(NAME, tail=5)
    assert out == "boot line 1\nboot line 2\n"
    call = next(c for c in runner.calls if "journalctl" in c)
    assert call == ["limactl", "shell", NAME, "--", "sudo", "journalctl", "-n", "5", "--no-pager"]


async def test_logs_never_raises_when_the_vm_is_unreachable():
    runner = FakeRunner()
    runner.responses["journalctl"] = _Proc(1, "", "no such instance")
    out = await InstanceVm(runner=runner).logs("odin-ec2-default-gone")
    assert "not reachable" in out
    assert "no such instance" in out


# --- Nebula join -----------------------------------------------------------------


class FakeLighthouseManager:
    """R3: the injectable seam `InstanceVm._activate_nebula` calls -- no real
    `sudo`/`nebula` involved, just records what it was asked to do."""

    def __init__(self) -> None:
        self.started: list[tuple] = []

    def ensure_started(self, root, env, underlay):
        self.started.append((root, env, underlay))
        return True


async def test_boot_with_nebula_signs_a_cert_and_installs_the_binary_via_cloud_init(tmp_path):
    """Cloud-init time: cert material + the nebula BINARY/unit land on the
    VM's disk -- `config.yml` deliberately does NOT (it needs the real
    underlay, only known post-boot -- see the next test)."""
    class _TrackingRunner(FakeRunner):
        async def __call__(self, args, input=None):
            if "create" in args:
                self.script = yaml.safe_load(Path(args[-1]).read_text())["provision"][0]["script"]
            return await super().__call__(args, input=input)

    runner = _TrackingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner, lighthouse=FakeLighthouseManager())

    nebula = NebulaJoin(root=tmp_path, env="myenv", host_id="i-0123456789abcdef0")
    await vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-0123456789abcdef0", nebula=nebula)

    # ensure_network also signs a "lighthouse" cert (first-time network
    # bootstrap) -- find the SIGN call for the instance's own host id.
    sign_call = next(c for c in runner.calls if "sign" in c and "i-0123456789abcdef0" in c)
    assert "-name" in sign_call
    assert (tmp_path / "myenv" / "nebula" / "ca.crt").exists()
    assert "cat > /etc/nebula/host.crt" in runner.script
    assert "chmod 600 /etc/nebula/host.key" in runner.script
    assert "cat > /etc/nebula/config.yml" not in runner.script
    assert "nebula-linux-${ARCH}.tar.gz" in runner.script
    assert "cat > /etc/systemd/system/nebula.service" in runner.script
    assert "systemctl enable --now nebula" not in runner.script  # not started yet


async def test_boot_signs_the_instances_security_groups_as_cert_groups(tmp_path):
    """W2.6: `NebulaJoin.groups` (the instance's sg ids) ride into
    `nebula-cert sign -groups`, alongside the standing "ec2" group -- nebula
    matches a peer's `group:` firewall rule against THESE, so an SG-to-SG
    rule ("allow 5432 from sg-web") can only ever match if the sg id is in
    the peer's certificate."""
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner, lighthouse=FakeLighthouseManager())

    nebula = NebulaJoin(
        root=tmp_path, env="myenv", host_id="i-0123456789abcdef0",
        groups=("sg-web00000000000000", "sg-ops00000000000000"),
    )
    await vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-0123456789abcdef0", nebula=nebula)

    sign_call = next(c for c in runner.calls if "sign" in c and "i-0123456789abcdef0" in c)
    # SORTED, deliberately (field test 3 HIGH-1): membership is now compared
    # across Applies to decide whether to re-issue this cert, and the order the
    # gateway lists an instance's groups in is not meaningful -- an unsorted
    # comparison would restart a daemon over a reorder.
    assert sign_call[sign_call.index("-groups") + 1] == "ec2,sg-ops00000000000000,sg-web00000000000000"


async def test_boot_without_nebula_never_touches_the_fabric():
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner)
    await vm.boot(NAME, get_instance_type("t3.micro"), hostname="ec2-test")
    assert not any("nebula-cert" in c for c in runner.calls)


# --- R3: post-boot activation (real underlay + the real daemon start) -----------


def _ifconfig_response(host_addr: str) -> _Proc:
    return _Proc(0, f"bridge100: flags=8a63<UP,BROADCAST>\n\tinet {host_addr} netmask 0xffffff00 broadcast 192.168.64.255\n")


async def test_activate_nebula_derives_underlay_and_writes_final_config(tmp_path):
    """Post-boot: the host's OWN address on the VM's vzNAT /24 is derived by
    correlating to the VM's just-discovered IP (not a hardcoded subnet), and
    the FINAL config (real underlay, real firewall) lands on the VM."""
    class _TrackingRunner(FakeRunner):
        async def __call__(self, args, input=None):
            if args[:4] == ["limactl", "shell", NAME, "--"] and "tee" in args:
                self.config_input = input
            return await super().__call__(args, input=input)

    runner = _TrackingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    runner.responses["ifconfig"] = _ifconfig_response("192.168.64.1")
    lighthouse = FakeLighthouseManager()
    vm = InstanceVm(runner=runner, lighthouse=lighthouse)

    firewall = FirewallRules(inbound=[FirewallRule(port="8080", proto="tcp", cidr="0.0.0.0/0")])
    nebula = NebulaJoin(root=tmp_path, env="myenv", host_id="i-0123456789abcdef0", firewall=firewall)
    ip = await vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-0123456789abcdef0", nebula=nebula)
    assert ip == "192.168.64.20"

    assert lighthouse.started == [(tmp_path, "myenv", "192.168.64.1")]
    config = yaml.safe_load(runner.config_input)
    # 4342, not the members' own 4242: Lima forwards a VM's 4242 to the host's
    # loopback, stealing it from the lighthouse (fabric/nebula.py::LIGHTHOUSE_PORT).
    assert config["static_host_map"] == {"10.42.0.1": ["192.168.64.1:4342"]}
    assert config["firewall"]["inbound"] == [{"port": "8080", "proto": "tcp", "cidr": "0.0.0.0/0"}]
    # R5: stock Lima vz has no VM-to-VM underlay path -- every VM routes to
    # every other VM through the lighthouse acting as a relay.
    assert config["relay"] == {"use_relays": True, "relays": ["10.42.0.1"]}
    enable_call = next(c for c in runner.calls if "enable" in c and "nebula" in c)
    assert enable_call == ["limactl", "shell", NAME, "--", "sudo", "systemctl", "enable", "--now", "nebula"]


async def test_activate_nebula_without_firewall_falls_back_to_default_allow_all(tmp_path):
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    runner.responses["ifconfig"] = _ifconfig_response("192.168.64.1")
    vm = InstanceVm(runner=runner, lighthouse=FakeLighthouseManager())

    nebula = NebulaJoin(root=tmp_path, env="myenv", host_id="i-nofw")
    await vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-nofw", nebula=nebula)
    # No exception, no `firewall=None` crash -- DEFAULT_FIREWALL (allow-all) used.


async def test_activate_nebula_is_best_effort_when_underlay_cannot_be_derived(tmp_path):
    """No `inet 192.168.64.*` line anywhere in `ifconfig` -> can't derive the
    underlay -- must degrade gracefully (log + skip), never raise or fail
    the boot itself (the instance is real and running regardless)."""
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    runner.responses["ifconfig"] = _Proc(0, "lo0: flags=8049\n\tinet 127.0.0.1 netmask 0xff000000\n")
    lighthouse = FakeLighthouseManager()
    vm = InstanceVm(runner=runner, lighthouse=lighthouse)

    nebula = NebulaJoin(root=tmp_path, env="myenv", host_id="i-nounderlay")
    ip = await vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-nounderlay", nebula=nebula)

    assert ip == "192.168.64.20"  # boot() itself still succeeds
    assert lighthouse.started == []
    assert not any("config.yml" in " ".join(c) for c in runner.calls)


async def test_activate_nebula_never_raises_even_if_lighthouse_manager_blows_up(tmp_path):
    class ExplodingLighthouse(FakeLighthouseManager):
        def ensure_started(self, root, env, underlay):
            raise RuntimeError("sudo not authorized")

    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    runner.responses["ifconfig"] = _ifconfig_response("192.168.64.1")
    vm = InstanceVm(runner=runner, lighthouse=ExplodingLighthouse())

    nebula = NebulaJoin(root=tmp_path, env="myenv", host_id="i-explode")
    ip = await vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-explode", nebula=nebula)
    assert ip == "192.168.64.20"  # never raised out of boot()


# --- refresh_nebula: an SG edit reaching an ALREADY-RUNNING VM (HIGH-1) ------


async def _booted_vm(tmp_path, firewall=None, host_id="i-refresh", groups=()):
    """A VM that has really been through `boot()` -- so the env's mesh is
    bootstrapped and odin has recorded the config it put on the VM, which is
    the state every refresh starts from."""
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    runner.responses["ifconfig"] = _ifconfig_response("192.168.64.1")
    vm = InstanceVm(runner=runner, lighthouse=FakeLighthouseManager())
    nebula = NebulaJoin(root=tmp_path, env="myenv", host_id=host_id, firewall=firewall, groups=groups)
    await vm.boot(NAME, get_instance_type("t3.micro"), hostname=host_id, nebula=nebula)
    runner.calls.clear()
    return vm, runner, nebula


def _rules(*ports: str) -> FirewallRules:
    return FirewallRules(inbound=[FirewallRule(port=p, proto="tcp", cidr="0.0.0.0/0") for p in ports])


async def test_refresh_nebula_does_nothing_at_all_when_the_rules_are_unchanged(tmp_path):
    """NO CHURN: this runs for every running instance on every Apply, so an
    unchanged firewall must cost one local file read -- no `limactl`, no
    signal, no restarted tunnel."""
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"))

    assert await vm.refresh_nebula(NAME, nebula) == "unchanged"
    assert runner.calls == []


async def test_refresh_nebula_reloads_a_running_vm_when_a_security_group_rule_is_added(tmp_path):
    """Field test 2 HIGH-1: `tcp:8080` added to `web-sg`, Apply says
    `applied`, and the already-running VM kept enforcing port 22 only
    (`NRestarts=0`). SIGHUP, not restart: nebula reloads firewall rules in
    place (verified live), so the VM's existing tunnels are never dropped to
    widen a rule."""
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"))

    widened = NebulaJoin(root=nebula.root, env=nebula.env, host_id=nebula.host_id, firewall=_rules("22", "8080"))
    assert await vm.refresh_nebula(NAME, widened) == "reloaded"

    tee = next(c for c in runner.calls if "tee" in c)
    assert tee == ["limactl", "shell", NAME, "--", "sudo", "tee", "/etc/nebula/config.yml"]
    signal = next(c for c in runner.calls if "kill" in c)
    assert signal == ["limactl", "shell", NAME, "--", "sudo", "systemctl", "kill", "-s", "HUP", "nebula"]
    assert not any("restart" in c for c in runner.calls), "widening a rule must not drop the tunnel"

    recorded = yaml.safe_load(instance_config_path(tmp_path, "myenv", "i-refresh").read_text())
    assert {r["port"] for r in recorded["firewall"]["inbound"]} == {"22", "8080"}
    # ...and now it IS the current state, so the next Apply is a no-op again.
    runner.calls.clear()
    assert await vm.refresh_nebula(NAME, widened) == "unchanged"
    assert runner.calls == []


async def test_refresh_nebula_restarts_when_something_reload_cannot_cover_changed(tmp_path):
    """A MOVED LIGHTHOUSE PORT changes `static_host_map`, which nebula does
    NOT reload on SIGHUP -- so a HUP here would be a lie. A restart is honest
    and costs nothing: in the only case that produces this, the VM's tunnel to
    the lighthouse is already dead."""
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"))
    overlay_path = tmp_path / "myenv" / "nebula" / "overlay.json"
    overlay = json.loads(overlay_path.read_text())
    overlay["lighthouse_port"] = overlay["lighthouse_port"] + 7
    overlay_path.write_text(json.dumps(overlay))

    assert await vm.refresh_nebula(NAME, nebula) == "restarted"
    restart = next(c for c in runner.calls if "restart" in c)
    assert restart == ["limactl", "shell", NAME, "--", "sudo", "systemctl", "restart", "nebula"]
    assert not any("HUP" in c for c in runner.calls)
    pushed = yaml.safe_load(instance_config_path(tmp_path, "myenv", "i-refresh").read_text())
    assert pushed["static_host_map"] == {"10.42.0.1": [f"192.168.64.1:{overlay['lighthouse_port']}"]}


async def test_refresh_nebula_reads_the_vm_itself_when_odin_has_no_record(tmp_path):
    """A VM booted before this existed has no recorded config -- so ask the VM
    what it is actually running rather than restarting it on no evidence."""
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"))
    on_the_vm = instance_config_path(tmp_path, "myenv", "i-refresh").read_text()
    instance_config_path(tmp_path, "myenv", "i-refresh").unlink()
    runner.responses["cat /etc/nebula/config.yml"] = _Proc(0, on_the_vm)

    assert await vm.refresh_nebula(NAME, nebula) == "unchanged"
    assert not any("tee" in c or "systemctl" in c for c in runner.calls)


async def test_refresh_nebula_restarts_when_the_vm_cannot_be_read_at_all(tmp_path):
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"))
    instance_config_path(tmp_path, "myenv", "i-refresh").unlink()
    runner.responses["cat /etc/nebula/config.yml"] = _Proc(1, "", "connection refused")

    assert await vm.refresh_nebula(NAME, nebula) == "restarted"


async def test_refresh_nebula_skips_an_env_with_no_mesh_bootstrapped(tmp_path):
    runner = FakeRunner()
    vm = InstanceVm(runner=runner, lighthouse=FakeLighthouseManager())
    nebula = NebulaJoin(root=tmp_path, env="no-mesh", host_id="i-nomesh")
    assert await vm.refresh_nebula(NAME, nebula) == "skipped"
    assert runner.calls == []


async def test_refresh_nebula_never_raises(tmp_path):
    """Same rule as `_activate_nebula`: mesh wiring must never fail an Apply."""
    class Exploding(FakeRunner):
        async def __call__(self, args, input=None):
            raise RuntimeError("limactl is not on PATH")

    vm, _runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"))
    broken = InstanceVm(runner=Exploding(), lighthouse=FakeLighthouseManager())
    widened = NebulaJoin(root=nebula.root, env=nebula.env, host_id=nebula.host_id, firewall=_rules("22", "8080"))
    assert await broken.refresh_nebula(NAME, widened) == "failed"


# --- membership: moving an instance BETWEEN groups (field test 3 HIGH-1) ----
#
# The REVOKE direction is what makes this a security fix rather than a
# convenience one: the engineer moved `web1` out of `web-sg` (the group the
# database admits) and into `admin-sg`, Apply returned `applied` with zero
# warnings, and web1 went on reaching the database -- because an instance's
# membership lives in its CERTIFICATE, which nothing re-issued.


def _sign_groups(runner) -> list[str]:
    sign = next(c for c in runner.calls if "nebula-cert" in c and "sign" in c)
    return sign[sign.index("-groups") + 1].split(",")


def _moved(nebula, *groups: str) -> NebulaJoin:
    return NebulaJoin(
        root=nebula.root, env=nebula.env, host_id=nebula.host_id,
        firewall=nebula.firewall, groups=groups,
    )


async def test_revoking_a_groups_membership_reissues_the_cert_and_restarts_the_daemon(tmp_path):
    """THE fix. `web1` leaves `web-sg`: a new certificate WITHOUT that group is
    signed and landed on the VM, and the daemon is restarted -- a SIGHUP would
    reload the firewall while every peer went on holding the OLD identity it
    cached at handshake time, which is precisely a revoke that does nothing."""
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"), groups=("sg-web",))

    assert await vm.refresh_nebula(NAME, _moved(nebula)) == "recertified"

    assert _sign_groups(runner) == ["ec2"], "the revoked group must be gone from the new cert"
    landed = next(c for c in runner.calls if c[-3:] == ["sudo", "bash", "-s"])
    assert landed[:4] == ["limactl", "shell", NAME, "--"]
    assert any("restart" in c for c in runner.calls), "a re-issued cert only reaches the wire on a restart"
    assert not any("HUP" in c for c in runner.calls)


async def test_granting_a_group_reissues_the_cert_too(tmp_path):
    """Revoke must be no less reliable than grant, so both go down the same
    path -- the direction is not even visible to this code."""
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"))

    assert await vm.refresh_nebula(NAME, _moved(nebula, "sg-web")) == "recertified"
    assert _sign_groups(runner) == ["ec2", "sg-web"]


async def test_an_unchanged_membership_re_issues_nothing(tmp_path):
    """NO CHURN: re-issuing a cert restarts a daemon and drops every live
    tunnel, so it must happen only on a REAL membership change -- and the
    check for one must stay a single local file read."""
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"), groups=("sg-web",))

    assert await vm.refresh_nebula(NAME, nebula) == "unchanged"
    assert runner.calls == []


async def test_reordered_groups_are_not_a_membership_change(tmp_path):
    """The gateway's group ORDER is not meaningful; treating a reorder as a
    move would restart a running VM's nebula on every Apply."""
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"), groups=("sg-web", "sg-ops"))

    assert await vm.refresh_nebula(NAME, _moved(nebula, "sg-ops", "sg-web")) == "unchanged"
    assert runner.calls == []


async def test_a_rule_edit_alone_still_reloads_without_touching_the_cert(tmp_path):
    """v0.7.1's fix must not regress into a restart: widening a rule leaves
    membership alone, so it stays a SIGHUP with live tunnels intact."""
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"), groups=("sg-web",))

    widened = NebulaJoin(
        root=nebula.root, env=nebula.env, host_id=nebula.host_id,
        firewall=_rules("22", "8080"), groups=("sg-web",),
    )
    assert await vm.refresh_nebula(NAME, widened) == "reloaded"
    assert not any("nebula-cert" in c for c in runner.calls)
    assert not any("restart" in c for c in runner.calls)


async def test_a_cert_that_cannot_be_landed_on_the_vm_fails_the_refresh(tmp_path):
    """And leaves NO record, so the next Apply tries again -- recording a
    membership odin only half-applied would re-create the original bug in a
    subtler form (the VM keeps the old identity, and odin believes it doesn't)."""
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"), groups=("sg-web",))
    runner.responses["bash -s"] = _Proc(1, "", "connection refused")

    assert await vm.refresh_nebula(NAME, _moved(nebula)) == "failed"
    assert not any("systemctl" in c for c in runner.calls), "nothing was adopted, so nothing was signalled"
    # ...and the next Apply still sees a membership change to apply.
    runner.responses.pop("bash -s")
    runner.calls.clear()
    assert await vm.refresh_nebula(NAME, _moved(nebula)) == "recertified"


async def test_a_restart_pokes_every_peer_to_re_handshake_immediately(tmp_path):
    """Field test 3 MED-2: after a mesh restart there is a ~10-60s window where
    peers keep using the tunnel that just died before nebula drops it, so the
    address does not answer while `/world` says healthy. The restarted member
    moves first instead."""
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"), groups=("sg-web",))
    overlay_path = tmp_path / "myenv" / "nebula" / "overlay.json"
    overlay = json.loads(overlay_path.read_text())
    overlay["subnets"]["hosts"]["assignments"]["odin-rds-myenv-db"] = "10.42.1.9"
    overlay_path.write_text(json.dumps(overlay))

    assert await vm.refresh_nebula(NAME, _moved(nebula)) == "recertified"

    pokes = [c for c in runner.calls if c[-3:] == ["sudo", "bash", "-s"]]
    assert len(pokes) == 2, "the cert push and the convergence poke"


async def test_a_reload_never_pokes_because_it_never_dropped_a_tunnel(tmp_path):
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"), groups=("sg-web",))
    widened = NebulaJoin(
        root=nebula.root, env=nebula.env, host_id=nebula.host_id,
        firewall=_rules("22", "8080"), groups=("sg-web",),
    )
    assert await vm.refresh_nebula(NAME, widened) == "reloaded"
    assert not any(c[-3:] == ["sudo", "bash", "-s"] for c in runner.calls)


async def test_a_moved_roster_reaches_a_bystander_vm_as_a_reload(tmp_path):
    """Field test 4, from the ADMITTING side. This VM's own groups and rules
    are untouched -- somebody ELSE in the env lost a group. It still has to act,
    because the flows it already permitted from that member are still open and
    nebula only re-validates them when its OWN ruleset version moves.

    A reload is the whole point: this fires for every member on every
    membership change in the env, so a restart here would drop every tunnel in
    the env every time anyone's groups moved."""
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("5432"), groups=("sg-db",))

    moved_roster = NebulaJoin(
        root=nebula.root, env=nebula.env, host_id=nebula.host_id,
        firewall=_rules("5432"), groups=("sg-db",), revision="roster-2",
    )
    assert await vm.refresh_nebula(NAME, moved_roster) == "reloaded"
    assert not any("restart" in c for c in runner.calls), "a bystander must not lose its tunnels"
    assert not any("nebula-cert" in c for c in runner.calls), "its own membership did not change"
    signal = next(c for c in runner.calls if "kill" in c)
    assert signal == ["limactl", "shell", NAME, "--", "sudo", "systemctl", "kill", "-s", "HUP", "nebula"]

    recorded = yaml.safe_load(instance_config_path(tmp_path, "myenv", "i-refresh").read_text())
    assert recorded["firewall"][FIREWALL_REVISION_KEY] == "roster-2"
    assert [r["port"] for r in recorded["firewall"]["inbound"]] == ["5432"], (
        "the rules are untouched -- an equal-rules reload is exactly what bumps the ruleset version"
    )
    # ...and having taken it, the next Apply over the same roster is free again.
    runner.calls.clear()
    assert await vm.refresh_nebula(NAME, moved_roster) == "unchanged"
    assert runner.calls == []


async def test_membership_changed_is_the_cheap_predicate_the_caller_orders_by(tmp_path):
    """`ensure_instance_mesh` re-certifies moved instances FIRST so an admitting
    member never re-checks a flow against a certificate the peer is about to
    replace. It has to be able to ask that question without a subprocess."""
    vm, _runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"), groups=("sg-web",))
    assert membership_changed(nebula) is False
    assert membership_changed(_moved(nebula)) is True
    # A revision move alone is NOT a membership move -- the bystander reloads,
    # it does not get re-signed, and it must not jump the queue.
    bystander = NebulaJoin(
        root=nebula.root, env=nebula.env, host_id=nebula.host_id,
        firewall=_rules("22"), groups=("sg-web",), revision="roster-2",
    )
    assert membership_changed(bystander) is False
    assert await vm.refresh_nebula(NAME, bystander) == "reloaded"


async def test_a_failed_signal_is_reported_not_swallowed(tmp_path):
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"))
    runner.responses["systemctl kill"] = _Proc(1, "", "Unit nebula.service not loaded")
    widened = NebulaJoin(root=nebula.root, env=nebula.env, host_id=nebula.host_id, firewall=_rules("22", "8080"))
    assert await vm.refresh_nebula(NAME, widened) == "failed"


# --- boot concurrency (owner directive B2): bound how many VMs may be
# mid-`limactl create`/`start` at once, process-wide -----------------------


def test_default_max_concurrent_boots_reads_the_env_var(monkeypatch):
    monkeypatch.setenv("ODIN_MAX_CONCURRENT_VM_BOOTS", "7")
    assert _default_max_concurrent_boots() == 7


def test_default_max_concurrent_boots_falls_back_to_3(monkeypatch):
    monkeypatch.delenv("ODIN_MAX_CONCURRENT_VM_BOOTS", raising=False)
    assert _default_max_concurrent_boots() == 3


def test_boot_semaphore_defaults_to_the_process_wide_one():
    # `vm or InstanceVm()` (ec2compute.py's pure_answer) constructs a FRESH
    # InstanceVm per gateway call -- the bound only works process-wide if
    # every one of those shares the SAME semaphore by default.
    assert InstanceVm(runner=FakeRunner())._boot_semaphore is _BOOT_SEMAPHORE


async def test_boot_never_exceeds_the_semaphore_bound_under_real_concurrency():
    """Three `boot()` calls, genuinely concurrent, against a `boot_semaphore`
    sized 1: at most ONE may be inside the create/start pair at any instant --
    draw N EC2 nodes and they queue instead of stampeding every VM boot onto
    the Mac at once.

    v0.7.7: "genuinely concurrent" is now three asyncio TASKS rather than three
    OS threads, because `boot()` is a coroutine -- `threading.Thread(target=
    vm.boot)` would only build three coroutine objects and drop them, i.e. this
    test would assert nothing at all. The `await asyncio.sleep(0.05)` inside the
    runner is what makes the race real: it is a suspension point in the middle
    of the guarded region, which is exactly where an unguarded second task gets
    in. Measured with the guard removed: peak 3.

    BLOCKED ON src (reported, not fixed here): `compute/instances.py` still
    holds `threading.Semaphore` with a plain `with` inside `async def boot`, so
    this fails on `__enter__` until that becomes `asyncio.Semaphore` +
    `async with` -- which `instances.py`'s own de-threading verdict comment
    already commits to. A blocking `with` on one event loop would not merely be
    slow, it would DEADLOCK the second task."""
    in_flight = 0
    peak = 0

    class _ConcurrencyTrackingRunner(FakeRunner):
        async def __call__(self, args, input=None):
            nonlocal in_flight, peak
            if "create" in args or ("start" in args and "shell" not in args):
                # No lock: a coroutine runs to completion between `await`s, so
                # this read-modify-write is already atomic w.r.t. the other
                # tasks (the whole reason the threads went).
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.05)  # hold the "boot" long enough to overlap
                in_flight -= 1
            return await super().__call__(args, input=input)

    runner = _ConcurrencyTrackingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner, boot_semaphore=asyncio.Semaphore(1))

    await asyncio.gather(*(
        vm.boot(f"{NAME}-{i}", get_instance_type("t3.micro"), hostname=f"h{i}")
        for i in range(3)
    ))

    assert peak == 1


async def test_start_also_goes_through_the_boot_semaphore():
    """Same conversion, and the same src block: `start()` must hold the bound
    for its whole duration. `entered`/`released` become asyncio Events so the
    assertions can run while `start()` is genuinely suspended inside the
    guarded region."""
    entered = asyncio.Event()
    released = asyncio.Event()

    class _BlockingRunner(FakeRunner):
        async def __call__(self, args, input=None):
            if "start" in args and "shell" not in args:
                entered.set()
                await released.wait()
            return await super().__call__(args, input=input)

    runner = _BlockingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    sem = asyncio.Semaphore(1)
    vm = InstanceVm(runner=runner, boot_semaphore=sem)

    task = asyncio.create_task(vm.start(NAME))
    async with asyncio.timeout(2):
        await entered.wait()
    assert sem.locked() is True  # held for the duration of start()
    released.set()
    async with asyncio.timeout(2):
        await task
    assert sem.locked() is False  # released once start() returns


# --- a `limactl` failure that said NOTHING ----------------------------------
#
# `_lima` raised `f"limactl {' '.join(args)} failed: {proc.stderr.strip()}"`,
# which renders `limactl shell <vm> -- ... failed: ` -- a sentence whose reason
# slot is a dangling colon -- for the whole class of failure this module
# actually produces. PROBED against REAL limactl 2.1.3 and a REAL Lima VM
# (created, driven, deleted), never reasoned about; each of these exits
# non-zero having written NOTHING to stderr:
#
#     shell <vm> -- sh -c 'exit 3'                       rc=3 err='' out=''
#     shell <vm> -- sh -c 'echo on-stdout; exit 7'       rc=7 err='' out='on-stdout\n'
#     shell <vm> -- sudo bash -s  <<< 'exit 9'           rc=9 err='' out=''
#     shell <vm> -- sudo bash -s  <<< 'echo x; exit 4'   rc=4 err='' out='x\n'
#     shell <vm> -- false                                rc=1 err='' out=''
#
# `limactl shell` PROPAGATES the guest's exit code (3, 7, 9, 4), so the one fact
# present every time was the one being discarded -- and case two shows the
# reason can be on STDOUT, which this seam kept only on success. The runners
# below replay exactly those measured triples: the wording is what is under test
# here, the integration was proved by running the real commands.


async def test_a_limactl_failure_with_no_output_at_all_still_states_a_reason():
    async def runner(args, input=None):
        return _Proc(3, "", "")

    with pytest.raises(RuntimeError) as raised:
        await InstanceVm(runner=runner).start(NAME)
    message = str(raised.value)
    assert message == (
        f"limactl start --timeout=300s {NAME} failed (exit 3): it wrote nothing "
        "to stderr or stdout, so the exit code is the whole of it"
    )
    assert not message.rstrip().endswith(":"), "nothing may trail off"


async def test_a_limactl_failure_that_explained_itself_on_stdout_is_not_reported_as_silent():
    """Measured: `limactl shell <vm> -- sh -c 'echo on-stdout; exit 7'` is rc=7,
    stderr EMPTY, reason on stdout. `_lima` kept stdout only on success."""
    async def runner(args, input=None):
        return _Proc(7, "on-stdout\n", "")

    with pytest.raises(RuntimeError) as raised:
        await InstanceVm(runner=runner).start(NAME)
    assert str(raised.value) == (
        f"limactl start --timeout=300s {NAME} failed (exit 7): "
        "nothing on stderr; on stdout: on-stdout"
    )


async def test_a_limactl_failure_with_real_stderr_still_leads_with_it():
    """The common case must not regress: a real `limactl` diagnostic is still
    the reason, now with the exit code in front of it."""
    real = 'time="..." level=fatal msg="instance `x` does not exist"'

    async def runner(args, input=None):
        return _Proc(1, "", real + "\n")

    with pytest.raises(RuntimeError) as raised:
        await InstanceVm(runner=runner).start(NAME)
    assert str(raised.value) == f"limactl start --timeout=300s {NAME} failed (exit 1): {real}"


async def test_the_two_limactl_seams_share_ONE_wording_with_docker():
    """`compute/instances.py` and `runtime/lima.py` were an exact
    `f"limactl … failed: {stderr.strip()}"` twin, and `runtime/colima.py` had
    already fixed the same sentence for `docker`. Identity, not equality: a
    re-spelled copy that agrees today would pass an equality check and drift
    tomorrow."""
    assert instances._failure_reason is canonical_failure_reason
    assert lima._failure_reason is canonical_failure_reason

    async def runner(args, input=None):
        return _Proc(9, "", "")

    with pytest.raises(RuntimeError) as from_instances:
        await InstanceVm(runner=runner)._lima("shell", "vm", "--", "sudo", "bash", "-s", input="exit 9\n")
    with pytest.raises(RuntimeError) as from_lima:
        await lima.LimaRuntime(runner=runner)._lima("shell", "vm", "--", "sudo", "bash", "-s", input="exit 9\n")
    assert str(from_instances.value) == str(from_lima.value)
    assert str(from_instances.value) == (
        "limactl shell vm -- sudo bash -s failed (exit 9): it wrote nothing to "
        "stderr or stdout, so the exit code is the whole of it"
    )


async def test_a_cert_that_could_not_be_landed_names_the_exit_code_too(tmp_path):
    """`_reissue_cert`'s message had HALF the treatment (`or 'no output'`, so it
    never trailed off) and threw the exit code away -- on a `sudo bash -s`,
    whose real failure IS a bare exit code. Driven end to end against a REAL
    Lima VM holding a REAL nebula-cert-signed certificate, with `/etc/nebula`
    made a FILE so the guest script really failed:

        rc=1  stderr='mkdir: Already exists\\nbash: line 2: /etc/nebula/host.crt:
              Not a directory\\n...'   -> the exit code was missing
        rc=9  stderr='' stdout=''      -> BEFORE said only 'no output'

    This replays the second, which is the one the old wording lost."""
    vm, runner, nebula = await _booted_vm(tmp_path, firewall=_rules("22"), groups=("sg-web",))
    runner.responses["sudo bash -s"] = _Proc(9, "", "")
    moved = NebulaJoin(
        root=nebula.root, env=nebula.env, host_id=nebula.host_id,
        firewall=_rules("22"), groups=("sg-admin",), revision="roster-2",
    )
    manager = NebulaManager(nebula.root / nebula.env / "nebula", runner=runner)
    with pytest.raises(RuntimeError) as raised:
        await vm._reissue_cert(NAME, moved, manager)
    message = str(raised.value)
    assert "(exit 9)" in message, message
    assert "it wrote nothing to stderr or stdout" in message
    assert "no output" not in message
    assert f"could not land {nebula.host_id}'s re-issued certificate" in message
