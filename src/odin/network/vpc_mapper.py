from __future__ import annotations

from odin.network.models import FirewallRule, FirewallRules


def sg_rules_to_firewall(permissions: list[dict]) -> FirewallRules:
    """Translate AWS security group IpPermissions to Nebula firewall rules."""
    inbound: list[FirewallRule] = []

    for perm in permissions:
        proto = perm.get("IpProtocol", "-1")
        from_port = perm.get("FromPort")
        to_port = perm.get("ToPort")

        nebula_proto = "any" if proto == "-1" else proto
        nebula_port = "any"
        if proto != "-1" and from_port is not None:
            nebula_port = str(from_port) if from_port == to_port else f"{from_port}-{to_port}"

        for ip_range in perm.get("IpRanges", []):
            inbound.append(FirewallRule(
                port=nebula_port, proto=nebula_proto, cidr=ip_range.get("CidrIp"),
            ))

        for group_ref in perm.get("UserIdGroupPairs", []):
            inbound.append(FirewallRule(
                port=nebula_port, proto=nebula_proto, group=group_ref.get("GroupId", ""),
            ))

        if not perm.get("IpRanges") and not perm.get("UserIdGroupPairs"):
            inbound.append(FirewallRule(port=nebula_port, proto=nebula_proto))

    outbound = [FirewallRule(port="any", proto="any")]
    return FirewallRules(inbound=inbound, outbound=outbound)


LIGHTHOUSE_FIREWALL = FirewallRules(
    inbound=[FirewallRule(port="any", proto="any")],
    outbound=[FirewallRule(port="any", proto="any")],
)
