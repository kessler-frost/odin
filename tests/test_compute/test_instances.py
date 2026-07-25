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

import json
import threading
import time
from pathlib import Path

import pytest
import yaml

from odin.compute.instances import (
    InstanceVm,
    NebulaJoin,
    _BOOT_SEMAPHORE,
    _default_max_concurrent_boots,
    _Proc,
    _pick_shared_ip,
    instance_config_path,
    vm_name,
)
from odin.compute.models import get_instance_type
from odin.fabric.models import FirewallRule, FirewallRules

NAME = vm_name("default", "i-0123456789abcdef0")


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.responses: dict[str, _Proc] = {}
        self.hostname_i_queue: list[str] = []

    def __call__(self, args: list[str], input: str | None = None) -> _Proc:
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


def test_boot_builds_create_and_start_commands():
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner)

    ip = vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-0123456789abcdef0")
    assert ip == "192.168.64.20"

    create_call = next(c for c in runner.calls if "create" in c)
    assert create_call[:4] == ["limactl", "create", "--tty=false", f"--name={NAME}"]
    start_call = next(c for c in runner.calls if "start" in c)
    assert start_call == ["limactl", "start", "--timeout=300s", NAME]


def test_boot_yaml_has_shared_network():
    # The yaml is written, read by `create`, then deleted in boot()'s
    # `finally` -- so capture its content via the runner call itself before
    # cleanup, not by re-reading the path afterward (it's gone by then).
    class _CapturingRunner(FakeRunner):
        def __call__(self, args, input=None):
            if "create" in args:
                self.doc = yaml.safe_load(Path(args[-1]).read_text())
            return super().__call__(args, input=input)

    runner = _CapturingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner)
    vm.boot(NAME, get_instance_type("t3.micro"), hostname="ec2-test")
    assert runner.doc["networks"] == [{"vzNAT": True}]  # vzNAT, not socket_vmnet -- see lima_yaml.py


def test_boot_temp_yaml_is_cleaned_up_after_start():
    class _TrackingRunner(FakeRunner):
        def __call__(self, args, input=None):
            if "create" in args:
                self.yaml_path = Path(args[-1])
                self.yaml_existed_during_create = self.yaml_path.exists()
            return super().__call__(args, input=input)

    runner = _TrackingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner)
    vm.boot(NAME, get_instance_type("t3.micro"), hostname="ec2-test")
    assert runner.yaml_existed_during_create is True
    assert not runner.yaml_path.exists()  # cleaned up after start returns


def test_boot_ssh_pubkey_and_user_data_land_in_the_cloud_init_script():
    class _TrackingRunner(FakeRunner):
        def __call__(self, args, input=None):
            if "create" in args:
                self.script = yaml.safe_load(Path(args[-1]).read_text())["provision"][0]["script"]
            return super().__call__(args, input=input)

    runner = _TrackingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner)
    vm.boot(
        NAME, get_instance_type("t3.micro"), hostname="ec2-test",
        ssh_pubkey="ssh-ed25519 AAAA test", user_data="echo hello-from-user-data",
    )
    assert "ssh-ed25519 AAAA test" in runner.script
    assert "echo hello-from-user-data" in runner.script


def test_boot_env_vars_land_in_the_cloud_init_script():
    class _TrackingRunner(FakeRunner):
        def __call__(self, args, input=None):
            if "create" in args:
                self.script = yaml.safe_load(Path(args[-1]).read_text())["provision"][0]["script"]
            return super().__call__(args, input=input)

    runner = _TrackingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner)
    vm.boot(
        NAME, get_instance_type("t3.micro"), hostname="ec2-test",
        env_vars={"AWS_ACCESS_KEY_ID": "AKboot", "AWS_SECRET_ACCESS_KEY": "bootsec"},
    )
    assert "AWS_ACCESS_KEY_ID=AKboot" in runner.script
    assert "aws_access_key_id=AKboot" in runner.script


def test_boot_polls_until_the_shared_ip_appears():
    runner = FakeRunner()
    runner.hostname_i_queue = ["192.168.5.9", "192.168.5.9", "192.168.64.30"]
    vm = InstanceVm(runner=runner, poll_interval=0.0)
    ip = vm.boot(NAME, get_instance_type("t3.micro"), hostname="ec2-test", timeout=5.0)
    assert ip == "192.168.64.30"


