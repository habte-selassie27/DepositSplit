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
