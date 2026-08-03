from __future__ import annotations

# Pinned to the SAME version this dev machine's `brew install nebula` carries
# (verified: `brew list nebula` -> 1.10.3) -- host lighthouse and VM daemon
# must speak the same wire protocol. A hardcoded version string matches this
# file's own existing NERDCTL_VERSION/BUILDKIT_VERSION convention below.
NEBULA_VERSION = "1.10.3"

# The markers delimiting the ONLY part of a VM's /etc/hosts odin owns.
#
# /etc/hosts is NOT odin's file, which is what makes this different from the
# `/etc/environment` section below. That file odin writes wholesale; this one
# arrives already carrying the image's loopback entries and the hostname line
# Lima sets, and destroying those breaks name resolution for the whole guest.
# So odin claims a delimited block and rewrites exactly that, leaving every
# other line untouched.
HOSTS_BEGIN = "# ODIN-ROUTE53-BEGIN"
HOSTS_END = "# ODIN-ROUTE53-END"


def hosts_block_script(hosts: dict[str, str]) -> str:
    """Bash that makes odin's block in `/etc/hosts` say exactly `hosts`.

    DELETE-THEN-APPEND, never append alone, and that is the same lesson the
    `env_vars` section below records: this runs as a Lima PER-BOOT provision
    script, so an append would duplicate every entry on every boot. Deleting
    the old block first also makes REMOVAL work -- a record taken off the
    canvas stops resolving, which an append-only writer could never achieve.

    Emitting the delete unconditionally (even for an empty `hosts`) is what
    makes "the last record was removed" a real state rather than a no-op.

    The SAME function renders the boot path (`generate_cloud_init`) and the
    in-place update path (`InstanceVm.push_hosts`) on purpose: identical
    inputs must produce identical bytes, or the "did anything actually
    change?" comparison that keeps an Apply churn-free would fire every time.
    `fabric/nebula.py`'s `_render_config` is the same idea for the same
    reason.

    Sorted by name so the bytes are a function of the RECORD SET and not of
    the store's iteration order.
    """
    for name, address in hosts.items():
        # A name or address carrying whitespace would write a hosts line that
        # means something else entirely, and a newline would write an arbitrary
        # extra line into a system file. Refuse rather than mangle: this is the
        # boundary where canvas-shaped text becomes a file the resolver obeys.
        if not name or not address or any(c.isspace() for c in f"{name}{address}"):
            raise ValueError(
                f"refusing to write an /etc/hosts entry from {name!r} -> {address!r}: "
                "a hosts name and address must both be non-empty and whitespace-free"
            )
    entries = "\n".join(f"{address}\t{name}" for name, address in sorted(hosts.items()))
    return "\n".join([
        f"sed -i '/^{HOSTS_BEGIN}$/,/^{HOSTS_END}$/d' /etc/hosts",
        "cat >> /etc/hosts << 'ODIN_ETC_HOSTS'",
        HOSTS_BEGIN,
        *([entries] if entries else []),
        HOSTS_END,
        "ODIN_ETC_HOSTS",
        # FLUSH THE GUEST RESOLVER, and this line is a measured bug fix rather
        # than defensive housekeeping. Writing the file is NOT the same as the
        # name changing: `systemd-resolved` is active on the stock Lima image
        # (`nsswitch` is `hosts: files dns`) and it caches what /etc/hosts said.
        #
        # MEASURED on a real VM, 2026-08-03. Boot with a record, then push an
        # EMPTY set: the file is correctly emptied (`grep -c` -> 0) and
        # `push_hosts` returns "pushed" -- while `getent hosts api.internal`
        # keeps answering `10.42.0.5` for a further **2.2 seconds**. That is odin
        # reporting an outcome it has not achieved yet, which is honesty rule 2
        # in the smallest possible window, and a withdrawn record that still
        # resolves is the one thing this feature must never do.
        #
        # With the flush the same sequence is NO-RESOLVE immediately.
        #
        # Guarded and non-fatal on purpose: a guest without systemd-resolved has
        # nothing to flush and must not fail the push for it, and this script
        # also runs as a per-boot provision step under `set -ux` with no
        # `set -e`, where a non-zero exit would leave `limactl start` waiting.
        "command -v resolvectl >/dev/null 2>&1 && resolvectl flush-caches || true",
    ])


