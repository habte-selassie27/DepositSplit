"""DepositSplit testnet demo — template script.

Deploys the contract (or reuses an existing deployment), creates three
cases (clean evidence / supported damage / broken evidence), assesses them,
and prints the derived settlements.

Usage (Bradbury testnet):
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt

    # 1. Host your evidence somewhere publicly reachable, e.g. GitHub Gist
    #    raw pages, a pastebin, or any static HTML/text pages.
    # 2. Run:
    python scripts/demo_testnet.py \
        --move-in-url  https://your-host/case-clean/move-in.html \
        --move-out-url https://your-host/case-clean/move-out.html \
        --damaged-move-out-url https://your-host/case-damage/move-out.html

For a local simulator, add --chain localnet (requires the GenLayer Studio
simulator running; you may also need sim_config validators for LLM-backed
consensus, see the genlayer-py e2e tests).
"""

import argparse
import sys

from genlayer_py import create_client, create_account
from genlayer_py.assertions import tx_execution_succeeded
from genlayer_py.chains import localnet, studionet, testnet_bradbury
from genlayer_py.types import TransactionStatus

CONTRACT_PATH = "contracts/deposit_split.py"
DEPOSIT_AMOUNT = 100_000
MAX_DEDUCTION_BPS = 10_000


def load_contract_code() -> str:
    with open(CONTRACT_PATH, "r") as f:
        return f.read()


def wait_and_check(client, tx_hash: str, label: str):
    receipt = client.wait_for_transaction_receipt(
        transaction_hash=tx_hash,
        status=TransactionStatus.FINALIZED,
    )
    if not tx_execution_succeeded(receipt):
        print(f"  ✗ {label} failed: {receipt}")
        sys.exit(1)
    print(f"  ✓ {label}")
    return receipt


def deploy(client, account) -> str:
    print("Deploying DepositSplit...")
    tx_hash = client.deploy_contract(code=load_contract_code(), account=account)
    receipt = wait_and_check(client, tx_hash, "deploy")
    try:
        address = receipt["data"]["contract_address"]
    except (KeyError, TypeError):
        print("  Could not read the contract address from the receipt.")
        print("  Full receipt (copy 'contract_address'):")
        print(receipt)
        sys.exit(1)
    print(f"  Contract address: {address}")
    return address


def create_case(client, account, address, move_in_urls, move_out_urls, label: str) -> int:
    print(f"Creating case: {label}...")
    tx_hash = client.write_contract(
        address=address,
        account=account,
        function_name="create_case",
        args=[
            "Tenant Alice",
            "Landlord Bob",
            "inventory-hash-demo",
            move_in_urls,
            move_out_urls,
            DEPOSIT_AMOUNT,
            MAX_DEDUCTION_BPS,
        ],
    )
    wait_and_check(client, tx_hash, f"create_case ({label})")
    case_id = client.read_contract(address=address, function_name="get_case_count", args=[]) - 1
    print(f"  case_id = {case_id}")
    return case_id


def assess(client, account, address, case_id: int, label: str) -> dict:
    print(f"Assessing case {case_id} ({label}) — validators fetch evidence...")
    tx_hash = client.write_contract(
        address=address,
        account=account,
        function_name="assess_case",
        args=[case_id],
    )
    wait_and_check(client, tx_hash, f"assess_case ({label})")
    settlement = client.read_contract(
        address=address, function_name="get_settlement", args=[case_id]
    )
    print(f"  settlement: {settlement}")
    return settlement


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chain", choices=["bradbury", "studionet", "localnet"], default="bradbury"
    )
    parser.add_argument("--address", help="reuse an already-deployed contract address")
    parser.add_argument("--move-in-url", required=True, help="reachable move-in evidence page")
    parser.add_argument(
        "--move-out-url", required=True, help="reachable clean move-out evidence page"
    )
    parser.add_argument(
        "--damaged-move-out-url",
        help="reachable damaged move-out evidence page (defaults to --move-out-url)",
    )
    args = parser.parse_args()

    chains = {
        "bradbury": testnet_bradbury,
        "studionet": studionet,
        "localnet": localnet,
    }
    account = create_account()
    client = create_client(chain=chains[args.chain], account=account)
    print(f"Account: {account.address} (fund it from the network faucet if needed)")

    if args.chain == "localnet":
        client.fund_account(address=account.address, amount=10**18)

    address = args.address or deploy(client, account)

    damaged_move_out = args.damaged_move_out_url or args.move_out_url

    # Case 1: clean evidence -> FULL_REFUND
    case_clean = create_case(
        client, account, address, [args.move_in_url], [args.move_out_url], "clean"
    )
    # Case 2: supported damage -> DEDUCT (bounded by max_deduction_bps)
    case_damage = create_case(
        client, account, address, [args.move_in_url], [damaged_move_out], "damage"
    )
    # Case 3: unreachable evidence -> REVIEW (fail closed)
    case_broken = create_case(
        client,
        account,
        address,
        [args.move_in_url],
        ["https://unreachable.invalid/move-out.html"],
        "broken URL",
    )

    print()
    assess(client, account, address, case_clean, "clean")
    assess(client, account, address, case_damage, "damage")
    assess(client, account, address, case_broken, "broken URL")


if __name__ == "__main__":
    main()
