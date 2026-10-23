"""Network planning: turns a user request into a validated, concrete blueprint.

This module is pure (no AWS calls) so it is fully unit-testable and is also
exposed through ``POST /v1/plans`` as a dry-run.

Two request styles are supported:

* explicit - the caller lists every subnet with its own CIDR
* layout   - the caller describes tiers x zones and a prefix length, and the
             planner carves consecutive blocks out of the VPC range
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import asdict, dataclass, field

from .errors import ValidationFailed

PRIVATE_RANGES = tuple(ipaddress.ip_network(c) for c in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
AWS_RESERVED_PER_SUBNET = 5  # network, router, DNS, future use, broadcast
MIN_PREFIX, MAX_PREFIX = 16, 28  # AWS limits for VPC and subnet blocks
TIERS = ("public", "private")
ZONE_LETTERS = "abcdef"
MAX_TAGS = 20
RESERVED_TAG_PREFIXES = ("aws:", "netloom:")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$")


@dataclass(frozen=True)
class SubnetPlan:
    key: str
    name: str
    cidr: str
    zone: str  # logical zone letter; resolved to a real AZ at provisioning time
    tier: str
    usable_ips: int


@dataclass(frozen=True)
class NetworkPlan:
    name: str
    cidr: str
    tags: dict[str, str]
    subnets: list[SubnetPlan] = field(default_factory=list)

    @property
    def needs_internet_gateway(self) -> bool:
        return any(s.tier == "public" for s in self.subnets)

    def summary(self) -> dict:
        vpc = ipaddress.ip_network(self.cidr)
        allocated = sum(ipaddress.ip_network(s.cidr).num_addresses for s in self.subnets)
        return {
            "vpc_addresses": vpc.num_addresses,
            "allocated_addresses": allocated,
            "unallocated_addresses": vpc.num_addresses - allocated,
            "subnet_count": len(self.subnets),
            "zones": sorted({s.zone for s in self.subnets}),
            "internet_gateway": self.needs_internet_gateway,
        }

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "cidr": self.cidr,
            "tags": dict(self.tags),
            "subnets": [asdict(s) for s in self.subnets],
            "summary": self.summary(),
        }


class _Problems:
    """Collects every validation problem so the caller gets them all at once."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, field_path: str, message: str) -> None:
        self.items.append({"field": field_path, "message": message})

    def raise_if_any(self) -> None:
        if self.items:
            raise ValidationFailed("The request is not a valid network definition.", details=self.items)


def _parse_block(value: object, path: str, problems: _Problems) -> ipaddress.IPv4Network | None:
    if not isinstance(value, str):
        problems.add(path, "must be a CIDR string such as 10.0.0.0/16")
        return None
    try:
        net = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        problems.add(path, f"is not a valid IPv4 network ({exc})")
        return None
    if net.version != 4:
        problems.add(path, "only IPv4 is supported")
        return None
    if not MIN_PREFIX <= net.prefixlen <= MAX_PREFIX:
        problems.add(path, f"prefix length must be between /{MIN_PREFIX} and /{MAX_PREFIX}")
        return None
    return net


def _check_name(value: object, path: str, problems: _Problems) -> str | None:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        problems.add(path, "must be 1-63 characters: letters, digits, space, '.', '_' or '-'")
        return None
    return value


def _check_tags(value: object, problems: _Problems) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.add("tags", "must be an object of string keys and values")
        return {}
    if len(value) > MAX_TAGS:
        problems.add("tags", f"at most {MAX_TAGS} tags are allowed")
    clean: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not 1 <= len(k) <= 128:
            problems.add(f"tags.{k}", "key must be 1-128 characters")
            continue
        if k.lower().startswith(RESERVED_TAG_PREFIXES) or k == "Name":
            problems.add(f"tags.{k}", "key is reserved by netloom or AWS")
            continue
        if not isinstance(v, str) or len(v) > 256:
            problems.add(f"tags.{k}", "value must be a string of at most 256 characters")
            continue
        clean[k] = v
    return clean


