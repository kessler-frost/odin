"""Owner directive B1 -- the pre-apply admission check: sum the Stack's
estimated memory footprint against real host headroom, and free disk on the
store volume, BEFORE Apply spawns anything."""
from __future__ import annotations

from odin.compute.instances import max_env_name_len
from odin.reconcile import admission
from odin.reconcile.admission import (
    CACHE_MEMORY_MIB,
    AdmissionResult,
    check_admission,
    default_memory_budget_mib,
    default_min_disk_gib,
    estimate_stack_memory_mib,
)
from odin.runtime.driver import HostFacts
from odin.spec.models import FieldValue, ResourceDesired, Stack

# --- estimate_stack_memory_mib -----------------------------------------------


def test_empty_stack_estimates_zero():
    assert estimate_stack_memory_mib(Stack()) == 0.0


def test_ec2_uses_the_real_instance_type_memory_table():
    stack = Stack(resources=(
        ResourceDesired(id="web1", kind="ec2", fields={"instanceType": FieldValue(value="t3.medium")}),
    ))
    assert estimate_stack_memory_mib(stack) == 4096.0  # t3.medium == 4GiB


def test_ec2_defaults_to_t3_micro_when_the_field_is_unset():
    stack = Stack(resources=(ResourceDesired(id="web1", kind="ec2"),))
    assert estimate_stack_memory_mib(stack) == 1024.0  # t3.micro == 1GiB


def test_rds_ecs_lambda_are_charged_per_node():
    stack = Stack(resources=(
        ResourceDesired(id="db1", kind="rds"),
        ResourceDesired(id="db2", kind="rds"),
        ResourceDesired(id="task1", kind="ecs"),
        ResourceDesired(id="fn1", kind="lambda"),
    ))
    assert estimate_stack_memory_mib(stack) == 2 * 256.0 + 512.0 + 256.0


def test_elasticache_is_charged_per_node_at_its_real_container_cap():
    # W2.8: each cache cluster is its OWN redis container, capped at
    # aws/cache.py's DEFAULT_MEMORY_MIB -- estimate and real ceiling agree.
    stack = Stack(resources=(
        ResourceDesired(id="c1", kind="elasticache"),
        ResourceDesired(id="c2", kind="elasticache"),
    ))
    assert estimate_stack_memory_mib(stack) == 2 * CACHE_MEMORY_MIB


def test_backing_kinds_are_charged_once_per_env_not_per_node():
    # Ten buckets share ONE RustFS container -- must NOT be 10x the estimate.
    stack = Stack(resources=tuple(ResourceDesired(id=f"bucket{i}", kind="s3") for i in range(10)))
    assert estimate_stack_memory_mib(stack) == 256.0


def test_multiple_distinct_backing_kinds_each_charged_once():
    stack = Stack(resources=(
        ResourceDesired(id="b1", kind="s3"), ResourceDesired(id="b2", kind="s3"),
        ResourceDesired(id="q1", kind="sqs"), ResourceDesired(id="t1", kind="sns"),
        ResourceDesired(id="d1", kind="dynamodb"),
    ))
    assert estimate_stack_memory_mib(stack) == 256.0 + 128.0 + 128.0 + 256.0


def test_zero_footprint_kinds_contribute_nothing():
    stack = Stack(resources=(
        ResourceDesired(id="v1", kind="vpc"), ResourceDesired(id="s1", kind="subnet"),
        ResourceDesired(id="sg1", kind="sg"), ResourceDesired(id="r1", kind="iam_role"),
        ResourceDesired(id="e1", kind="ecr"),
    ))
    assert estimate_stack_memory_mib(stack) == 0.0


# --- default_memory_budget_mib / default_min_disk_gib -----------------------


def test_memory_budget_defaults_to_70_percent_of_total(monkeypatch):
    monkeypatch.delenv("ODIN_MEMORY_BUDGET_MIB", raising=False)
    assert default_memory_budget_mib(10_000.0) == 7_000.0


def test_memory_budget_env_override_wins(monkeypatch):
    monkeypatch.setenv("ODIN_MEMORY_BUDGET_MIB", "2048")
    assert default_memory_budget_mib(10_000.0) == 2048.0


def test_min_disk_gib_defaults_to_10(monkeypatch):
    monkeypatch.delenv("ODIN_MIN_DISK_GIB", raising=False)
    assert default_min_disk_gib() == 10.0


def test_min_disk_gib_env_override_wins(monkeypatch):
    monkeypatch.setenv("ODIN_MIN_DISK_GIB", "25")
    assert default_min_disk_gib() == 25.0


# --- check_admission ----------------------------------------------------------


