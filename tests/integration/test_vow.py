"""Vow on studionet: every public method, including the refusals.

One deployment per run. The deterministic methods go first — they cannot fail on
network weather — then the money paths, then the settlement that costs a live web
fetch and live LLM consensus, then governance. Settlement waits on the real
clock: the vow is created with a one-minute deadline and the suite polls
`is_due` instead of sleeping for a fixed span.

The two wallets are the throwaway Studio accounts from the private wallets
repository. `scripts/wallet.py` loads them from `tests/` locally, or from
VOW_TESTER1_KEYSTORE / VOW_TESTER1_PASSWORD and the TESTER2 pair in CI.

    pytest tests/integration -v -s
"""

import sys
import time
from pathlib import Path

import pytest
from genlayer_py import create_client, studionet

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from wallet import load_account  # noqa: E402

CONTRACT_PATH = ROOT / "contracts" / "Vow.py"

# studionet allows roughly thirty requests a minute and every receipt poll is a
# request, so a ten second interval keeps a whole run inside that budget.
POLL_INTERVAL = 10_000
POLL_RETRIES = 60
RETRY_PAUSE = 20

DAY = 86400
STAKE = 500
STRANGER_STAKE = 100
SETTLE_DEADLINE_SECONDS = 60

ALLOWED_VERDICTS = ("KEPT", "BROKEN", "UNRESOLVED")

# Public, stable, plain text — the same page ClauseGuard's live suite fetches, so
# validators demonstrably reach it. The promise below is checkable against it
# without interpretation.
EVIDENCE_URL = "https://www.rfc-editor.org/rfc/rfc2119.txt"
PROMISE = (
    "Publish the full text of RFC 2119, the requirement-keyword standard, at "
    "https://www.rfc-editor.org/rfc/rfc2119.txt."
)
CRITERIA = (
    "KEPT only when the page carries the RFC 2119 text and its requirement "
    "keywords."
)

# A page that does not exist. Whatever the committee reports about it, the stake
# must not move: an unreadable source is never evidence of a broken promise.
UNREADABLE_URL = "https://www.rfc-editor.org/rfc/rfc99999.txt"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def account():
    return load_account("tester1")


@pytest.fixture(scope="module")
def stranger():
    return load_account("tester2")


@pytest.fixture(scope="module")
def client(account):
    return create_client(studionet, account=account)


@pytest.fixture(scope="module")
def contract(client, account):
    """The only deployment of this run."""
    code = CONTRACT_PATH.read_text(encoding="utf-8")
    tx_hash = client.deploy_contract(code=code, account=account)
    receipt = client.wait_for_transaction_receipt(
        tx_hash, retries=POLL_RETRIES, interval=POLL_INTERVAL
    )
    address = receipt["data"]["contract_address"]
    assert address, f"the deployment produced no address: {receipt}"
    print(f"\n[vow] deployed at {address}")
    return address


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hex(value) -> str:
    return str(value).lower().replace("0x", "")


def _execution_succeeded(result) -> bool:
    """Send landed, reached consensus, and the call itself returned.

    A refused write still lands as a decided transaction: the committee agrees it
    rolled back. Only `execution_result == "SUCCESS"` means state changed.
    """
    if not isinstance(result, dict):
        return False
    if result.get("status_name") not in ("ACCEPTED", "FINALIZED"):
        return False
    leader = (result.get("consensus_data") or {}).get("leader_receipt")
    if isinstance(leader, list):
        leader = leader[0] if leader else None
    if isinstance(leader, dict):
        return leader.get("execution_result") == "SUCCESS"
    return str(result.get("tx_execution_result_name", "")) == "FINISHED_WITH_RETURN"


def _rollback_reason(result) -> str:
    if not isinstance(result, dict):
        return str(result)
    leader = (result.get("consensus_data") or {}).get("leader_receipt")
    if isinstance(leader, list):
        leader = leader[0] if leader else None
    if isinstance(leader, dict) and isinstance(leader.get("result"), dict):
        return str(leader["result"].get("status"))
    return str(result.get("status_name"))


