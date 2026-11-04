"""Drive the pinned contract with the two Studio wallets.

    python scripts/smoke.py                    # uses deployment.json
    python scripts/smoke.py --address 0x...    # or an explicit address

The Studio suite deploys its own copy on purpose, so its checks never touch the
published contract. This is the run that does: it reads the views, publishes a
vow, stakes it, and abandons it, which pays the beneficiary for real. Every
transaction hash is printed, so the run can be checked from the explorer.

That also means the smoke leaves one ABANDONED vow on the public wall. That is
the point: the wall is empty until somebody makes a promise on it.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import re

from genlayer_py import create_client, studionet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from wallet import load_account  # noqa: E402

DEPLOYMENT_PATH = ROOT / "deployment.json"
DEPLOYMENT_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")

POLL_INTERVAL = 10_000
POLL_RETRIES = 60

DAY = 86400
STAKE = 100

PROMISE = (
    "Publish the full text of RFC 2119, the requirement-keyword standard, at "
    "https://www.rfc-editor.org/rfc/rfc2119.txt."
)
CRITERIA = "KEPT only when the page carries the RFC 2119 text and its requirement keywords."
EVIDENCE_URL = "https://www.rfc-editor.org/rfc/rfc2119.txt"


def deployed_address() -> str:
    if not DEPLOYMENT_PATH.exists():
        raise SystemExit(
            "[smoke] no deployment.json; run the Deploy Contract workflow first"
        )
    payload = json.loads(DEPLOYMENT_PATH.read_text(encoding="utf-8"))
    address = str(payload.get("address", "")).strip()
    if not DEPLOYMENT_ADDRESS.fullmatch(address):
        raise SystemExit(f"[smoke] deployment.json has no usable address: {address}")
    return address


def execution_succeeded(receipt) -> bool:
    if not isinstance(receipt, dict):
        return False
    if receipt.get("status_name") not in ("ACCEPTED", "FINALIZED"):
        return False
    leader = (receipt.get("consensus_data") or {}).get("leader_receipt")
    if isinstance(leader, list):
        leader = leader[0] if leader else None
    if isinstance(leader, dict):
        return leader.get("execution_result") == "SUCCESS"
    return str(receipt.get("tx_execution_result_name", "")) == "FINISHED_WITH_RETURN"


def send(client, account, contract, method, args=None, value=0):
    """Send one write, print both hashes, and require it to land."""
    tx_hash = client.write_contract(
        contract, method, args=args or [], value=value, account=account
    )
    print(f"[smoke] {method}({', '.join(str(a) for a in (args or []))}) value={value}")
    print(f"[smoke]   tx {tx_hash}")

    receipt = client.wait_for_transaction_receipt(
        tx_hash, retries=POLL_RETRIES, interval=POLL_INTERVAL
    )
    if not execution_succeeded(receipt):
        raise SystemExit(f"[smoke] {method} did not succeed: {receipt}")
    return receipt


def read(client, contract, method, args=None):
    return client.read_contract(contract, method, args=args or [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="", help="the contract to drive")
    args = parser.parse_args()

    address = args.address.strip() or deployed_address()

    account = load_account("tester1")
    beneficiary = load_account("tester2")
    client = create_client(studionet, account=account)

    print(f"[smoke] contract {address}")
    print(f"[smoke] https://explorer-studio.genlayer.com/address/{address}")
    print(f"[smoke] signer   {account.address}")
    print(f"[smoke] payee    {beneficiary.address}")

    owner = read(client, contract=address, method="get_owner")
    print(f"[smoke] get_owner -> {owner}")
    if str(owner).lower().replace("0x", "") != account.address.lower().replace("0x", ""):
        raise SystemExit(f"[smoke] {address} is not owned by the signer")

    print(f"[smoke] get_summary -> {read(client, address, 'get_summary')}")
    print(f"[smoke] get_ledger  -> {read(client, address, 'get_ledger')}")
    print(f"[smoke] get_rules   -> {read(client, address, 'get_rules')}")

    before_ids = read(client, address, "get_vow_ids")["ids"]
    balance_before = int(read(client, address, "get_balance"))
    ledger_before = read(client, address, "get_ledger")

    ref = "smoke-" + str(int(time.time()))
    send(
        client,
        account,
        address,
        "make_vow",
        [PROMISE, EVIDENCE_URL, CRITERIA, DAY, beneficiary.address, ref],
    )

    ids = read(client, address, "get_vow_ids")["ids"]
    if len(ids) <= len(before_ids):
        raise SystemExit("[smoke] make_vow did not add a vow")
    vow_id = ids[-1]
    print(f"[smoke] vow {vow_id}")

    send(client, account, address, "stake_vow", [vow_id], value=STAKE)

    record = read(client, address, "get_vow", [vow_id])
    assert int(record["stake"]) == STAKE, record

    send(client, account, address, "abandon", [vow_id])

    record = read(client, address, "get_vow", [vow_id])
    if record["status"] != "ABANDONED":
        raise SystemExit(f"[smoke] the vow was not abandoned: {record}")

    # The ledger is settled inside the transaction; the on-chain balance only
    # moves when the emitted payout finalises, so poll for the money to leave.
    ledger_after = read(client, address, "get_ledger")
    if int(ledger_after["total_out"]) != int(ledger_before["total_out"]) + STAKE:
        raise SystemExit(f"[smoke] the payout was not booked: {ledger_after}")
    if int(ledger_after["locked_total"]) != int(ledger_before["locked_total"]):
        raise SystemExit(f"[smoke] the stake was not released: {ledger_after}")

    balance_after = balance_before
    for _ in range(30):
        balance_after = int(read(client, address, "get_balance"))
        if balance_after == balance_before:
            break
        time.sleep(10)

    print(f"[smoke] vow     -> {record['status']} / {record['verdict']}")
    print(f"[smoke] ledger  -> {ledger_after}")
    print(f"[smoke] balance -> {balance_before} then {balance_after}")
    print("[smoke] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
