"""`LimaRuntime` can be bound to a NAMED VM, not only the shared one.

The unlocking change for real ECS-on-EC2 placement (owner's intelligence-layer
ask). `VM` was a class constant, so every `LimaRuntime` anywhere pointed at the
one shared `odin-host` VM. That is correct for odin's "VM isolation" runtime
mode and is exactly what prevented an ECS task drawn inside an EC2 box from
running inside THAT instance's VM (`odin-ec2-<env>-<id>`), which is a different
VM per node.

The two meanings are distinguished by the NAME rather than by the type, so these
tests pin that the default has not moved -- every existing caller and the whole
runtime-isolation mode depend on `LimaRuntime()` still being the shared host.
"""
from __future__ import annotations

from odin.runtime.lima import LimaRuntime


def test_the_default_is_still_the_shared_host_vm():
    """Changing this silently would repoint odin's VM-isolation runtime mode."""
    assert LimaRuntime().VM == "odin-host"
    assert LimaRuntime.DEFAULT_VM == "odin-host"


def test_a_named_vm_is_used_instead():
    assert LimaRuntime(vm="odin-ec2-prod-api-server").VM == "odin-ec2-prod-api-server"


def test_instances_do_not_share_a_vm():
    """The actual bug a class constant would have caused: two runtimes pointed
    at different instances would both drive whichever was assigned last."""
    shared, first, second = LimaRuntime(), LimaRuntime(vm="vm-a"), LimaRuntime(vm="vm-b")
    assert (shared.VM, first.VM, second.VM) == ("odin-host", "vm-a", "vm-b")


def test_an_empty_name_falls_back_rather_than_targeting_nothing():
    """`vm=""` from an unset field must not produce `limactl shell ""`."""
    assert LimaRuntime(vm="").VM == "odin-host"
    assert LimaRuntime(vm=None).VM == "odin-host"


def test_the_named_vm_reaches_the_actual_command():
    """Pins the seam end to end: the name has to appear in the argv, not just on
    the object. `_argv` is what every container call goes through."""
    argv = LimaRuntime(vm="odin-ec2-prod-web")._argv("ps", "-a")
    assert argv[:3] == ["limactl", "shell", "odin-ec2-prod-web"], argv
    assert argv[-2:] == ["ps", "-a"]
    assert LimaRuntime()._argv("ps")[2] == "odin-host"