def _definite_rollback(result) -> bool:
    """True when consensus decided the call was refused — not a flaky race."""
    if not isinstance(result, dict):
        return False
    leader = (result.get("consensus_data") or {}).get("leader_receipt")
    if isinstance(leader, list):
        leader = leader[0] if leader else None
    if not isinstance(leader, dict):
        return False
    return leader.get("execution_result") == "ERROR"


def _attempt(client, account, contract, method, args=None, value=0):
    """Send one write and return its receipt, or the exception it raised."""
    try:
        tx_hash = client.write_contract(
            contract, method, args=args or [], value=value, account=account
        )
        return client.wait_for_transaction_receipt(
            tx_hash, retries=POLL_RETRIES, interval=POLL_INTERVAL
        )
    except Exception as error:
        return error


def _expect_ok(
    client, account, contract, method, args=None, value=0, attempts=3, verify=None
):
    """Send a write and require it to land.

    A non-idempotent write must not be blindly resubmitted: if the first
    transaction landed but its receipt came back inconclusive, a retry is
    refused as a duplicate and looks like a failure. `verify` re-reads state.
    """
    last = None
    for attempt in range(attempts):
        result = _attempt(client, account, contract, method, args, value)
        if _execution_succeeded(result):
            return result
        if verify is not None and verify():
            return result
        if _definite_rollback(result):
            raise AssertionError(f"{method} was refused: {_rollback_reason(result)}")
        last = result
        if attempt < attempts - 1:
            print(f"  {method} did not reach consensus, retrying")
            time.sleep(RETRY_PAUSE)
    raise AssertionError(f"{method} did not reach consensus: {_rollback_reason(last)}")


def _expect_denied(client, account, contract, method, args=None, value=0):
    result = _attempt(client, account, contract, method, args, value)
    assert not _execution_succeeded(result), (
        f"{method} should have been refused, but it succeeded: {result}"
    )
    return result


def _read(client, contract, method, args=None):
    """Read a view, retrying a provider hiccup.

    A 502 from the RPC endpoint is not an answer from the contract, and the
    provider does return them. Retrying keeps a green suite from going red on
    weather; a real failure still raises the provider's own error.
    """
    last = None
    for attempt in range(3):
        try:
            return client.read_contract(contract, method, args=args or [])
        except Exception as error:
            last = error
            if attempt < 2:
                print(f"  {method} read failed, retrying: {error}")
                time.sleep(RETRY_PAUSE)
    raise last


def _vow_ids(client, contract):
    return _read(client, contract, "get_vow_ids")["ids"]


def _vow(client, contract, vow_id):
    return _read(client, contract, "get_vow", [vow_id])


def _ledger(client, contract):
    return _read(client, contract, "get_ledger")


def _balance(client, contract):
    return int(_read(client, contract, "get_balance"))


def _make_vow(
    client,
    account,
    contract,
    url,
    ref,
    seconds,
    beneficiary,
    pledge=PROMISE,
    criteria=CRITERIA,
):
    """Publish a vow and return its id, verified by the state it changed."""
    before = len(_vow_ids(client, contract))
    _expect_ok(
        client,
        account,
        contract,
        "make_vow",
        [pledge, url, criteria, seconds, beneficiary, ref],
        verify=lambda: len(_vow_ids(client, contract)) > before,
    )
    ids = _vow_ids(client, contract)
    assert len(ids) > before, "make_vow did not add a vow"
    return ids[-1]


def _stake(client, account, contract, vow_id, amount):
    _expect_ok(client, account, contract, "stake_vow", [vow_id], value=amount)
    return int(_vow(client, contract, vow_id)["stake"])


def _wait_balance_drop(client, contract, before, amount, attempts=30, pause=10):
    """Poll until the contract balance has fallen by exactly `amount`.

    A payout is an external message emitted on='finalized': it executes after the
    parent transaction is finalized, so reading the balance straight after the
    write still shows the funds in the contract.
    """
    last = _balance(client, contract)
    for _ in range(attempts):
        if before - last == amount:
            return last
        time.sleep(pause)
        last = _balance(client, contract)
    return last


def _wait_until_due(client, contract, vow_id, attempts=18, pause=10):
    for _ in range(attempts):
        if bool(_read(client, contract, "is_due", [vow_id])):
            return True
        time.sleep(pause)
    return False


