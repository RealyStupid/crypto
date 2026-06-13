a basic wallet maker for shops and shi
usage examples is in the bottom of the code OR here:
```python
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
```