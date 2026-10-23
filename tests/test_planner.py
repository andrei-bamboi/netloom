import pytest

from netloom.errors import ValidationFailed
from netloom.planner import build_plan


def problems(request):
    with pytest.raises(ValidationFailed) as exc:
        build_plan(request)
    return {p["field"]: p["message"] for p in exc.value.details}


def test_layout_carves_tiers_by_zone():
    plan = build_plan(
        {"name": "demo", "cidr": "10.20.0.0/16", "layout": {"tiers": ["public", "private"], "zones": 3, "prefix": 20}}
    )
    assert [(s.name, s.cidr) for s in plan.subnets] == [
        ("public-a", "10.20.0.0/20"),
        ("public-b", "10.20.16.0/20"),
        ("public-c", "10.20.32.0/20"),
        ("private-a", "10.20.48.0/20"),
        ("private-b", "10.20.64.0/20"),
        ("private-c", "10.20.80.0/20"),
    ]
    assert plan.needs_internet_gateway
    assert plan.subnets[0].usable_ips == 4096 - 5


def test_layout_without_prefix_uses_largest_fit():
    plan = build_plan({"name": "auto", "cidr": "10.0.0.0/24", "layout": {"tiers": ["private"], "zones": 3}})
    assert [s.cidr for s in plan.subnets] == ["10.0.0.0/26", "10.0.0.64/26", "10.0.0.128/26"]
    assert not plan.needs_internet_gateway
    assert plan.summary()["unallocated_addresses"] == 64


def test_explicit_subnets_accepted():
    plan = build_plan(
        {
            "name": "x",
            "cidr": "172.16.0.0/20",
            "subnets": [
                {"name": "web", "cidr": "172.16.0.0/24", "zone": "a", "tier": "public"},
                {"name": "db", "cidr": "172.16.8.0/22", "zone": "b"},
            ],
            "tags": {"team": "platform"},
        }
    )
    assert [s.tier for s in plan.subnets] == ["public", "private"]
    assert plan.tags == {"team": "platform"}


def test_reports_all_problems_at_once():
    found = problems(
        {
            "name": "ok",
            "cidr": "10.0.0.0/16",
            "subnets": [
                {"name": "a", "cidr": "10.0.0.0/24"},
                {"name": "a", "cidr": "10.0.0.128/25"},
                {"name": "c", "cidr": "10.1.0.0/24", "tier": "dmz"},
            ],
        }
    )
    assert "overlaps" in found["subnets[1].cidr"]
    assert "duplicates" in found["subnets[1].name"]
    assert "outside the VPC" in found["subnets[2].cidr"]
    assert "subnets[2].tier" in found


@pytest.mark.parametrize(
    "cidr,fragment",
    [
        ("8.8.8.0/24", "RFC 1918"),
        ("10.0.0.0/8", "prefix length"),
        ("10.0.0.1/24", "not a valid"),
        ("not-a-cidr", "not a valid"),
        (42, "CIDR string"),
    ],
)
def test_bad_vpc_cidr(cidr, fragment):
    assert fragment in problems({"name": "n", "cidr": cidr, "layout": {}})["cidr"]


def test_must_choose_exactly_one_style():
    assert "exactly one" in problems({"name": "n", "cidr": "10.0.0.0/16"})["$"]
    both = {"name": "n", "cidr": "10.0.0.0/16", "layout": {}, "subnets": []}
    assert "exactly one" in problems(both)["$"]


def test_layout_that_does_not_fit():
    found = problems({"name": "n", "cidr": "10.0.0.0/28", "layout": {"tiers": ["private"], "zones": 2, "prefix": 28}})
    assert "only fits 1" in found["layout"]


def test_reserved_tags_rejected():
    found = problems({"name": "n", "cidr": "10.0.0.0/16", "layout": {}, "tags": {"netloom:managed": "x", "Name": "y"}})
    assert "reserved" in found["tags.netloom:managed"]
    assert "reserved" in found["tags.Name"]


def test_subnet_limit():
    subnets = [{"cidr": f"10.0.{i}.0/24"} for i in range(17)]
    assert "at most 16" in problems({"name": "n", "cidr": "10.0.0.0/16", "subnets": subnets})["subnets"]