# ---------------------------------------------------------------------------
# deployment and the deterministic surface
# ---------------------------------------------------------------------------


def test_01_deployment_is_live_and_empty(client, account, contract):
    assert _hex(_read(client, contract, "get_owner")) == _hex(account.address)
    assert _hex(_read(client, contract, "get_pending_owner")) == "0" * 40
    assert _read(client, contract, "is_paused") is False

    assert _ledger(client, contract) == {
        "total_in": 0,
        "total_out": 0,
        "locked_total": 0,
    }
    assert _vow_ids(client, contract) == []

    summary = _read(client, contract, "get_summary")
    assert int(summary["vows"]) == 0
    assert int(summary["active"]) == 0

    rules = _read(client, contract, "get_rules")
    assert int(rules["min_deadline_seconds"]) == 60
    assert int(rules["max_deadline_seconds"]) == 365 * DAY
    assert int(rules["settle_bounty_bps"]) == 200


def test_02_make_vow_records_the_promise(client, account, contract, stranger):
    vow_id = _make_vow(
        client, account, contract, EVIDENCE_URL, "ref-1", 3 * DAY, stranger.address
    )

    record = _vow(client, contract, vow_id)
    assert record["promise"] == PROMISE
    assert record["evidence_url"] == EVIDENCE_URL
    assert record["criteria"] == CRITERIA
    assert record["status"] == "ACTIVE"
    assert record["verdict"] == ""
    assert int(record["stake"]) == 0
    assert int(record["attempts"]) == 0
    assert _hex(record["promiser"]) == _hex(account.address)
    assert _hex(record["beneficiary"]) == _hex(stranger.address)
    assert int(record["deadline_ts"]) - int(record["created_ts"]) == 3 * DAY


def test_03_duplicate_vow_ref_is_refused(client, account, contract, stranger):
    _expect_denied(
        client,
        account,
        contract,
        "make_vow",
        [PROMISE, EVIDENCE_URL, "", 3 * DAY, stranger.address, "ref-1"],
    )


def test_04_adversarial_inputs_are_refused(client, account, contract, stranger):
    # Bad text, an insecure scheme, a loopback host, a private host, a zero
    # beneficiary and a deadline below the floor. Every one must revert, and none
    # may write a vow.
    before = len(_vow_ids(client, contract))

    attempts = [
        ["soon", EVIDENCE_URL, "", 3 * DAY, stranger.address, "ref-short"],
        [
            PROMISE,
            "http://www.rfc-editor.org/rfc/rfc2119.txt",
            "",
            3 * DAY,
            stranger.address,
            "ref-http",
        ],
        [PROMISE, "https://127.0.0.1/rfc2119.txt", "", 3 * DAY, stranger.address, "ref-ip"],
        [
            PROMISE,
            "https://localhost/rfc2119.txt",
            "",
            3 * DAY,
            stranger.address,
            "ref-local",
        ],
        [PROMISE, EVIDENCE_URL, "", 3 * DAY, "0x" + "0" * 40, "ref-zero"],
        [PROMISE, EVIDENCE_URL, "", 30, stranger.address, "ref-short-deadline"],
    ]

    for args in attempts:
        _expect_denied(client, account, contract, "make_vow", args)

    assert len(_vow_ids(client, contract)) == before


def test_05_vows_are_listed_and_unknown_ones_are_refused(client, contract):
    vows = _read(client, contract, "get_vows")
    assert len(vows) == len(_vow_ids(client, contract))
    assert all(record["status"] == "ACTIVE" for record in vows.values())

    with pytest.raises(Exception):
        _read(client, contract, "get_vow", ["vw-does-not-exist"])

    with pytest.raises(Exception):
        _read(client, contract, "is_due", ["vw-does-not-exist"])


# ---------------------------------------------------------------------------
# money: stakes, credits and a real transfer
# ---------------------------------------------------------------------------


def test_06_promiser_stakes_behind_the_vow(client, account, contract):
    vow_id = _vow_ids(client, contract)[0]
    assert _stake(client, account, contract, vow_id, STAKE) == STAKE

    ledger = _ledger(client, contract)
    assert int(ledger["total_in"]) == STAKE
    assert int(ledger["locked_total"]) == STAKE
    assert int(ledger["total_out"]) == 0

    standing = _read(client, contract, "get_standing", [account.address])
    assert int(standing["staked"]) == STAKE


