"""Aggregate views: the wall of vows, the counters, and the public standing."""

import json

PROMISE = "Ship v1.0 of the console to production."
EVIDENCE_URL = "https://evidence.example.com/releases/v1"
CRITERIA = "A published release tag on the page counts as shipped."

EVIDENCE_BODY = (
    "Release notes. On 3 March the team published v1.0.0 to production. "
    "The console has been live since that morning."
)
QUOTE = "On 3 March the team published v1.0.0 to production."

DAY = 86400


def _days(count):
    """Deadlines are seconds on-chain; the tests still read in days."""
    return count * DAY


def _hex(addr):
    """Address fixtures arrive as bytes or Address depending on the SDK load."""
    if hasattr(addr, "as_hex"):
        return str(addr.as_hex).lower().replace("0x", "")
    return addr.hex().lower().replace("0x", "")


def _mock_page(direct_vm):
    direct_vm.mock_web(
        r".*evidence\.example\.com.*", {"status": 200, "body": EVIDENCE_BODY}
    )


def _mock_jury(direct_vm, verdict):
    direct_vm.mock_llm(
        r".*\[\[VOW_SETTLEMENT\]\].*",
        json.dumps({"verdict": verdict, "quote": QUOTE, "reason": "the page decides it"}),
    )


def test_empty_contract(direct_deploy):
    contract = direct_deploy("contracts/Vow.py")

    assert contract.get_vows() == {}
    assert contract.get_vow_ids()["ids"] == []
    assert contract.is_paused() is False
    assert contract.get_ledger() == {"total_in": 0, "total_out": 0, "locked_total": 0}
    assert int(contract.get_balance()) >= 0

    summary = contract.get_summary()
    assert summary["vows"] == 0
    assert summary["active"] == 0
    assert summary["kept"] == 0
    assert summary["broken"] == 0
    assert summary["abandoned"] == 0
    assert summary["void"] == 0


def test_vows_are_listed(direct_vm, direct_deploy, direct_owner, direct_alice):
    contract = direct_deploy("contracts/Vow.py")
    direct_vm.sender = direct_owner

    contract.make_vow(PROMISE, EVIDENCE_URL, "", _days(3), direct_alice, "ref-a")
    contract.make_vow(PROMISE, EVIDENCE_URL, "", _days(3), direct_alice, "ref-b")

    vows = contract.get_vows()
    assert len(vows) == 2
    assert vows["vw-1"]["promise"] == PROMISE
    assert vows["vw-2"]["status"] == "ACTIVE"


def test_summary_counts_outcomes(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract = direct_deploy("contracts/Vow.py")
    direct_vm.sender = direct_owner

    kept_vow = contract.make_vow(PROMISE, EVIDENCE_URL, CRITERIA, _days(1), direct_alice, "ref-1")
    broken_vow = contract.make_vow(PROMISE, EVIDENCE_URL, CRITERIA, _days(1), direct_alice, "ref-2")
    abandoned_vow = contract.make_vow(
        PROMISE, EVIDENCE_URL, CRITERIA, _days(30), direct_alice, "ref-3"
    )

    direct_vm.warp("2025-01-03T00:00:00Z")

    _mock_page(direct_vm)
    _mock_jury(direct_vm, "KEPT")
    contract.settle(kept_vow)

    direct_vm.clear_mocks()
    _mock_page(direct_vm)
    _mock_jury(direct_vm, "BROKEN")
    contract.settle(broken_vow)

    contract.abandon(abandoned_vow)

    summary = contract.get_summary()
    assert summary["vows"] == 3
    assert summary["kept"] == 1
    assert summary["broken"] == 1
    assert summary["abandoned"] == 1
    assert summary["active"] == 0


def test_standing_needs_a_track_record(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract = direct_deploy("contracts/Vow.py")
    direct_vm.sender = direct_owner

    kept_vow = contract.make_vow(PROMISE, EVIDENCE_URL, CRITERIA, _days(1), direct_alice, "ref-1")
    broken_vow = contract.make_vow(PROMISE, EVIDENCE_URL, CRITERIA, _days(1), direct_alice, "ref-2")
    abandoned_vow = contract.make_vow(
        PROMISE, EVIDENCE_URL, CRITERIA, _days(30), direct_alice, "ref-3"
    )

    direct_vm.warp("2025-01-03T00:00:00Z")

    _mock_page(direct_vm)
    _mock_jury(direct_vm, "KEPT")
    contract.settle(kept_vow)

    # One kept promise is not proof yet.
    first = contract.get_standing(direct_owner)
    assert first["resolved"] == 1
    assert first["kept_rate_bps"] == 10000
    assert first["standing"] == "UNPROVEN"

    direct_vm.clear_mocks()
    _mock_page(direct_vm)
    _mock_jury(direct_vm, "BROKEN")
    contract.settle(broken_vow)

    contract.abandon(abandoned_vow)

    standing = contract.get_standing(direct_owner)
    assert standing["kept"] == 1
    assert standing["broken"] == 1
    assert standing["abandoned"] == 1
    assert standing["resolved"] == 3
    assert standing["active"] == 0
    assert standing["kept_rate_bps"] == 3333
    assert standing["standing"] == "HOLLOW"


def test_standings_of_unknown_addresses_are_empty(direct_deploy, direct_bob):
    contract = direct_deploy("contracts/Vow.py")

    standing = contract.get_standing(direct_bob)
    assert standing["kept"] == 0
    assert standing["broken"] == 0
    assert standing["active"] == 0
    assert standing["resolved"] == 0
    assert standing["standing"] == "UNPROVEN"
    assert standing["credit"] == 0


def test_rules_view_reports_the_bounds(direct_deploy):
    contract = direct_deploy("contracts/Vow.py")

    rules = contract.get_rules()
    assert rules["min_deadline_seconds"] == 60
    assert rules["max_deadline_seconds"] == 365 * 86400
    assert rules["settle_bounty_bps"] == 200
    assert rules["max_active_per_promiser"] == 20
    assert rules["max_settlement_attempts"] == 5
    assert rules["max_promise_chars"] == 400


def test_is_due_follows_the_clock(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract = direct_deploy("contracts/Vow.py")
    direct_vm.sender = direct_owner

    vow_id = contract.make_vow(PROMISE, EVIDENCE_URL, "", _days(2), direct_alice, "ref-1")

    assert contract.is_due(vow_id) is False

    direct_vm.warp("2025-01-02T00:00:00Z")
    assert contract.is_due(vow_id) is False

    direct_vm.warp("2025-01-04T00:00:00Z")
    assert contract.is_due(vow_id) is True


def test_unknown_vow_view_reverts(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/Vow.py")

    with direct_vm.expect_revert("[EXPECTED]:unknown_vow"):
        contract.get_vow("vw-99")

    with direct_vm.expect_revert("[EXPECTED]:unknown_vow"):
        contract.is_due("vw-99")


def test_ownership_moves_in_two_steps(
    direct_vm, direct_deploy, direct_owner, direct_alice, direct_bob
):
    contract = direct_deploy("contracts/Vow.py")
    direct_vm.sender = direct_owner

    contract.transfer_ownership(direct_alice)
    assert contract.get_pending_owner().lower().replace("0x", "") == _hex(direct_alice)

    # A nomination is not a handover: only the nominee can accept.
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("[EXPECTED]:not_pending_owner"):
        contract.accept_ownership()

    direct_vm.sender = direct_alice
    contract.accept_ownership()

    assert contract.get_owner().lower().replace("0x", "") == _hex(direct_alice)
    assert contract.get_pending_owner().lower().replace("0x", "") == "0" * 40
