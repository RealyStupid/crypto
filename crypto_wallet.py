import os
import json
import time
import requests
from typing import Callable

# === bitcoinlib imports ===
from bitcoin import SelectParams
from bitcoin.wallet import CBitcoinSecret, P2PKHBitcoinAddress
from bitcoin.core import (
    lx, COutPoint, CMutableTxIn, CMutableTxOut,
    CMutableTransaction, b2x, CScript
)
from bitcoin.core.script import OP_DUP, OP_HASH160, OP_EQUALVERIFY, OP_CHECKSIG

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SelectParams("mainnet")  # change to "testnet" for testing

# ============================================================
# Utility functions
# ============================================================

def usd_to_btc(usd_amount: float) -> float:
    """Fetch BTC price and convert USD → BTC."""
    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "bitcoin", "vs_currencies": "usd"},
        timeout=10
    )
    r.raise_for_status()
    price = r.json()["bitcoin"]["usd"]
    return usd_amount / price

def get_received_btc(address: str) -> float:
    """Check how much BTC has been received at an address (confirmed only)."""
    url = f"https://blockstream.info/api/address/{address}"
    data = requests.get(url, timeout=10).json()
    sats = data["chain_stats"]["funded_txo_sum"]
    return sats / 1e8

def get_utxos(address: str):
    """Fetch UTXOs for an address from Blockstream API."""
    url = f"https://blockstream.info/api/address/{address}/utxo"
    return requests.get(url, timeout=10).json()

def broadcast_tx(raw_hex: str) -> str:
    """Broadcast a raw transaction hex via Blockstream API."""
    url = "https://blockstream.info/api/tx"
    r = requests.post(url, data=raw_hex, timeout=10)
    if r.status_code != 200:
        raise Exception(f"Broadcast failed: {r.text}")
    return r.text  # txid

def load_master_key(key_path="master.key"):
    """
    Loads or creates a 256-bit AES key.
    Keep this file secret and do NOT ship it to untrusted clients.
    """
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            return f.read()

    key = AESGCM.generate_key(bit_length=256)
    with open(key_path, "wb") as f:
        f.write(key)
    return key

# ============================================================
# Wallet Manager Class
# ============================================================