def generate_cloud_init(
    hostname: str,
    ssh_pubkey: str | None = None,
    install_nerdctl: bool = False,
    install_nebula: bool = False,
    extra_script: str | None = None,
    env_vars: dict[str, str] | None = None,
    hosts: dict[str, str] | None = None,
) -> str:
    lines = [
        "#!/bin/bash",
        # NOT `set -e`: this runs as a Lima per-boot provision script, and if any
        # one command fails the whole boot script fails — which leaves
        # `limactl start` waiting forever for a readiness it never gets.
        "set -ux",
        "",
        f"hostnamectl set-hostname {hostname} || true",
    ]

    if ssh_pubkey:
        # Detect the VM's regular user at runtime (UID 1000). Lima warns against
        # referencing LIMA_CIDATA_* in provision scripts (and they're undefined
        # there), which left provisioning unfinished so `limactl start` hung.
        lines.extend([
            "",
            "# Install SSH public key",
            'LIMA_USER="$(getent passwd 1000 | cut -d: -f1)"',
            'mkdir -p "/home/${LIMA_USER}/.ssh"',
            f'echo "{ssh_pubkey}" >> "/home/${{LIMA_USER}}/.ssh/authorized_keys"',
            'chown -R "${LIMA_USER}:${LIMA_USER}" "/home/${LIMA_USER}/.ssh"',
            'chmod 700 "/home/${LIMA_USER}/.ssh"',
            'chmod 600 "/home/${LIMA_USER}/.ssh/authorized_keys"',
        ])

    if env_vars:
        # Workload identity (gateway/keys.py::workload_env): the four AWS-SDK
        # env vars land system-wide in /etc/environment (overwritten, not
        # appended -- this is a per-boot provision script, appending would
        # duplicate lines every boot) AND the credential pair lands in the
        # default user's ~/.aws/credentials ([default] holds ONLY the two
        # standard credentials-file keys; endpoint/region aren't valid there).
        # Computes its OWN LIMA_USER: this section and the SSH-key one are
        # independent conditionals -- either can run without the other.
        environment_lines = "\n".join(f"{key}={value}" for key, value in env_vars.items())
        lines.extend([
            "",
            "# Inject AWS credentials + endpoint (odin gateway workload identity)",
            "cat > /etc/environment << 'ODIN_ETC_ENVIRONMENT'",
            environment_lines,
            "ODIN_ETC_ENVIRONMENT",
            'LIMA_USER="$(getent passwd 1000 | cut -d: -f1)"',
            'mkdir -p "/home/${LIMA_USER}/.aws"',
            'cat > "/home/${LIMA_USER}/.aws/credentials" << \'ODIN_AWS_CREDENTIALS\'',
            "[default]",
            f"aws_access_key_id={env_vars.get('AWS_ACCESS_KEY_ID', '')}",
            f"aws_secret_access_key={env_vars.get('AWS_SECRET_ACCESS_KEY', '')}",
            "ODIN_AWS_CREDENTIALS",
            'chown -R "${LIMA_USER}:${LIMA_USER}" "/home/${LIMA_USER}/.aws"',
            'chmod 600 "/home/${LIMA_USER}/.aws/credentials"',
        ])

    if hosts is not None:
        # BEFORE `extra_script` (the instance's own UserData) deliberately: a
        # boot script that dials a drawn name must find it already resolving,
        # or the record is real everywhere except the one place the user put
        # their code. `is not None` rather than truthiness -- an EMPTY map is a
        # meaningful instruction ("this VM should resolve nothing of odin's"),
        # and it is how the last record being removed reaches the guest.
        lines.extend([
            "",
            "# Resolve this env's route53 records (odin-owned block only)",
            hosts_block_script(hosts),
        ])

    if install_nerdctl:
        lines.extend([
            "",
            "# Install containerd + nerdctl",
            "apt-get update -qq",
            "apt-get install -y -qq containerd",
            "systemctl enable --now containerd",
            'ARCH=$(uname -m)',
            'case $ARCH in aarch64|arm64) ARCH="arm64" ;; x86_64) ARCH="amd64" ;; esac',
            'NERDCTL_VERSION="2.0.3"',
            'curl -fsSL -o /tmp/nerdctl.tar.gz '
            '"https://github.com/containerd/nerdctl/releases/download/v${NERDCTL_VERSION}/nerdctl-${NERDCTL_VERSION}-linux-${ARCH}.tar.gz"',
            'tar -xzf /tmp/nerdctl.tar.gz -C /usr/local/bin nerdctl',
            'chmod +x /usr/local/bin/nerdctl',
            'rm /tmp/nerdctl.tar.gz',
            '',
            '# Install buildkit for nerdctl build',
            'BUILDKIT_VERSION="0.19.0"',
            'curl -fsSL -o /tmp/buildkit.tar.gz '
            '"https://github.com/moby/buildkit/releases/download/v${BUILDKIT_VERSION}/buildkit-v${BUILDKIT_VERSION}.linux-${ARCH}.tar.gz"',
            'tar -xzf /tmp/buildkit.tar.gz -C /usr/local --strip-components=1 bin/buildkitd bin/buildctl',
            'rm /tmp/buildkit.tar.gz',
            '',
            '# Create and start buildkitd systemd service',
            'cat > /etc/systemd/system/buildkitd.service << \'ODIN_BUILDKIT_UNIT\'',
            '[Unit]',
            'Description=BuildKit Daemon',
            'After=containerd.service',
            '[Service]',
            'ExecStart=/usr/local/bin/buildkitd --oci-worker-no-process-sandbox',
            'Restart=always',
            '[Install]',
            'WantedBy=multi-user.target',
            'ODIN_BUILDKIT_UNIT',
            'systemctl daemon-reload',
            'systemctl enable --now buildkitd',
        ])

    if install_nebula:
        lines.extend([
            "",
            "# Install the Nebula overlay-network binary (MIT, slackhq/nebula) --",
            "# same download-a-release-tarball shape as nerdctl/buildkit above.",
            "# The systemd unit is registered but NOT started here: its",
            "# config.yml (the real underlay address + the VPC's compiled SG",
            "# firewall) is only knowable once the VM is up and vzNAT-networked --",
            "# InstanceVm._activate_nebula writes it and starts the daemon",
            "# post-boot (compute/instances.py).",
            'ARCH=$(uname -m)',
            'case $ARCH in aarch64|arm64) ARCH="arm64" ;; x86_64) ARCH="amd64" ;; esac',
            f'NEBULA_VERSION="{NEBULA_VERSION}"',
            'curl -fsSL -o /tmp/nebula.tar.gz '
            '"https://github.com/slackhq/nebula/releases/download/v${NEBULA_VERSION}/nebula-linux-${ARCH}.tar.gz"',
            'tar -xzf /tmp/nebula.tar.gz -C /usr/local/bin nebula',
            'chmod +x /usr/local/bin/nebula',
            'rm /tmp/nebula.tar.gz',
            'mkdir -p /etc/nebula',
            '',
            '# Registered, not enabled -- see comment above.',
            'cat > /etc/systemd/system/nebula.service << \'ODIN_NEBULA_UNIT\'',
            '[Unit]',
            'Description=Nebula overlay network',
            'After=network-online.target',
            'Wants=network-online.target',
            '[Service]',
            'ExecStart=/usr/local/bin/nebula -config /etc/nebula/config.yml',
            'Restart=always',
            '[Install]',
            'WantedBy=multi-user.target',
            'ODIN_NEBULA_UNIT',
            'systemctl daemon-reload',
        ])

    if extra_script:
        # V3: compute/instances.py composes this from two sources -- an
        # instance's Nebula cert/config files (written verbatim, no download)
        # and/or its RunInstances `UserData` (already base64-decoded by the
        # caller) -- both are plain bash appended after the base provisioning
        # above, one script, one Lima `provision` entry.
        lines.extend(["", "# Extra provisioning (Nebula join / instance user_data)", extra_script])

    return "\n".join(lines) + "\n"
