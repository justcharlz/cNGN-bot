from web3 import Web3
from web3.middleware import geth_poa_middleware
from eth_account import Account
import config
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def get_web3_provider():
    """Initializes and returns a Web3 provider."""
    provider = Web3(Web3.HTTPProvider(config.RPC_URL))
    if not provider.is_connected():
        logging.error(f"Failed to connect to RPC URL: {config.RPC_URL}")
        raise ConnectionError(f"Unable to connect to {config.RPC_URL}")

    # Middleware for PoA chains like Base, Polygon, BNB Chain, etc.
    provider.middleware_onion.inject(geth_poa_middleware, layer=0)
    logging.info(f"Successfully connected to RPC: {config.RPC_URL}, Chain ID: {provider.eth.chain_id}")
    if provider.eth.chain_id != config.CHAIN_ID:
        logging.warning(f"Connected Chain ID ({provider.eth.chain_id}) does not match expected Chain ID ({config.CHAIN_ID})!")
    return provider

def get_wallet_credentials(private_key_hex):
    """Derives wallet address from private key."""
    try:
        account = Account.from_key(private_key_hex)
        wallet_address = account.address
        logging.info(f"Wallet address: {wallet_address}")
        return account, wallet_address
    except Exception as e:
        logging.error(f"Error deriving wallet address from private key: {e}")
        raise ValueError("Invalid private key format or value.")


def load_contract(w3_provider, contract_address, abi):
    """Loads a contract given its address and ABI."""
    try:
        checksum_address = Web3.to_checksum_address(contract_address)
        contract = w3_provider.eth.contract(address=checksum_address, abi=abi)
        logging.info(f"Contract loaded successfully at address: {checksum_address}")
        return contract
    except Exception as e:
        logging.error(f"Error loading contract at {contract_address}: {e}")
        return None

if __name__ == '__main__':
    # Basic test
    try:
        w3 = get_web3_provider()
        print(f"Current block number: {w3.eth.block_number}")

        account, wallet_addr = get_wallet_credentials(config.DUMMY_PRIVATE_KEY)
        print(f"Derived wallet address: {wallet_addr}")
        print(f"Balance (dummy account, likely 0): {w3.eth.get_balance(wallet_addr)} Ether")

        # Test loading pool contract
        pool_contract = load_contract(w3, config.POOL_ADDRESS, config.MINIMAL_POOL_ABI)
        if pool_contract:
            print(f"Pool contract {config.POOL_ADDRESS} loaded.")
            # token0 = pool_contract.functions.token0().call()
            # print(f"Token0 of pool: {token0}")


        # Test loading NFT manager contract
        nft_manager_contract = load_contract(w3, config.NONFUNGIBLE_POSITION_MANAGER_ADDRESS, config.MINIMAL_NONFUNGIBLE_POSITION_MANAGER_ABI)
        if nft_manager_contract:
            print(f"NFT Manager contract {config.NONFUNGIBLE_POSITION_MANAGER_ADDRESS} loaded.")

    except Exception as e:
        print(f"An error occurred: {e}")