class BitcoinPaymentWallet:
    def __init__(self, user_id: str, storage_dir="wallets"):
        self.user_id = user_id
        self.storage_dir = storage_dir
        self.wallet_path = os.path.join(storage_dir, f"{user_id}.wallet")  # now binary
        self.master_key = load_master_key()
        self.aes = AESGCM(self.master_key)

        if not os.path.exists(storage_dir):
            os.makedirs(storage_dir)

        if os.path.exists(self.wallet_path):
            self._load_wallet()
        else:
            self._create_wallet()

    # --------------------------------------------------------

    def _create_wallet(self):
        """Generate a new private key + address and save it."""
        secret = CBitcoinSecret.from_secret_bytes(os.urandom(32))
        address = P2PKHBitcoinAddress.from_pubkey(secret.pub)

        self.wallet = {
            "wif": str(secret),          # WIF-encoded private key
            "address": str(address),
            "required_usd": 0.0,
            "required_btc": 0.0,
            "completed": False
        }

        self._save_wallet()

    # --------------------------------------------------------

    def _load_wallet(self):
        with open(self.wallet_path, "rb") as f:
            raw = f.read()

        nonce = raw[:12]
        ciphertext = raw[12:]

        decrypted = self.aes.decrypt(nonce, ciphertext, None)
        self.wallet = json.loads(decrypted.decode())

    def _save_wallet(self):
        data = json.dumps(self.wallet).encode()

        nonce = os.urandom(12)  # AES-GCM nonce
        encrypted = self.aes.encrypt(nonce, data, None)

        with open(self.wallet_path, "wb") as f:
            f.write(nonce + encrypted)

    # ============================================================
    # Public API
    # ============================================================

    def get_address(self) -> str:
        return self.wallet["address"]

    def set_required_payment(self, usd_amount: float):
        btc_amount = usd_to_btc(usd_amount)
        self.wallet["required_usd"] = float(usd_amount)
        self.wallet["required_btc"] = float(btc_amount)
        self._save_wallet()

    def get_status(self) -> dict:
        received = get_received_btc(self.wallet["address"])
        return {
            "address": self.wallet["address"],
            "required_usd": self.wallet["required_usd"],
            "required_btc": self.wallet["required_btc"],
            "received_btc": received,
            "remaining_btc": max(0.0, self.wallet["required_btc"] - received),
            "completed": self.wallet["completed"]
        }

    # ============================================================
    # Sweep funds to your main wallet
    # ============================================================

    def sweep_to_main_wallet(self, main_btc_address: str, fee_sats: int = 1000) -> str:
        """
        Sweeps all UTXOs from this wallet's address to your main BTC wallet.
        Returns the broadcasted txid.
        """

        secret = CBitcoinSecret(self.wallet["wif"])
        from_address = P2PKHBitcoinAddress(self.wallet["address"])
        to_address = P2PKHBitcoinAddress(main_btc_address)

        # 1. Fetch UTXOs
        utxos = get_utxos(str(from_address))
        if not utxos:
            raise Exception("No UTXOs available to sweep.")

        # 2. Build inputs and calculate total value
        txins = []
        total_sats = 0

        for u in utxos:
            txid = lx(u["txid"])
            vout = u["vout"]
            value = u["value"]  # in sats

            outpoint = COutPoint(txid, vout)
            txin = CMutableTxIn(outpoint)
            txins.append(txin)
            total_sats += value

        # 3. Calculate output amount (minus fee)
        send_sats = total_sats - fee_sats
        if send_sats <= 0:
            raise Exception("Not enough funds to cover fee.")

        txout = CMutableTxOut(send_sats, to_address.to_scriptPubKey())

        # 4. Create unsigned transaction
        tx = CMutableTransaction(txins, [txout])

        # 5. Sign each input (P2PKH)
        from bitcoin.core import SignatureHash, SIGHASH_ALL
        for i, txin in enumerate(tx.vin):
            script_pub_key = from_address.to_scriptPubKey()
            sighash = SignatureHash(script_pub_key, tx, i, SIGHASH_ALL)
            sig = secret.sign(sighash) + bytes([SIGHASH_ALL])
            txin.scriptSig = CScript([sig, secret.pub])

        # 6. Serialize and broadcast
        raw_hex = b2x(tx.serialize())
        txid = broadcast_tx(raw_hex)
        return txid

    # ============================================================
    # Event Listener
    # ============================================================

    def wait_for_payment(self, callback: Callable, poll_interval=10):
        """
        Polls the blockchain until required BTC is received.
        Calls callback(status) when payment is complete.
        """

        print(f"Waiting for payment to {self.wallet['address']}...")

        while True:
            status = self.get_status()
            received = status["received_btc"]

            if received >= self.wallet["required_btc"]:
                print("Payment complete!")
                self.wallet["completed"] = True
                self._save_wallet()

                callback(status)

                return

            time.sleep(poll_interval)

    # ============================================================
    # Cleanup
    # ============================================================

    def delete_wallet_file(self):
        if os.path.exists(self.wallet_path):
            os.remove(self.wallet_path)

# ============================================================
# Example usage
# ============================================================

if __name__ == "__main__":
    # 1. Create or load a wallet for a user
    user_id = "example_user_123"
    wallet = BitcoinPaymentWallet(user_id)

    print("Loaded wallet for user:", user_id)
    print("User payment address:", wallet.get_address())

    # 2. Set the required payment in USD
    usd_amount = 20.0
    wallet.set_required_payment(usd_amount)
    print(f"User must pay ${usd_amount} worth of BTC")

    # 3. Show current wallet status
    status = wallet.get_status()
    print("Current status:")
    print(status)

    # 4. Define your main BTC wallet (where you want to receive funds)
    MAIN_WALLET_ADDRESS = "YOUR_MAIN_BTC_ADDRESS_HERE"

    # 5. Define what happens when payment is complete
    def on_payment_complete(final_status):
        print("\n=== PAYMENT RECEIVED ===")
        print("Final status:", final_status)
        print("Sweeping funds to main wallet...")

        txid = wallet.sweep_to_main_wallet(MAIN_WALLET_ADDRESS)
        print("Sweep transaction broadcasted, txid:", txid)

        wallet.delete_wallet_file()
        print("User wallet file deleted. Product can be delivered.")

    # 6. Start waiting for payment
    print("\nWaiting for payment... send BTC to:")
    print(wallet.get_address())
    wallet.wait_for_payment(on_payment_complete)
