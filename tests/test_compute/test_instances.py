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

from pathlib import Path

import pytest
import yaml

from odin.compute.instances import InstanceVm, NebulaJoin, _Proc, _pick_shared_ip, vm_name
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
    assert config["static_host_map"] == {"10.42.0.1": ["192.168.64.1:4242"]}
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
