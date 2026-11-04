"""Settlement: the fetch, the jury, and the one field consensus binds.

A vow is settled after its deadline, by anyone, against the page it froze at
creation. The committee must agree on the verdict — the quote is the
deterministic link to the evidence, and a quote that is not on the page turns
the verdict into UNRESOLVED instead of confiscating a stake.
"""

import json

PROMISE = "Ship v1.0 of the console to production."
EVIDENCE_URL = "https://evidence.example.com/releases/v1"
CRITERIA = "A published release tag on the page counts as shipped."

EVIDENCE_BODY = (
    "Release notes. On 3 March the team published v1.0.0 to production. "
    "The console has been live since that morning."
)
QUOTE = "On 3 March the team published v1.0.0 to production."


def _mock_page(direct_vm, body=EVIDENCE_BODY):
    direct_vm.mock_web(r".*evidence\.example\.com.*", {"status": 200, "body": body})


def _mock_jury(direct_vm, verdict, quote=QUOTE, reason="the page decides it"):
    direct_vm.mock_llm(
        r".*\[\[VOW_SETTLEMENT\]\].*",
        json.dumps({"verdict": verdict, "quote": quote, "reason": reason}),
    )


def _vow(direct_vm, direct_deploy, direct_owner, direct_alice, days=1):
    contract = direct_deploy("contracts/Vow.py")
    direct_vm.sender = direct_owner
    vow_id = contract.make_vow(
        PROMISE, EVIDENCE_URL, CRITERIA, days, direct_alice, "ref-1"
    )
    return contract, vow_id


def test_vow_is_not_due_before_the_deadline(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice, days=7)

    assert contract.is_due(vow_id) is False

    _mock_page(direct_vm)
    _mock_jury(direct_vm, "KEPT")

    with direct_vm.expect_revert("[EXPECTED]:not_due"):
        contract.settle(vow_id)


def test_kept_promise_closes_the_vow(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)
    direct_vm.warp("2025-01-03T00:00:00Z")

    _mock_page(direct_vm)
    _mock_jury(direct_vm, "KEPT")

    assert contract.settle(vow_id) == "KEPT"

    record = contract.get_vow(vow_id)
    assert record["status"] == "KEPT"
    assert record["verdict"] == "KEPT"
    assert record["quote"].lower() == QUOTE.lower()
    assert record["attempts"] == 1
    assert record["evidence_digest"] != ""
    assert record["resolved_ts"] > 0

    assert contract.get_summary()["kept"] == 1
    assert contract.get_standing(direct_owner)["kept"] == 1


def test_broken_promise_is_recorded(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)
    direct_vm.warp("2025-01-03T00:00:00Z")

    _mock_page(direct_vm)
    _mock_jury(direct_vm, "BROKEN", reason="the page shows the release never shipped")

    assert contract.settle(vow_id) == "BROKEN"

    record = contract.get_vow(vow_id)
    assert record["status"] == "BROKEN"
    assert record["verdict"] == "BROKEN"

    assert contract.get_summary()["broken"] == 1
    assert contract.get_standing(direct_owner)["broken"] == 1


def test_unresolved_verdict_keeps_the_vow_open(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)
    direct_vm.warp("2025-01-03T00:00:00Z")

    _mock_page(direct_vm)
    _mock_jury(
        direct_vm,
        "UNRESOLVED",
        quote="",
        reason="the page does not mention a release",
    )

    assert contract.settle(vow_id) == "ACTIVE"

    record = contract.get_vow(vow_id)
    assert record["status"] == "ACTIVE"
    assert record["verdict"] == "UNRESOLVED"
    assert record["attempts"] == 1
    assert record["quote"] == ""
    assert record["resolved_ts"] == 0


def test_ungrounded_quote_downgrades_to_unresolved(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)
    direct_vm.warp("2025-01-03T00:00:00Z")

    _mock_page(direct_vm)
    # A sentence that is not on the page cannot settle anything.
    _mock_jury(direct_vm, "KEPT", quote="a sentence that is not on the page at all")

    assert contract.settle(vow_id) == "ACTIVE"

    record = contract.get_vow(vow_id)
    assert record["verdict"] == "UNRESOLVED"
    assert record["quote"] == ""


