from web3 import Web3
from web3.middleware import geth_poa_middleware
from eth_account import Account
import bot.config as config
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s][%(filename)s:%(lineno)s] %(message)s")
logger_bc_utils = logging.getLogger(__name__) # Use a distinct logger name

def get_web3_provider():
    """Initializes and returns a Web3 provider."""
    provider = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 120} )) # config.RPC_URL will be ANVIL_RPC_URL during tests
    if not provider.is_connected():
        logger_bc_utils.error(f"Failed to connect to RPC URL: {config.RPC_URL}")
        raise ConnectionError(f"Unable to connect to {config.RPC_URL}")

    # Conditionally apply geth_poa_middleware
    # Do not apply for local Anvil/Hardhat nodes
    if not ("127.0.0.1" in config.RPC_URL or "localhost" in config.RPC_URL):
        logger_bc_utils.info(f"Applying geth_poa_middleware for RPC: {config.RPC_URL}")
        provider.middleware_onion.inject(geth_poa_middleware, layer=0)
    else:
        logger_bc_utils.info(f"Skipping geth_poa_middleware for local RPC: {config.RPC_URL}")

    fetched_chain_id = provider.eth.chain_id
    logger_bc_utils.info(f"Successfully connected to RPC: {config.RPC_URL}, Fetched Chain ID: {fetched_chain_id}")
    
    # During Anvil forking, the chain ID will be that of the forked network (Base mainnet: 8453)
    # So, config.CHAIN_ID should still match.
    if fetched_chain_id != config.CHAIN_ID:
        logger_bc_utils.warning(f"Connected Chain ID ({fetched_chain_id}) does not match expected Chain ID ({config.CHAIN_ID}) in config.py!")
    return provider

def get_wallet_credentials(private_key_hex):
    """Derives wallet address from private key."""
    try:
        account = Account.from_key(private_key_hex)
        wallet_address = account.address
        logger_bc_utils.info(f"Wallet address: {wallet_address}")
        return account, wallet_address
    except Exception as e:
        logger_bc_utils.error(f"Error deriving wallet address from private key: {e}")
        raise ValueError("Invalid private key format or value.")


def load_contract(w3_provider, contract_address, abi):
    """Loads a contract given its address and ABI."""
    try:
        checksum_address = Web3.to_checksum_address(contract_address)
        contract = w3_provider.eth.contract(address=checksum_address, abi=abi)
        logger_bc_utils.info(f"Contract loaded successfully at address: {checksum_address}")
        return contract
    except Exception as e:
        logger_bc_utils.error(f"Error loading contract at {contract_address}: {e}")
        return None

# Keep the if __name__ == '__main__': block as is for standalone testing if needed.
if __name__ == '__main__':
    # Basic test
    try:
        # Temporarily set RPC_URL for standalone test if needed, or ensure config is correct
        # config.RPC_URL = "YOUR_MAINNET_OR_TESTNET_RPC" 
        w3_instance = get_web3_provider()
        print(f"Current block number: {w3_instance.eth.block_number}")

        # Use a default dummy key for this standalone test, ensure it's valid format
        dummy_pk_for_test = "0x0000000000000000000000000000000000000000000000000000000000000001"
        test_account, test_wallet_addr = get_wallet_credentials(dummy_pk_for_test) # Use the corrected dummy key
        print(f"Derived test wallet address: {test_wallet_addr}")
        print(f"Balance (dummy account): {w3_instance.eth.get_balance(test_wallet_addr)} Ether")

        if config.POOL_ADDRESS: # Check if POOL_ADDRESS is defined
            pool_contract_test = load_contract(w3_instance, config.POOL_ADDRESS, config.MINIMAL_POOL_ABI)
            if pool_contract_test:
                print(f"Pool contract {config.POOL_ADDRESS} loaded for standalone test.")
        if config.NONFUNGIBLE_POSITION_MANAGER_ADDRESS:
            nft_manager_contract_test = load_contract(w3_instance, config.NONFUNGIBLE_POSITION_MANAGER_ADDRESS, config.MINIMAL_NONFUNGIBLE_POSITION_MANAGER_ABI)
            if nft_manager_contract_test:
                print(f"NFT Manager contract {config.NONFUNGIBLE_POSITION_MANAGER_ADDRESS} loaded for standalone test.")

    except Exception as main_e:
        print(f"An error occurred in blockchain_utils standalone test: {main_e}")