"""Consensus: what counts as agreement between the leader and a validator.

Direct mode captures the validator of the last run_nondet call, so these tests
swap the mocks and then replay the validator against the stored leader result.

Consensus binds one field only: the verdict. Two independent models word their
reasoning differently and disagree on scores, but should agree on whether a
promise was kept.
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


def _settled(direct_vm, direct_deploy, direct_owner, direct_alice, verdict="KEPT"):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract = direct_deploy("contracts/Vow.py")
    direct_vm.sender = direct_owner
    vow_id = contract.make_vow(
        PROMISE, EVIDENCE_URL, CRITERIA, 86400, direct_alice, "ref-1"
    )
    direct_vm.warp("2025-01-03T00:00:00Z")

    _mock_page(direct_vm)
    _mock_jury(direct_vm, verdict)
    contract.settle(vow_id)
    return contract, vow_id


def test_validator_accepts_the_same_verdict(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    _settled(direct_vm, direct_deploy, direct_owner, direct_alice, "KEPT")

    assert direct_vm.run_validator() is True


def test_validator_rejects_a_different_verdict(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    _settled(direct_vm, direct_deploy, direct_owner, direct_alice, "KEPT")

    direct_vm.clear_mocks()
    _mock_page(direct_vm)
    _mock_jury(direct_vm, "BROKEN")

    assert direct_vm.run_validator() is False


def test_validator_ignores_the_wording_of_the_reason(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    # Same verdict, a completely different explanation: still agreement. The
    # comparison binds the decision, never the prose.
    _settled(direct_vm, direct_deploy, direct_owner, direct_alice, "KEPT")

    direct_vm.clear_mocks()
    _mock_page(direct_vm)
    _mock_jury(
        direct_vm,
        "KEPT",
        reason="a different reading of the same page, worded differently",
    )

    assert direct_vm.run_validator() is True


def test_validator_ignores_a_different_citation(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    # Two careful readers may cite different sentences of the same page. The
    # verdict is what settles the promise, so both citations are legitimate.
    _settled(direct_vm, direct_deploy, direct_owner, direct_alice, "KEPT")

    direct_vm.clear_mocks()
    _mock_page(direct_vm)
    _mock_jury(
        direct_vm,
        "KEPT",
        quote="The console has been live since that morning.",
    )

    assert direct_vm.run_validator() is True


def test_validator_refuses_when_it_cannot_verify(
    direct_vm, direct_deploy, direct_owner, direct_alice
):
    _settled(direct_vm, direct_deploy, direct_owner, direct_alice, "KEPT")

    # No page and no model: the validator cannot reproduce the reading, and an
    # unverifiable result must never be accepted.
    direct_vm.clear_mocks()

    assert direct_vm.run_validator() is False