def test_empty_evidence_page_is_an_external_error(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)
    direct_vm.warp("2025-01-03T00:00:00Z")

    _mock_page(direct_vm, body="")
    _mock_jury(direct_vm, "KEPT")

    with direct_vm.expect_revert("[EXTERNAL]:empty_evidence_page"):
        contract.settle(vow_id)

    # Nothing was written: the vow stays open with no attempt spent.
    record = contract.get_vow(vow_id)
    assert record["status"] == "ACTIVE"
    assert record["attempts"] == 0


def test_settling_a_closed_vow_is_refused(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)
    direct_vm.warp("2025-01-03T00:00:00Z")

    _mock_page(direct_vm)
    _mock_jury(direct_vm, "KEPT")
    contract.settle(vow_id)

    with direct_vm.expect_revert("[EXPECTED]:vow_closed"):
        contract.settle(vow_id)


def test_unknown_vow_is_refused(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/Vow.py")

    with direct_vm.expect_revert("[EXPECTED]:unknown_vow"):
        contract.settle("vw-99")


def test_abandon_forfeits_before_the_deadline(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice, days=7)

    contract.abandon(vow_id)

    record = contract.get_vow(vow_id)
    assert record["status"] == "ABANDONED"
    assert record["verdict"] == "ABANDONED"
    assert record["resolved_ts"] > 0

    assert contract.get_summary()["abandoned"] == 1
    assert contract.get_standing(direct_owner)["abandoned"] == 1


def test_abandon_is_refused_after_the_deadline(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)
    direct_vm.warp("2025-01-05T00:00:00Z")

    with direct_vm.expect_revert("[EXPECTED]:too_late"):
        contract.abandon(vow_id)


def test_abandon_is_reserved_to_the_promiser(
    direct_vm, direct_deploy, direct_owner, direct_alice, direct_bob
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice, days=7)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("[EXPECTED]:not_promiser"):
        contract.abandon(vow_id)


def test_vow_can_be_voided_after_the_grace_window(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)
    # The deadline was 2 January; the grace window is 14 days.
    direct_vm.warp("2025-01-20T00:00:00Z")

    contract.void_vow(vow_id)

    record = contract.get_vow(vow_id)
    assert record["status"] == "VOID"
    assert record["verdict"] == "UNRESOLVED"
    assert contract.get_summary()["void"] == 1


def test_void_is_refused_inside_the_grace_window(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)
    direct_vm.warp("2025-01-05T00:00:00Z")

    with direct_vm.expect_revert("[EXPECTED]:not_voidable"):
        contract.void_vow(vow_id)


def test_void_is_refused_on_a_resolved_vow(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)
    direct_vm.warp("2025-01-03T00:00:00Z")

    _mock_page(direct_vm)
    _mock_jury(direct_vm, "KEPT")
    contract.settle(vow_id)

    with direct_vm.expect_revert("[EXPECTED]:vow_closed"):
        contract.void_vow(vow_id)


def test_settlement_gives_up_after_the_attempt_cap(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract, vow_id = _vow(direct_vm, direct_deploy, direct_owner, direct_alice)
    direct_vm.warp("2025-01-03T00:00:00Z")

    _mock_page(direct_vm)
    _mock_jury(direct_vm, "UNRESOLVED", quote="", reason="the page is silent")

    for _ in range(5):
        assert contract.settle(vow_id) == "ACTIVE"

    assert contract.get_vow(vow_id)["attempts"] == 5

    with direct_vm.expect_revert("[EXPECTED]:too_many_attempts"):
        contract.settle(vow_id)

    # Out of attempts, the stake can be unwound at once instead of waiting for
    # the grace window.
    contract.void_vow(vow_id)
    assert contract.get_vow(vow_id)["status"] == "VOID"