def test_a_small_stack_on_a_healthy_host_is_admitted(tmp_path, monkeypatch):
    monkeypatch.delenv("ODIN_MEMORY_BUDGET_MIB", raising=False)
    monkeypatch.delenv("ODIN_MIN_DISK_GIB", raising=False)
    stack = Stack(resources=(ResourceDesired(id="uploads", kind="s3"),))
    result = check_admission(stack, HostFacts(total_mem_mib=16_000.0), tmp_path)
    assert result.ok is True
    assert result.reason == ""
    assert result.estimated_mib == 256.0


def test_a_stack_exceeding_the_vm_memory_budget_is_rejected_with_true_numbers(tmp_path, monkeypatch):
    """Field-test 2 finding MEDIUM-9: the rejection was protective but said
    "the admission budget is 4.0 GiB (5.8 GiB total on this host)" on a 48 GiB
    Mac -- 5.8 GiB is COLIMA's VM (`docker info` MemTotal). EC2 instances are
    Lima VMs on the Mac and consume none of Colima's memory, so they must be
    charged against, and the message must quote, REAL host memory."""
    monkeypatch.delenv("ODIN_MEMORY_BUDGET_MIB", raising=False)
    monkeypatch.delenv("ODIN_VM_MEMORY_BUDGET_MIB", raising=False)
    # 20 t3.medium EC2 nodes -> 20 * 4GiB == 80GiB, way past 70% of a 16GiB host.
    stack = Stack(resources=tuple(
        ResourceDesired(id=f"web{i}", kind="ec2", fields={"instanceType": FieldValue(value="t3.medium")})
        for i in range(20)
    ))
    result = check_admission(
        stack, HostFacts(total_mem_mib=8_000.0), tmp_path, host_mem_mib=16_384.0,
    )
    assert result.ok is False
    assert "80.0 GiB" in result.reason
    assert "11.2 GiB" in result.reason  # 70% of the 16 GiB HOST, not of Colima's 8 GiB
    assert "16.0 GiB" in result.reason  # the host total, quoted truthfully
    assert "Colima" not in result.reason, "an EC2 node never touches the container runtime"
    assert "reduce instance sizes or apply fewer nodes" in result.reason
    assert result.vm_mib == 20 * 4096.0
    assert result.container_mib == 0.0


def test_a_stack_exceeding_the_container_memory_budget_names_the_container_runtime(tmp_path, monkeypatch):
    """The other pool: container-backed kinds really do live in Colima's VM, so
    THAT number is the true one to quote for them -- and it must be described as
    what it is, never as "total on this host"."""
    monkeypatch.delenv("ODIN_MEMORY_BUDGET_MIB", raising=False)
    stack = Stack(resources=tuple(ResourceDesired(id=f"task{i}", kind="ecs") for i in range(20)))
    result = check_admission(
        stack, HostFacts(total_mem_mib=5_910.0), tmp_path, host_mem_mib=49_152.0,
    )
    assert result.ok is False
    assert "10.0 GiB" in result.reason  # 20 * 512 MiB
    assert "container runtime" in result.reason
    assert "5.8 GiB" in result.reason  # Colima's VM, honestly labelled
    assert "48.0 GiB total on this host" not in result.reason
    assert result.container_mib == 20 * 512.0
    assert result.vm_mib == 0.0


def test_a_small_ec2_canvas_on_a_big_mac_is_admitted(tmp_path, monkeypatch):
    """The practical harm MEDIUM-9 reported: 5 x t3.micro (5 GiB) is trivial for
    a 48 GiB Mac but was rejected because it was charged against Colima's
    ~5.8 GiB VM, which those VMs never touch."""
    monkeypatch.delenv("ODIN_MEMORY_BUDGET_MIB", raising=False)
    monkeypatch.delenv("ODIN_VM_MEMORY_BUDGET_MIB", raising=False)
    stack = Stack(resources=tuple(
        ResourceDesired(id=f"web{i}", kind="ec2", fields={"instanceType": FieldValue(value="t3.micro")})
        for i in range(5)
    ))
    result = check_admission(
        stack, HostFacts(total_mem_mib=5_910.0), tmp_path, host_mem_mib=49_152.0,
    )
    assert result.ok is True, result.reason


def test_memory_budget_env_override_is_honored_by_check_admission(tmp_path, monkeypatch):
    monkeypatch.setenv("ODIN_MEMORY_BUDGET_MIB", "100")  # absurdly small on purpose
    stack = Stack(resources=(ResourceDesired(id="uploads", kind="s3"),))  # 256 MiB estimate
    result = check_admission(stack, HostFacts(total_mem_mib=64_000.0), tmp_path)
    assert result.ok is False
    assert result.budget_mib == 100.0


def test_vm_memory_budget_env_override_is_honored(tmp_path, monkeypatch):
    monkeypatch.setenv("ODIN_VM_MEMORY_BUDGET_MIB", "512")
    stack = Stack(resources=(ResourceDesired(id="web1", kind="ec2"),))  # t3.micro == 1 GiB
    result = check_admission(stack, HostFacts(total_mem_mib=64_000.0), tmp_path, host_mem_mib=49_152.0)
    assert result.ok is False
    assert result.budget_mib == 512.0
    assert "ODIN_VM_MEMORY_BUDGET_MIB" in result.reason