def test_boot_raises_timeout_when_ip_never_appears():
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.5.9")  # only slirp, forever
    vm = InstanceVm(runner=runner, poll_interval=0.0)
    with pytest.raises(TimeoutError):
        vm.boot(NAME, get_instance_type("t3.micro"), hostname="ec2-test", timeout=0.05)


def test_boot_raises_when_create_fails():
    runner = FakeRunner()
    runner.responses["create"] = _Proc(1, "", "boom")
    vm = InstanceVm(runner=runner)
    with pytest.raises(RuntimeError, match="boom"):
        vm.boot(NAME, get_instance_type("t3.micro"), hostname="ec2-test")


# --- stop / start / delete / status --------------------------------------------


def test_stop_never_raises_even_on_failure():
    runner = FakeRunner()
    runner.responses["stop"] = _Proc(1, "", "no such vm")
    InstanceVm(runner=runner).stop(NAME)  # must not raise -- check=False


def test_start_reboots_and_rediscovers_ip():
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.55")
    ip = InstanceVm(runner=runner).start(NAME)
    assert ip == "192.168.64.55"
    start_call = next(c for c in runner.calls if "start" in c)
    assert start_call == ["limactl", "start", "--timeout=300s", NAME]


def test_delete_stops_then_deletes_by_exact_name_only():
    runner = FakeRunner()
    InstanceVm(runner=runner).delete(NAME)
    # Exactly these two calls, both naming this VM verbatim -- never a
    # wildcard/`--all` that could touch a VM outside this convention.
    assert runner.calls == [["limactl", "stop", "--force", NAME], ["limactl", "delete", "--force", NAME]]


def test_status_filters_by_exact_name_from_json_lines():
    runner = FakeRunner()
    runner.responses["list"] = _Proc(
        0,
        '{"name": "veronica", "status": "Running"}\n'
        f'{{"name": "{NAME}", "status": "Running"}}\n',
    )
    assert InstanceVm(runner=runner).status(NAME) == "running"
    assert InstanceVm(runner=runner).status("veronica-typo") == "absent"


def test_status_absent_when_vm_is_gone():
    runner = FakeRunner()
    runner.responses["list"] = _Proc(0, "")
    assert InstanceVm(runner=runner).status(NAME) == "absent"


def test_list_names_returns_every_name_from_json_lines():
    runner = FakeRunner()
    runner.responses["list"] = _Proc(
        0,
        '{"name": "veronica", "status": "Running"}\n'
        f'{{"name": "{NAME}", "status": "Stopped"}}\n',
    )
    assert InstanceVm(runner=runner).list_names() == ["veronica", NAME]


def test_list_names_empty_when_no_vms():
    runner = FakeRunner()
    runner.responses["list"] = _Proc(0, "")
    assert InstanceVm(runner=runner).list_names() == []


# --- logs (w1 observability: the VM's journal, the container-runtime's
# closest honest equivalent since there's no single process to attach to) --


def test_logs_reads_the_vms_journal_tail():
    runner = FakeRunner()
    runner.responses["journalctl"] = _Proc(0, "boot line 1\nboot line 2\n")
    out = InstanceVm(runner=runner).logs(NAME, tail=5)
    assert out == "boot line 1\nboot line 2\n"
    call = next(c for c in runner.calls if "journalctl" in c)
    assert call == ["limactl", "shell", NAME, "--", "sudo", "journalctl", "-n", "5", "--no-pager"]


def test_logs_never_raises_when_the_vm_is_unreachable():
    runner = FakeRunner()
    runner.responses["journalctl"] = _Proc(1, "", "no such instance")
    out = InstanceVm(runner=runner).logs("odin-ec2-default-gone")
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


def test_boot_with_nebula_signs_a_cert_and_installs_the_binary_via_cloud_init(tmp_path):
    """Cloud-init time: cert material + the nebula BINARY/unit land on the
    VM's disk -- `config.yml` deliberately does NOT (it needs the real
    underlay, only known post-boot -- see the next test)."""
    class _TrackingRunner(FakeRunner):
        def __call__(self, args, input=None):
            if "create" in args:
                self.script = yaml.safe_load(Path(args[-1]).read_text())["provision"][0]["script"]
            return super().__call__(args, input=input)

    runner = _TrackingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner, lighthouse=FakeLighthouseManager())

    nebula = NebulaJoin(root=tmp_path, env="myenv", host_id="i-0123456789abcdef0")
    vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-0123456789abcdef0", nebula=nebula)

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