def _explicit_subnets(raw: object, vpc: ipaddress.IPv4Network, max_subnets: int, problems: _Problems) -> list[SubnetPlan]:
    if not isinstance(raw, list) or not raw:
        problems.add("subnets", "must be a non-empty list")
        return []
    if len(raw) > max_subnets:
        problems.add("subnets", f"at most {max_subnets} subnets are allowed")
        return []

    plans: list[SubnetPlan] = []
    seen_names: set[str] = set()
    accepted: list[tuple[ipaddress.IPv4Network, str]] = []
    for i, item in enumerate(raw):
        path = f"subnets[{i}]"
        if not isinstance(item, dict):
            problems.add(path, "must be an object")
            continue
        tier = item.get("tier", "private")
        if tier not in TIERS:
            problems.add(f"{path}.tier", f"must be one of {', '.join(TIERS)}")
        zone = item.get("zone", ZONE_LETTERS[i % 2])
        if not (isinstance(zone, str) and len(zone) == 1 and zone in ZONE_LETTERS):
            problems.add(f"{path}.zone", f"must be a single letter in '{ZONE_LETTERS}'")
        name = _check_name(item.get("name", f"{tier}-{zone}-{i + 1}"), f"{path}.name", problems)
        if name and name in seen_names:
            problems.add(f"{path}.name", "duplicates another subnet name")
        if name:
            seen_names.add(name)

        block = _parse_block(item.get("cidr"), f"{path}.cidr", problems)
        if block is None:
            continue
        if not block.subnet_of(vpc):
            problems.add(f"{path}.cidr", f"{block} is outside the VPC range {vpc}")
            continue
        clash = next((other for other, _ in accepted if block.overlaps(other)), None)
        if clash is not None:
            owner = next(n for b, n in accepted if b == clash)
            problems.add(f"{path}.cidr", f"{block} overlaps {clash} ({owner})")
            continue
        accepted.append((block, name or path))
        plans.append(
            SubnetPlan(
                key=f"{i + 1:02d}",
                name=name or "",
                cidr=str(block),
                zone=zone if isinstance(zone, str) else "a",
                tier=tier if tier in TIERS else "private",
                usable_ips=block.num_addresses - AWS_RESERVED_PER_SUBNET,
            )
        )
    return plans


def _layout_subnets(raw: object, vpc: ipaddress.IPv4Network, max_subnets: int, problems: _Problems) -> list[SubnetPlan]:
    if not isinstance(raw, dict):
        problems.add("layout", "must be an object with tiers, zones and prefix")
        return []
    tiers = raw.get("tiers", ["public", "private"])
    zones = raw.get("zones", 2)
    prefix = raw.get("prefix")

    if not isinstance(tiers, list) or not tiers or any(t not in TIERS for t in tiers) or len(set(tiers)) != len(tiers):
        problems.add("layout.tiers", f"must be a non-empty list of distinct values from {list(TIERS)}")
        return []
    if not isinstance(zones, int) or isinstance(zones, bool) or not 1 <= zones <= len(ZONE_LETTERS):
        problems.add("layout.zones", f"must be an integer between 1 and {len(ZONE_LETTERS)}")
        return []
    count = len(tiers) * zones
    if count > max_subnets:
        problems.add("layout", f"{count} subnets requested; at most {max_subnets} are allowed")
        return []
    if prefix is None:
        # Pick the largest equal-size blocks that still fit every subnet.
        prefix = vpc.prefixlen + max(1, (count - 1).bit_length())
    if not isinstance(prefix, int) or isinstance(prefix, bool) or not vpc.prefixlen <= prefix <= MAX_PREFIX:
        problems.add("layout.prefix", f"must be an integer between /{vpc.prefixlen} and /{MAX_PREFIX}")
        return []

    available = 2 ** (prefix - vpc.prefixlen)
    if count > available:
        problems.add("layout", f"{vpc} only fits {available} x /{prefix} blocks but {count} are needed")
        return []

    blocks = vpc.subnets(new_prefix=prefix)
    plans: list[SubnetPlan] = []
    for tier in tiers:
        for z in range(zones):
            block = next(blocks)
            letter = ZONE_LETTERS[z]
            plans.append(
                SubnetPlan(
                    key=f"{len(plans) + 1:02d}",
                    name=f"{tier}-{letter}",
                    cidr=str(block),
                    zone=letter,
                    tier=tier,
                    usable_ips=block.num_addresses - AWS_RESERVED_PER_SUBNET,
                )
            )
    return plans


def build_plan(request: object, *, max_subnets: int = 16) -> NetworkPlan:
    """Validate a create-network request and return a concrete plan.

    Raises ``ValidationFailed`` listing every problem found.
    """
    problems = _Problems()
    if not isinstance(request, dict):
        problems.add("$", "request body must be a JSON object")
        problems.raise_if_any()

    name = _check_name(request.get("name"), "name", problems)
    vpc = _parse_block(request.get("cidr"), "cidr", problems)
    if vpc is not None and not any(vpc.subnet_of(r) for r in PRIVATE_RANGES):
        problems.add("cidr", "must be inside an RFC 1918 private range (10/8, 172.16/12, 192.168/16)")
        vpc = None
    tags = _check_tags(request.get("tags"), problems)

    has_explicit, has_layout = "subnets" in request, "layout" in request
    if has_explicit == has_layout:
        problems.add("$", "provide exactly one of 'subnets' (explicit) or 'layout' (auto-carve)")

    subnets: list[SubnetPlan] = []
    if vpc is not None and has_explicit != has_layout:
        if has_explicit:
            subnets = _explicit_subnets(request["subnets"], vpc, max_subnets, problems)
        else:
            subnets = _layout_subnets(request["layout"], vpc, max_subnets, problems)

    problems.raise_if_any()
    return NetworkPlan(name=name or "", cidr=str(vpc), tags=tags, subnets=subnets)