def test_insufficient_disk_is_rejected_even_when_memory_is_fine(tmp_path, monkeypatch):
    monkeypatch.delenv("ODIN_MEMORY_BUDGET_MIB", raising=False)
    monkeypatch.setattr(admission, "disk_usage", lambda path: type("_U", (), {"free": 2 * 2**30})())
    stack = Stack(resources=(ResourceDesired(id="uploads", kind="s3"),))
    result = check_admission(stack, HostFacts(total_mem_mib=64_000.0), tmp_path)
    assert result.ok is False
    assert "2.0 GiB free disk" in result.reason
    assert "need >10 GiB" in result.reason


def test_min_disk_env_override_is_honored(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, "disk_usage", lambda path: type("_U", (), {"free": 50 * 2**30})())
    monkeypatch.setenv("ODIN_MIN_DISK_GIB", "100")
    stack = Stack(resources=(ResourceDesired(id="uploads", kind="s3"),))
    result = check_admission(stack, HostFacts(total_mem_mib=64_000.0), tmp_path)
    assert result.ok is False
    assert "need >100 GiB" in result.reason


def test_unknown_container_memory_skips_only_the_container_check(tmp_path, monkeypatch):
    # HostFacts() (all zero) is what ensure_host() returns when `docker
    # info` fails (Colima not running) -- and what every test fake that
    # predates this feature returns. Rejecting on a bogus "0 GiB budget"
    # would be actively misleading; Apply fails with a clearer error later.
    # Colima being down says NOTHING about the Mac's RAM, so the VM pool's
    # check is unaffected -- the two pools are independent.
    monkeypatch.delenv("ODIN_MEMORY_BUDGET_MIB", raising=False)
    stack = Stack(resources=tuple(ResourceDesired(id=f"task{i}", kind="ecs") for i in range(50)))
    result = check_admission(stack, HostFacts(total_mem_mib=0.0), tmp_path, host_mem_mib=49_152.0)
    assert result.ok is True


def test_unknown_host_memory_skips_only_the_vm_check(tmp_path, monkeypatch):
    """`os.sysconf` not answering is the VM pool's equivalent of `docker info`
    failing: skip, rather than print a confident wrong number."""
    monkeypatch.delenv("ODIN_MEMORY_BUDGET_MIB", raising=False)
    monkeypatch.delenv("ODIN_VM_MEMORY_BUDGET_MIB", raising=False)
    stack = Stack(resources=tuple(
        ResourceDesired(id=f"web{i}", kind="ec2", fields={"instanceType": FieldValue(value="t3.medium")})
        for i in range(50)  # would obviously blow any real budget
    ))
    result = check_admission(stack, HostFacts(total_mem_mib=64_000.0), tmp_path, host_mem_mib=0.0)
    assert result.ok is True


def test_the_real_host_total_is_read_and_is_not_the_container_runtime_number():
    """The number itself must be real: os.sysconf-derived, no new dependency, no
    subprocess -- and on any machine odin runs on it is a plausible RAM size."""
    total = admission.host_total_mem_mib()
    assert total > 512.0, total  # nobody runs odin on half a gig
    assert total % 1024 == 0 or total > 1024, total


def test_an_explicit_budget_override_still_applies_even_with_unknown_host_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("ODIN_MEMORY_BUDGET_MIB", "100")
    stack = Stack(resources=(ResourceDesired(id="uploads", kind="s3"),))  # 256 MiB estimate
    result = check_admission(stack, HostFacts(total_mem_mib=0.0), tmp_path)
    assert result.ok is False
    assert result.budget_mib == 100.0


def test_ok_result_still_carries_the_numbers_for_display():
    result = AdmissionResult(ok=True, estimated_mib=256.0, budget_mib=7000.0, free_disk_gib=50.0, min_disk_gib=10.0)
    assert result.reason == ""


def test_check_admission_reads_real_free_disk_on_the_given_path(tmp_path):
    # No monkeypatch here -- a real shutil.disk_usage call against tmp_path,
    # proving disk_path (not a hardcoded root) is what's actually checked.
    stack = Stack(resources=(ResourceDesired(id="uploads", kind="s3"),))
    result = check_admission(stack, HostFacts(total_mem_mib=64_000.0), tmp_path)
    assert isinstance(result.free_disk_gib, float) and result.free_disk_gib > 0