def test_07_zero_stake_is_refused(client, account, contract):
    vow_id = _vow_ids(client, contract)[0]
    _expect_denied(client, account, contract, "stake_vow", [vow_id])
    assert int(_ledger(client, contract)["total_in"]) == STAKE


def test_08_stranger_stake_becomes_a_credit(client, stranger, contract):
    vow_id = _vow_ids(client, contract)[0]

    # Not a revert: a payable call never strands value. The stranger's GEN is
    # recorded against the stranger and stays withdrawable.
    _expect_ok(client, stranger, contract, "stake_vow", [vow_id], value=STRANGER_STAKE)

    assert int(_vow(client, contract, vow_id)["stake"]) == STAKE
    standing = _read(client, contract, "get_standing", [stranger.address])
    assert int(standing["credit"]) == STRANGER_STAKE

    ledger = _ledger(client, contract)
    assert int(ledger["locked_total"]) == STAKE + STRANGER_STAKE
    assert int(ledger["total_out"]) == 0


def test_09_credit_is_withdrawable(client, stranger, contract):
    before = _balance(client, contract)

    _expect_ok(client, stranger, contract, "withdraw_credit")

    after = _wait_balance_drop(client, contract, before, STRANGER_STAKE)
    assert before - after == STRANGER_STAKE

    assert int(_read(client, contract, "get_standing", [stranger.address])["credit"]) == 0
    assert int(_ledger(client, contract)["locked_total"]) == STAKE


def test_10_settle_is_refused_before_the_deadline(client, account, contract):
    vow_id = _vow_ids(client, contract)[0]

    assert _read(client, contract, "is_due", [vow_id]) is False
    _expect_denied(client, account, contract, "settle", [vow_id])


def test_11_stranger_cannot_abandon(client, stranger, contract):
    vow_id = _vow_ids(client, contract)[0]

    _expect_denied(client, stranger, contract, "abandon", [vow_id])
    assert _vow(client, contract, vow_id)["status"] == "ACTIVE"


def test_12_abandon_pays_the_beneficiary(client, account, contract):
    vow_id = _vow_ids(client, contract)[0]
    before = _balance(client, contract)
    locked_before = int(_ledger(client, contract)["locked_total"])
    out_before = int(_ledger(client, contract)["total_out"])

    _expect_ok(client, account, contract, "abandon", [vow_id])

    record = _vow(client, contract, vow_id)
    assert record["status"] == "ABANDONED"
    assert record["verdict"] == "ABANDONED"

    after = _wait_balance_drop(client, contract, before, STAKE)
    assert before - after == STAKE

    # Ledger assertions are deltas: an earlier test already paid out the
    # stranger's withdrawn credit, so the totals do not start at zero.
    ledger = _ledger(client, contract)
    assert int(ledger["locked_total"]) == locked_before - STAKE
    assert int(ledger["total_out"]) == out_before + STAKE

    standing = _read(client, contract, "get_standing", [account.address])
    assert int(standing["abandoned"]) == 1


# ---------------------------------------------------------------------------
# settlement: a real fetch, a real committee, and a real clock
# ---------------------------------------------------------------------------