def test_boot_signs_the_instances_security_groups_as_cert_groups(tmp_path):
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
    vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-0123456789abcdef0", nebula=nebula)

    sign_call = next(c for c in runner.calls if "sign" in c and "i-0123456789abcdef0" in c)
    assert sign_call[sign_call.index("-groups") + 1] == "ec2,sg-web00000000000000,sg-ops00000000000000"


def test_boot_without_nebula_never_touches_the_fabric():
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner)
    vm.boot(NAME, get_instance_type("t3.micro"), hostname="ec2-test")
    assert not any("nebula-cert" in c for c in runner.calls)


# --- R3: post-boot activation (real underlay + the real daemon start) -----------


def _ifconfig_response(host_addr: str) -> _Proc:
    return _Proc(0, f"bridge100: flags=8a63<UP,BROADCAST>\n\tinet {host_addr} netmask 0xffffff00 broadcast 192.168.64.255\n")


def test_activate_nebula_derives_underlay_and_writes_final_config(tmp_path):
    """Post-boot: the host's OWN address on the VM's vzNAT /24 is derived by
    correlating to the VM's just-discovered IP (not a hardcoded subnet), and
    the FINAL config (real underlay, real firewall) lands on the VM."""
    class _TrackingRunner(FakeRunner):
        def __call__(self, args, input=None):
            if args[:4] == ["limactl", "shell", NAME, "--"] and "tee" in args:
                self.config_input = input
            return super().__call__(args, input=input)

    runner = _TrackingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    runner.responses["ifconfig"] = _ifconfig_response("192.168.64.1")
    lighthouse = FakeLighthouseManager()
    vm = InstanceVm(runner=runner, lighthouse=lighthouse)

    firewall = FirewallRules(inbound=[FirewallRule(port="8080", proto="tcp", cidr="0.0.0.0/0")])
    nebula = NebulaJoin(root=tmp_path, env="myenv", host_id="i-0123456789abcdef0", firewall=firewall)
    ip = vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-0123456789abcdef0", nebula=nebula)
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


def test_activate_nebula_without_firewall_falls_back_to_default_allow_all(tmp_path):
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    runner.responses["ifconfig"] = _ifconfig_response("192.168.64.1")
    vm = InstanceVm(runner=runner, lighthouse=FakeLighthouseManager())

    nebula = NebulaJoin(root=tmp_path, env="myenv", host_id="i-nofw")
    vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-nofw", nebula=nebula)
    # No exception, no `firewall=None` crash -- DEFAULT_FIREWALL (allow-all) used.


def test_activate_nebula_is_best_effort_when_underlay_cannot_be_derived(tmp_path):
    """No `inet 192.168.64.*` line anywhere in `ifconfig` -> can't derive the
    underlay -- must degrade gracefully (log + skip), never raise or fail
    the boot itself (the instance is real and running regardless)."""
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    runner.responses["ifconfig"] = _Proc(0, "lo0: flags=8049\n\tinet 127.0.0.1 netmask 0xff000000\n")
    lighthouse = FakeLighthouseManager()
    vm = InstanceVm(runner=runner, lighthouse=lighthouse)

    nebula = NebulaJoin(root=tmp_path, env="myenv", host_id="i-nounderlay")
    ip = vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-nounderlay", nebula=nebula)

    assert ip == "192.168.64.20"  # boot() itself still succeeds
    assert lighthouse.started == []
    assert not any("config.yml" in " ".join(c) for c in runner.calls)


def test_activate_nebula_never_raises_even_if_lighthouse_manager_blows_up(tmp_path):
    class ExplodingLighthouse(FakeLighthouseManager):
        def ensure_started(self, root, env, underlay):
            raise RuntimeError("sudo not authorized")

    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    runner.responses["ifconfig"] = _ifconfig_response("192.168.64.1")
    vm = InstanceVm(runner=runner, lighthouse=ExplodingLighthouse())

    nebula = NebulaJoin(root=tmp_path, env="myenv", host_id="i-explode")
    ip = vm.boot(NAME, get_instance_type("t3.micro"), hostname="i-explode", nebula=nebula)
    assert ip == "192.168.64.20"  # never raised out of boot()


# --- refresh_nebula: an SG edit reaching an ALREADY-RUNNING VM (HIGH-1) ------