def test_check_admission_tolerates_a_store_root_that_does_not_exist_yet(tmp_path):
    # A brand-new install's very first Apply: `.odin/` hasn't been created by
    # anything yet -- disk_usage must walk up to an existing ancestor instead
    # of raising FileNotFoundError.
    never_created = tmp_path / "does-not-exist-yet" / ".odin"
    stack = Stack(resources=(ResourceDesired(id="uploads", kind="s3"),))
    result = check_admission(stack, HostFacts(total_mem_mib=64_000.0), never_created)
    assert isinstance(result.free_disk_gib, float) and result.free_disk_gib > 0


# --- env-name length: the trap that made every EC2 boot fail with a raw
# limactl error, ~60s after Apply, naming nothing the user chose -------------


def _ec2_stack(env: str) -> Stack:
    return Stack(env=env, resources=(ResourceDesired(id="web1", kind="ec2"),))


def _long_env(monkeypatch) -> str:
    """One character past what this machine can boot. Derived from the same
    path arithmetic the check uses -- never a hardcoded 23."""
    monkeypatch.setenv("LIMA_HOME", "/Users/somebody/.lima")
    return "e" * (max_env_name_len() + 1)


def test_an_env_too_long_for_a_lima_vm_name_is_refused_before_anything_boots(tmp_path, monkeypatch):
    env = _long_env(monkeypatch)
    result = check_admission(_ec2_stack(env), HostFacts(total_mem_mib=64_000.0), tmp_path, host_mem_mib=49_152.0)
    assert result.ok is False


def test_the_refusal_names_the_length_the_limit_and_the_real_constraint(tmp_path, monkeypatch):
    """The whole point: the raw limactl error names a socket path and
    UNIX_PATH_MAX, neither of which the user chose. This one has to name what
    they DID choose (the env name and its length), the actual number to get
    under, and why."""
    env = _long_env(monkeypatch)
    limit = max_env_name_len()
    result = check_admission(_ec2_stack(env), HostFacts(total_mem_mib=64_000.0), tmp_path, host_mem_mib=49_152.0)
    assert env in result.reason
    assert str(len(env)) in result.reason
    assert str(limit) in result.reason
    assert "ec2" in result.reason.lower()
    assert "UNIX_PATH_MAX" in result.reason
    assert "LIMA_HOME" in result.reason  # the other way out, for a user who can't rename


def test_a_canvas_with_no_ec2_node_is_never_blocked_by_the_env_name(tmp_path, monkeypatch):
    """Nothing will boot a VM, so nothing can hit the limit -- a long env is
    perfectly fine for a bucket."""
    env = _long_env(monkeypatch)
    stack = Stack(env=env, resources=(ResourceDesired(id="uploads", kind="s3"),))
    result = check_admission(stack, HostFacts(total_mem_mib=64_000.0), tmp_path, host_mem_mib=49_152.0)
    assert result.ok is True, result.reason


def test_an_env_exactly_at_the_limit_is_admitted(tmp_path, monkeypatch):
    """The boundary is inclusive: `max_env_name_len()` characters BOOT
    (verified against a real limactl -- 103 bytes of socket path is accepted,
    104 is not), so refusing it here would be a false alarm."""
    monkeypatch.setenv("LIMA_HOME", "/Users/somebody/.lima")
    env = "e" * max_env_name_len()
    result = check_admission(_ec2_stack(env), HostFacts(total_mem_mib=64_000.0), tmp_path, host_mem_mib=49_152.0)
    assert result.ok is True, result.reason


def test_a_longer_lima_home_makes_a_previously_fine_env_refused(tmp_path, monkeypatch):
    """Machine-specific by construction: the identical canvas is admitted for
    one user and refused for another whose home path is longer. This is why
    the limit is derived rather than hardcoded."""
    monkeypatch.setenv("LIMA_HOME", "/Users/somebody/.lima")
    env = "e" * max_env_name_len()
    facts, disk = HostFacts(total_mem_mib=64_000.0), tmp_path
    assert check_admission(_ec2_stack(env), facts, disk, host_mem_mib=49_152.0).ok is True

    monkeypatch.setenv("LIMA_HOME", "/Users/somebody-with-a-much-longer-name/.lima")
    assert check_admission(_ec2_stack(env), facts, disk, host_mem_mib=49_152.0).ok is False


def test_the_env_name_is_refused_ahead_of_a_memory_rejection(tmp_path, monkeypatch):
    """Ordering, deliberately: an over-budget canvas can be applied after
    freeing RAM, while this one can NEVER work as drawn -- so the terminal
    problem is the one worth naming."""
    env = _long_env(monkeypatch)
    stack = Stack(env=env, resources=tuple(
        ResourceDesired(id=f"web{i}", kind="ec2", fields={"instanceType": FieldValue(value="t3.medium")})
        for i in range(20)
    ))
    result = check_admission(stack, HostFacts(total_mem_mib=64_000.0), tmp_path, host_mem_mib=8_192.0)
    assert result.ok is False
    assert "UNIX_PATH_MAX" in result.reason