def test_13_settlement_moves_the_stake_with_the_verdict(
    client, account, contract, stranger
):
    readable = _make_vow(
        client,
        account,
        contract,
        EVIDENCE_URL,
        "ref-settle",
        SETTLE_DEADLINE_SECONDS,
        stranger.address,
    )
    _stake(client, account, contract, readable, STAKE)

    # A second vow aimed at a page that cannot prove anything, staked too, so
    # "no confiscation without evidence" is actually under test.
    unreadable = _make_vow(
        client,
        account,
        contract,
        UNREADABLE_URL,
        "ref-blank",
        SETTLE_DEADLINE_SECONDS,
        stranger.address,
        criteria="",
    )
    _stake(client, account, contract, unreadable, STAKE)

    assert _wait_until_due(client, contract, readable), "the vow never became due"
    assert _wait_until_due(client, contract, unreadable)

    # The readable page: the committee returns one of three verdicts, and the
    # money follows exactly that verdict.
    before = _balance(client, contract)
    _expect_ok(client, account, contract, "settle", [readable])

    record = _vow(client, contract, readable)
    verdict = record["verdict"]
    assert verdict in ALLOWED_VERDICTS, verdict
    assert int(record["attempts"]) == 1

    ledger = _ledger(client, contract)
    assert int(ledger["total_in"]) == int(ledger["total_out"]) + int(ledger["locked_total"])

    if verdict == "UNRESOLVED":
        assert record["status"] == "ACTIVE"
        assert record["quote"] == ""
        assert _balance(client, contract) == before
    else:
        assert record["status"] == verdict
        assert record["quote"], "a resolving verdict must carry a grounded quote"
        after = _wait_balance_drop(client, contract, before, STAKE)
        assert before - after == STAKE

    # The unreadable page: whatever the committee says, nothing moves.
    locked_before = int(_ledger(client, contract)["locked_total"])
    out_before = int(_ledger(client, contract)["total_out"])
    _attempt(client, account, contract, "settle", [unreadable])

    blank = _vow(client, contract, unreadable)
    assert blank["status"] == "ACTIVE", blank
    assert int(_ledger(client, contract)["locked_total"]) == locked_before
    assert int(_ledger(client, contract)["total_out"]) == out_before


# ---------------------------------------------------------------------------
# governance and the remaining guards
# ---------------------------------------------------------------------------


def test_14_pause_blocks_new_vows_and_stakes(client, account, contract, stranger):
    _expect_denied(client, stranger, contract, "set_paused", [True])

    _expect_ok(client, account, contract, "set_paused", [True])
    assert _read(client, contract, "is_paused") is True

    _expect_denied(
        client,
        account,
        contract,
        "make_vow",
        [PROMISE, EVIDENCE_URL, "", 3 * DAY, stranger.address, "ref-paused"],
    )

    # A stake while paused is not lost: it becomes a credit.
    _expect_ok(
        client,
        account,
        contract,
        "stake_vow",
        [_vow_ids(client, contract)[0]],
        value=STRANGER_STAKE,
    )
    assert int(
        _read(client, contract, "get_standing", [account.address])["credit"]
    ) >= STRANGER_STAKE

    _expect_ok(client, account, contract, "set_paused", [False])
    assert _read(client, contract, "is_paused") is False


def test_15_ownership_moves_in_two_steps(client, account, contract, stranger):
    _expect_denied(client, stranger, contract, "transfer_ownership", [stranger.address])

    _expect_ok(client, account, contract, "transfer_ownership", [stranger.address])
    assert _hex(_read(client, contract, "get_pending_owner")) == _hex(stranger.address)

    _expect_denied(client, account, contract, "accept_ownership")

    _expect_ok(client, stranger, contract, "accept_ownership")
    assert _hex(_read(client, contract, "get_owner")) == _hex(stranger.address)

    # Hand it back so the contract is left owned by the signer.
    _expect_ok(client, stranger, contract, "transfer_ownership", [account.address])
    _expect_ok(client, account, contract, "accept_ownership")
    assert _hex(_read(client, contract, "get_owner")) == _hex(account.address)


def test_16_void_is_refused_inside_the_grace_window(client, account, contract):
    # Nothing here is old enough to void: a vow becomes voidable only after the
    # grace window, or once the attempt cap has been reached.
    for vow_id in _vow_ids(client, contract):
        if _vow(client, contract, vow_id)["status"] == "ACTIVE":
            _expect_denied(client, account, contract, "void_vow", [vow_id])


def test_17_ledger_and_summary_stay_consistent(client, contract):
    ledger = _ledger(client, contract)
    assert int(ledger["total_in"]) == int(ledger["total_out"]) + int(ledger["locked_total"])

    summary = _read(client, contract, "get_summary")
    vows = _read(client, contract, "get_vows")
    assert int(summary["vows"]) == len(vows)
    assert int(summary["active"]) == sum(
        1 for record in vows.values() if record["status"] == "ACTIVE"
    )
    assert _balance(client, contract) >= 0