def _booted_vm(tmp_path, firewall=None, host_id="i-refresh"):
    """A VM that has really been through `boot()` -- so the env's mesh is
    bootstrapped and odin has recorded the config it put on the VM, which is
    the state every refresh starts from."""
    runner = FakeRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    runner.responses["ifconfig"] = _ifconfig_response("192.168.64.1")
    vm = InstanceVm(runner=runner, lighthouse=FakeLighthouseManager())
    nebula = NebulaJoin(root=tmp_path, env="myenv", host_id=host_id, firewall=firewall)
    vm.boot(NAME, get_instance_type("t3.micro"), hostname=host_id, nebula=nebula)
    runner.calls.clear()
    return vm, runner, nebula


def _rules(*ports: str) -> FirewallRules:
    return FirewallRules(inbound=[FirewallRule(port=p, proto="tcp", cidr="0.0.0.0/0") for p in ports])


def test_refresh_nebula_does_nothing_at_all_when_the_rules_are_unchanged(tmp_path):
    """NO CHURN: this runs for every running instance on every Apply, so an
    unchanged firewall must cost one local file read -- no `limactl`, no
    signal, no restarted tunnel."""
    vm, runner, nebula = _booted_vm(tmp_path, firewall=_rules("22"))

    assert vm.refresh_nebula(NAME, nebula) == "unchanged"
    assert runner.calls == []


def test_refresh_nebula_reloads_a_running_vm_when_a_security_group_rule_is_added(tmp_path):
    """Field test 2 HIGH-1: `tcp:8080` added to `web-sg`, Apply says
    `applied`, and the already-running VM kept enforcing port 22 only
    (`NRestarts=0`). SIGHUP, not restart: nebula reloads firewall rules in
    place (verified live), so the VM's existing tunnels are never dropped to
    widen a rule."""
    vm, runner, nebula = _booted_vm(tmp_path, firewall=_rules("22"))

    widened = NebulaJoin(root=nebula.root, env=nebula.env, host_id=nebula.host_id, firewall=_rules("22", "8080"))
    assert vm.refresh_nebula(NAME, widened) == "reloaded"

    tee = next(c for c in runner.calls if "tee" in c)
    assert tee == ["limactl", "shell", NAME, "--", "sudo", "tee", "/etc/nebula/config.yml"]
    signal = next(c for c in runner.calls if "kill" in c)
    assert signal == ["limactl", "shell", NAME, "--", "sudo", "systemctl", "kill", "-s", "HUP", "nebula"]
    assert not any("restart" in c for c in runner.calls), "widening a rule must not drop the tunnel"

    recorded = yaml.safe_load(instance_config_path(tmp_path, "myenv", "i-refresh").read_text())
    assert {r["port"] for r in recorded["firewall"]["inbound"]} == {"22", "8080"}
    # ...and now it IS the current state, so the next Apply is a no-op again.
    runner.calls.clear()
    assert vm.refresh_nebula(NAME, widened) == "unchanged"
    assert runner.calls == []


def test_refresh_nebula_restarts_when_something_reload_cannot_cover_changed(tmp_path):
    """A MOVED LIGHTHOUSE PORT changes `static_host_map`, which nebula does
    NOT reload on SIGHUP -- so a HUP here would be a lie. A restart is honest
    and costs nothing: in the only case that produces this, the VM's tunnel to
    the lighthouse is already dead."""
    vm, runner, nebula = _booted_vm(tmp_path, firewall=_rules("22"))
    overlay_path = tmp_path / "myenv" / "nebula" / "overlay.json"
    overlay = json.loads(overlay_path.read_text())
    overlay["lighthouse_port"] = overlay["lighthouse_port"] + 7
    overlay_path.write_text(json.dumps(overlay))

    assert vm.refresh_nebula(NAME, nebula) == "restarted"
    restart = next(c for c in runner.calls if "restart" in c)
    assert restart == ["limactl", "shell", NAME, "--", "sudo", "systemctl", "restart", "nebula"]
    assert not any("HUP" in c for c in runner.calls)
    pushed = yaml.safe_load(instance_config_path(tmp_path, "myenv", "i-refresh").read_text())
    assert pushed["static_host_map"] == {"10.42.0.1": [f"192.168.64.1:{overlay['lighthouse_port']}"]}


def test_refresh_nebula_reads_the_vm_itself_when_odin_has_no_record(tmp_path):
    """A VM booted before this existed has no recorded config -- so ask the VM
    what it is actually running rather than restarting it on no evidence."""
    vm, runner, nebula = _booted_vm(tmp_path, firewall=_rules("22"))
    on_the_vm = instance_config_path(tmp_path, "myenv", "i-refresh").read_text()
    instance_config_path(tmp_path, "myenv", "i-refresh").unlink()
    runner.responses["cat /etc/nebula/config.yml"] = _Proc(0, on_the_vm)

    assert vm.refresh_nebula(NAME, nebula) == "unchanged"
    assert not any("tee" in c or "systemctl" in c for c in runner.calls)


def test_refresh_nebula_restarts_when_the_vm_cannot_be_read_at_all(tmp_path):
    vm, runner, nebula = _booted_vm(tmp_path, firewall=_rules("22"))
    instance_config_path(tmp_path, "myenv", "i-refresh").unlink()
    runner.responses["cat /etc/nebula/config.yml"] = _Proc(1, "", "connection refused")

    assert vm.refresh_nebula(NAME, nebula) == "restarted"


def test_refresh_nebula_skips_an_env_with_no_mesh_bootstrapped(tmp_path):
    runner = FakeRunner()
    vm = InstanceVm(runner=runner, lighthouse=FakeLighthouseManager())
    nebula = NebulaJoin(root=tmp_path, env="no-mesh", host_id="i-nomesh")
    assert vm.refresh_nebula(NAME, nebula) == "skipped"
    assert runner.calls == []


def test_refresh_nebula_never_raises(tmp_path):
    """Same rule as `_activate_nebula`: mesh wiring must never fail an Apply."""
    class Exploding(FakeRunner):
        def __call__(self, args, input=None):
            raise RuntimeError("limactl is not on PATH")

    vm, _runner, nebula = _booted_vm(tmp_path, firewall=_rules("22"))
    broken = InstanceVm(runner=Exploding(), lighthouse=FakeLighthouseManager())
    widened = NebulaJoin(root=nebula.root, env=nebula.env, host_id=nebula.host_id, firewall=_rules("22", "8080"))
    assert broken.refresh_nebula(NAME, widened) == "failed"


def test_a_failed_signal_is_reported_not_swallowed(tmp_path):
    vm, runner, nebula = _booted_vm(tmp_path, firewall=_rules("22"))
    runner.responses["systemctl kill"] = _Proc(1, "", "Unit nebula.service not loaded")
    widened = NebulaJoin(root=nebula.root, env=nebula.env, host_id=nebula.host_id, firewall=_rules("22", "8080"))
    assert vm.refresh_nebula(NAME, widened) == "failed"


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


def test_boot_never_exceeds_the_semaphore_bound_under_real_concurrency():
    """Three `boot()` calls, genuinely concurrent (real threads -- the exact
    shape ec2compute.py's `_spawn` produces: one daemon thread per
    RunInstances), against a `boot_semaphore` sized 1: at most ONE may be
    inside the create/start pair at any instant -- draw N EC2 nodes and they
    queue instead of stampeding every VM boot onto the Mac at once."""
    lock = threading.Lock()
    in_flight = 0
    peak = 0

    class _ConcurrencyTrackingRunner(FakeRunner):
        def __call__(self, args, input=None):
            nonlocal in_flight, peak
            if "create" in args or ("start" in args and "shell" not in args):
                with lock:
                    in_flight += 1
                    peak = max(peak, in_flight)
                time.sleep(0.05)  # hold the "boot" long enough to overlap
                with lock:
                    in_flight -= 1
            return super().__call__(args, input=input)

    runner = _ConcurrencyTrackingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    vm = InstanceVm(runner=runner, boot_semaphore=threading.Semaphore(1))

    threads = [
        threading.Thread(
            target=vm.boot, args=(f"{NAME}-{i}", get_instance_type("t3.micro")), kwargs={"hostname": f"h{i}"},
        )
        for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert peak == 1


def test_start_also_goes_through_the_boot_semaphore():
    entered = threading.Event()
    released = threading.Event()

    class _BlockingRunner(FakeRunner):
        def __call__(self, args, input=None):
            if "start" in args and "shell" not in args:
                entered.set()
                released.wait(timeout=2)
            return super().__call__(args, input=input)

    runner = _BlockingRunner()
    runner.responses["hostname -I"] = _Proc(0, "192.168.64.20")
    sem = threading.Semaphore(1)
    vm = InstanceVm(runner=runner, boot_semaphore=sem)

    t = threading.Thread(target=vm.start, args=(NAME,))
    t.start()
    assert entered.wait(timeout=2)
    assert sem.acquire(blocking=False) is False  # held for the duration of start()
    released.set()
    t.join(timeout=2)
    assert sem.acquire(blocking=False) is True  # released once start() returns
