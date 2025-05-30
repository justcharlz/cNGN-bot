import unittest
import os
import time
import logging
from decimal import Decimal 

from web3 import Web3
from eth_account import Account 

import bot.config as config
from bot.utils.blockchain_utils import get_web3_provider, get_wallet_credentials, load_contract
import bot.utils.dex_utils as dex_utils 

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s][%(filename)s:%(lineno)s] %(message)s")
logger = logging.getLogger(__name__)

ANVIL_RPC_URL = "http://127.0.0.1:8545"

CNGN_WHALE_ADDRESS = "0xF7Fceda4d895602de62E54E4289ECFA97B86EBea" 
USDC_WHALE_ADDRESS = "0x122fDD9fEcbc82F7d4237C0549a5057E31c8EF8D" 

ETH_TO_FUND_BOT = Web3.to_wei(10, 'ether') 
ETH_FOR_WHALE_TX = Web3.to_wei(0.1, 'ether') # Amount of ETH to give whale for gas

# Using Decimal for initial units before multiplying by 10**decimals
INITIAL_CNGN_FUNDING_UNITS = Decimal("10000") # Updated to your initial value
INITIAL_USDC_FUNDING_UNITS = Decimal("10000") # Updated to your initial value


class TestDexUtilsAnvil(unittest.TestCase):
    original_rpc_url = None
    pool_info_data = None
    pool_state_data = None
    current_test_token_id = None
    current_position_liquidity = None
    
    # Variables to store actual token addresses and decimals determined in setUpClass
    cngn_actual_address = None
    cngn_actual_decimals = None
    usdc_actual_address = None
    usdc_actual_decimals = None


    @classmethod
    def setUpClass(cls):
        logger.info("Setting up Anvil test environment...")
        TestDexUtilsAnvil.original_rpc_url = config.RPC_URL 
        config.RPC_URL = ANVIL_RPC_URL

        try:
            cls.w3 = get_web3_provider() 
            if not cls.w3.is_connected():
                raise ConnectionError(f"Failed to connect to Anvil at {ANVIL_RPC_URL}.")

            cls.bot_account, cls.bot_wallet_address = get_wallet_credentials(config.DUMMY_PRIVATE_KEY)
            logger.info(f"Test bot wallet address: {cls.bot_wallet_address}")

            cls.fund_account_with_eth(cls.bot_wallet_address, ETH_TO_FUND_BOT)

            cls.pool_contract = load_contract(cls.w3, config.POOL_ADDRESS, config.MINIMAL_POOL_ABI)
            cls.nft_manager_contract = load_contract(cls.w3, config.NONFUNGIBLE_POSITION_MANAGER_ADDRESS, config.MINIMAL_NONFUNGIBLE_POSITION_MANAGER_ABI)

            if not cls.pool_contract or not cls.nft_manager_contract:
                raise ConnectionError("Failed to load pool or NFT manager contract on Anvil fork.")

            # Determine actual token0 and token1 from the pool
            pool_token0_addr_str = cls.pool_contract.functions.token0().call()
            pool_token1_addr_str = cls.pool_contract.functions.token1().call()
            cls.actual_pool_token0_address = Web3.to_checksum_address(pool_token0_addr_str)
            cls.actual_pool_token1_address = Web3.to_checksum_address(pool_token1_addr_str)
            
            token0_contract_pool = load_contract(cls.w3, cls.actual_pool_token0_address, config.MINIMAL_ERC20_ABI)
            token1_contract_pool = load_contract(cls.w3, cls.actual_pool_token1_address, config.MINIMAL_ERC20_ABI)

            actual_pool_token0_decimals = token0_contract_pool.functions.decimals().call()
            actual_pool_token1_decimals = token1_contract_pool.functions.decimals().call()
            logger.info(f"Pool Token0 ({cls.actual_pool_token0_address}) decimals: {actual_pool_token0_decimals}")
            logger.info(f"Pool Token1 ({cls.actual_pool_token1_address}) decimals: {actual_pool_token1_decimals}")

            # Map configured cNGN/USDC to actual pool token0/token1
            # This assumes config.TOKEN0_ADDRESS is cNGN and config.TOKEN1_ADDRESS is USDC
            if cls.actual_pool_token0_address.lower() == config.TOKEN0_ADDRESS.lower(): # cNGN is actual token0
                TestDexUtilsAnvil.cngn_actual_address = cls.actual_pool_token0_address
                TestDexUtilsAnvil.cngn_actual_decimals = actual_pool_token0_decimals
                TestDexUtilsAnvil.usdc_actual_address = cls.actual_pool_token1_address
                TestDexUtilsAnvil.usdc_actual_decimals = actual_pool_token1_decimals
            elif cls.actual_pool_token1_address.lower() == config.TOKEN0_ADDRESS.lower(): # cNGN is actual token1
                TestDexUtilsAnvil.cngn_actual_address = cls.actual_pool_token1_address
                TestDexUtilsAnvil.cngn_actual_decimals = actual_pool_token1_decimals
                TestDexUtilsAnvil.usdc_actual_address = cls.actual_pool_token0_address
                TestDexUtilsAnvil.usdc_actual_decimals = actual_pool_token0_decimals
            else:
                logger.error("FATAL: Configured cNGN address (config.TOKEN0_ADDRESS) does not match either token0 or token1 of the loaded pool. Cannot proceed with ERC20 funding correctly.")
                raise ValueError("cNGN address mismatch with pool tokens.")
            
            # Ensure the other token is USDC as expected
            if TestDexUtilsAnvil.usdc_actual_address.lower() != config.TOKEN1_ADDRESS.lower():
                logger.error(f"FATAL: Pool's other token ({TestDexUtilsAnvil.usdc_actual_address}) does not match configured USDC address ({config.TOKEN1_ADDRESS}).")
                raise ValueError("USDC address mismatch with pool tokens.")


            logger.info(f"Mapped cNGN: {TestDexUtilsAnvil.cngn_actual_address} (Decimals: {TestDexUtilsAnvil.cngn_actual_decimals})")
            logger.info(f"Mapped USDC: {TestDexUtilsAnvil.usdc_actual_address} (Decimals: {TestDexUtilsAnvil.usdc_actual_decimals})")

            # Fund with cNGN
            cngn_amount_to_fund = int(INITIAL_CNGN_FUNDING_UNITS * (10**TestDexUtilsAnvil.cngn_actual_decimals))
            cls.fund_account_with_erc20(
                cls.bot_wallet_address, TestDexUtilsAnvil.cngn_actual_address, 
                CNGN_WHALE_ADDRESS, cngn_amount_to_fund, "cNGN"
            )
            # Fund with USDC
            usdc_amount_to_fund = int(INITIAL_USDC_FUNDING_UNITS * (10**TestDexUtilsAnvil.usdc_actual_decimals))
            cls.fund_account_with_erc20(
                cls.bot_wallet_address, TestDexUtilsAnvil.usdc_actual_address, 
                USDC_WHALE_ADDRESS, usdc_amount_to_fund, "USDC"
            )

        except Exception as e:
            logger.error(f"Fatal error during setUpClass: {e}", exc_info=True)
            if TestDexUtilsAnvil.original_rpc_url is not None: 
                config.RPC_URL = TestDexUtilsAnvil.original_rpc_url
            raise 

    @classmethod
    def tearDownClass(cls):
        logger.info("Tearing down Anvil test environment...")
        if TestDexUtilsAnvil.original_rpc_url is not None:
            config.RPC_URL = TestDexUtilsAnvil.original_rpc_url
            logger.info("Original RPC URL restored.")
        else:
            logger.warning("Original RPC URL was not stored, cannot restore.")


    @classmethod
    def anvil_rpc_request(cls, method, params):
        try:
            response = cls.w3.provider.make_request(method, params)
            if "error" in response:
                logger.error(f"Anvil RPC error for {method}: {response['error']}")
                return None
            return response.get("result")
        except Exception as e:
            logger.error(f"Exception during Anvil RPC request {method}: {e}")
            return None

    @classmethod
    def fund_account_with_eth(cls, account_address, amount_wei):
        logger.info(f"Funding {account_address} with {Web3.from_wei(amount_wei, 'ether')} ETH on Anvil fork...")
        cls.anvil_rpc_request("anvil_setBalance", [account_address, hex(amount_wei)])
        balance = cls.w3.eth.get_balance(account_address)
        logger.info(f"New ETH balance for {account_address}: {Web3.from_wei(balance, 'ether')} ETH")
        assert balance >= amount_wei, "ETH funding failed"


    @classmethod
    def fund_account_with_erc20(cls, recipient_address, token_address, whale_address, amount_smallest_unit, token_symbol="Token"):
        if "0xSOME_" in whale_address.upper() or not whale_address or not Web3.is_address(whale_address): # More robust check
            logger.warning(f"Skipping ERC20 funding for {token_symbol} ({token_address}) as whale address is a placeholder or invalid: '{whale_address}'")
            logger.warning(f"CRITICAL: You MUST set a valid EOA whale address for {token_symbol} in test_dex_utils_anvil.py.")
            return False 

        logger.info(f"Attempting to fund {recipient_address} with {amount_smallest_unit} of {token_symbol} ({token_address}) from whale {whale_address}...")
        token_contract = load_contract(cls.w3, token_address, config.MINIMAL_ERC20_ABI)
        if not token_contract:
            logger.error(f"Failed to load token {token_address} for funding.")
            return False

        # Ensure whale has ETH for gas
        logger.info(f"Ensuring whale {whale_address} has ETH for gas...")
        cls.fund_account_with_eth(whale_address, ETH_FOR_WHALE_TX) # Give whale some ETH

        logger.debug(f"Impersonating whale: {whale_address}")
        cls.anvil_rpc_request("anvil_impersonateAccount", [whale_address])

        funded_successfully = False
        try:
            whale_balance_before = token_contract.functions.balanceOf(whale_address).call()
            logger.info(f"Whale {whale_address} balance of {token_symbol} before transfer: {whale_balance_before}")
            if whale_balance_before < amount_smallest_unit:
                logger.error(f"Whale {whale_address} does not have enough {token_symbol} ({whale_balance_before}) to transfer {amount_smallest_unit}.")
                # Stop impersonating even if transfer fails or is skipped
                cls.anvil_rpc_request("anvil_stopImpersonatingAccount", [whale_address])
                return False 

            tx_hash = token_contract.functions.transfer(recipient_address, amount_smallest_unit).transact({'from': whale_address})
            receipt = cls.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60) 
            if receipt.status != 1:
                logger.error(f"ERC20 transfer from whale failed: {receipt}")
            else:
                logger.info(f"Successfully transferred {amount_smallest_unit} of {token_symbol} to {recipient_address}")
                funded_successfully = True
        except Exception as e:
            logger.error(f"Error transferring ERC20 from whale: {e}", exc_info=True)
        finally:
            logger.debug(f"Stopping impersonation of whale: {whale_address}")
            cls.anvil_rpc_request("anvil_stopImpersonatingAccount", [whale_address])
        
        if funded_successfully:
            balance = token_contract.functions.balanceOf(recipient_address).call()
            logger.info(f"New {token_symbol} balance for {recipient_address}: {balance}")
            if balance < amount_smallest_unit:
                 logger.warning(f"Recipient {token_symbol} balance ({balance}) is less than requested amount ({amount_smallest_unit}). Funding may have effectively failed or partial transfer occurred.")
                 # Depending on strictness, you might want to return False here too
            # self.assertGreaterEqual(balance, amount_smallest_unit, f"ERC20 funding for {token_symbol} failed or whale had insufficient funds.")
        else: # If transfer didn't succeed
            logger.error(f"ERC20 funding for {token_symbol} did not complete successfully.")
        
        return funded_successfully


    # --- Test Cases ---

    def test_01_get_pool_info(self):
        logger.info("Running test_01_get_pool_info...")
        pool_contract = load_contract(self.w3, config.POOL_ADDRESS, config.MINIMAL_POOL_ABI)
        pool_info = dex_utils.get_pool_info(pool_contract)
        self.assertIsNotNone(pool_info, "get_pool_info returned None")
        self.assertIn("token0", pool_info)
        self.assertEqual(Web3.to_checksum_address(pool_info["token0"]), self.actual_pool_token0_address)
        self.assertIn("token1", pool_info)
        self.assertEqual(Web3.to_checksum_address(pool_info["token1"]), self.actual_pool_token1_address)
        self.assertIn("tickSpacing", pool_info)
        logger.info(f"Pool info retrieved: {pool_info}")
        TestDexUtilsAnvil.pool_info_data = pool_info


    def test_02_get_current_pool_state(self):
        logger.info("Running test_02_get_current_pool_state...")
        pool_state = dex_utils.get_current_pool_state(self.pool_contract)
        self.assertIsNotNone(pool_state, "get_current_pool_state returned None.") 
        if pool_state is not None: 
            self.assertIn("sqrtPriceX96", pool_state)
            self.assertIn("tick", pool_state)
            logger.info(f"Current pool state retrieved: Tick={pool_state['tick']}")
            TestDexUtilsAnvil.pool_state_data = pool_state


    def test_03_initial_owner_token_ids(self):
        logger.info("Running test_03_initial_owner_token_ids...")
        token_ids = dex_utils.get_owner_token_ids(self.nft_manager_contract, self.bot_wallet_address)
        self.assertIsInstance(token_ids, list)
        logger.info(f"Initial token IDs for bot: {token_ids}")
        TestDexUtilsAnvil.initial_token_ids_count = len(token_ids) # Store initial count


    def test_04_approve_tokens(self):
        logger.info("Running test_04_approve_tokens...")
        amount_to_approve = 2**256 - 1 

        # Use actual token addresses determined in setUpClass
        token0_to_approve = TestDexUtilsAnvil.cngn_actual_address
        token1_to_approve = TestDexUtilsAnvil.usdc_actual_address
        
        token0_contract_for_approve = load_contract(self.w3, token0_to_approve, config.MINIMAL_ERC20_ABI)
        token1_contract_for_approve = load_contract(self.w3, token1_to_approve, config.MINIMAL_ERC20_ABI)


        logger.info(f"Approving Token0 ({token0_to_approve}) for NFT Manager ({self.nft_manager_contract.address})...")
        success0 = dex_utils.approve_token_if_needed(
            self.w3, token0_to_approve, self.bot_wallet_address,
            self.nft_manager_contract.address, amount_to_approve, self.bot_account.key
        )
        self.assertTrue(success0, f"Approval failed for Token0 ({token0_to_approve})")
        if success0: 
            allowance0 = token0_contract_for_approve.functions.allowance(self.bot_wallet_address, self.nft_manager_contract.address).call()
            self.assertGreaterEqual(allowance0, amount_to_approve, "Token0 allowance not set correctly.")


        logger.info(f"Approving Token1 ({token1_to_approve}) for NFT Manager ({self.nft_manager_contract.address})...")
        success1 = dex_utils.approve_token_if_needed(
            self.w3, token1_to_approve, self.bot_wallet_address,
            self.nft_manager_contract.address, amount_to_approve, self.bot_account.key
        )
        self.assertTrue(success1, f"Approval failed for Token1 ({token1_to_approve})")
        if success1: 
            allowance1 = token1_contract_for_approve.functions.allowance(self.bot_wallet_address, self.nft_manager_contract.address).call()
            self.assertGreaterEqual(allowance1, amount_to_approve, "Token1 allowance not set correctly.")


    def test_05_mint_new_position(self):
        logger.info("Running test_05_mint_new_position...")
        self.assertIsNotNone(TestDexUtilsAnvil.pool_info_data, "Pool info not available for mint test (setUpClass or test_01 failed).")
        self.assertIsNotNone(TestDexUtilsAnvil.pool_state_data, "Pool state not available for mint test (test_02 failed).")

        tick_spacing = TestDexUtilsAnvil.pool_info_data['tickSpacing']
        current_tick = TestDexUtilsAnvil.pool_state_data['tick']
        current_sqrt_price_x96 = TestDexUtilsAnvil.pool_state_data['sqrtPriceX96']
        
        self.assertNotEqual(tick_spacing, 0, "Tick spacing cannot be zero.")

        # Define tick range for the new position
        # You might want to adjust these offsets based on your strategy or test needs
        lower_tick_offset = 20 * tick_spacing 
        upper_tick_offset = 20 * tick_spacing 
        
        target_lower = current_tick - lower_tick_offset
        target_upper = current_tick + upper_tick_offset

        # Align ticks to the nearest tick_spacing
        tick_lower = (target_lower // tick_spacing) * tick_spacing
        tick_upper = (target_upper // tick_spacing) * tick_spacing
        
        if tick_lower >= tick_upper:
            logger.warning(f"Calculated tick_lower ({tick_lower}) >= tick_upper ({tick_upper}). Adjusting to ensure tick_lower < tick_upper.")
            tick_upper = tick_lower + tick_spacing # Ensure upper is at least one spacing away
        
        self.assertLess(tick_lower, tick_upper, "tick_lower must be less than tick_upper for minting.")

        # --- MODIFICATION: Use full available balances for minting ---
        logger.info("Fetching full cNGN and USDC balances for minting...")

        # Load ERC20 contracts for cNGN and USDC using their actual determined addresses
        # TestDexUtilsAnvil.cngn_actual_address and TestDexUtilsAnvil.usdc_actual_address are set in setUpClass
        cngn_token_contract = load_contract(self.w3, TestDexUtilsAnvil.cngn_actual_address, config.MINIMAL_ERC20_ABI)
        usdc_token_contract = load_contract(self.w3, TestDexUtilsAnvil.usdc_actual_address, config.MINIMAL_ERC20_ABI)

        self.assertIsNotNone(cngn_token_contract, f"Failed to load cNGN contract ({TestDexUtilsAnvil.cngn_actual_address}) for balance check.")
        self.assertIsNotNone(usdc_token_contract, f"Failed to load USDC contract ({TestDexUtilsAnvil.usdc_actual_address}) for balance check.")

        # Fetch the current balances
        balance_cngn_for_mint = cngn_token_contract.functions.balanceOf(self.bot_wallet_address).call()
        balance_usdc_for_mint = usdc_token_contract.functions.balanceOf(self.bot_wallet_address).call()

        logger.info(f"Bot's cNGN balance (raw): {balance_cngn_for_mint} for address {TestDexUtilsAnvil.cngn_actual_address}")
        logger.info(f"Bot's USDC balance (raw): {balance_usdc_for_mint} for address {TestDexUtilsAnvil.usdc_actual_address}")

        # Ensure there are tokens to mint. These should be funded in setUpClass.
        self.assertGreater(balance_cngn_for_mint, 0, "Bot cNGN balance is 0. Check funding in setUpClass or previous test steps.")
        self.assertGreater(balance_usdc_for_mint, 0, "Bot USDC balance is 0. Check funding in setUpClass or previous test steps.")

        # Map these balances to amount0Desired and amount1Desired based on the pool's actual token order
        # self.actual_pool_token0_address and self.actual_pool_token1_address are determined in setUpClass from the pool
        amount0_desired_for_mint, amount1_desired_for_mint = 0, 0

        if self.actual_pool_token0_address.lower() == TestDexUtilsAnvil.cngn_actual_address.lower():
            # Pool's token0 is cNGN, so pool's token1 must be USDC
            amount0_desired_for_mint = balance_cngn_for_mint
            amount1_desired_for_mint = balance_usdc_for_mint
            logger.info(f"Pool token0 is cNGN. Using cNGN balance ({amount0_desired_for_mint}) for amount0Desired.")
            logger.info(f"Pool token1 is USDC. Using USDC balance ({amount1_desired_for_mint}) for amount1Desired.")
        elif self.actual_pool_token0_address.lower() == TestDexUtilsAnvil.usdc_actual_address.lower():
            # Pool's token0 is USDC, so pool's token1 must be cNGN
            amount0_desired_for_mint = balance_usdc_for_mint
            amount1_desired_for_mint = balance_cngn_for_mint
            logger.info(f"Pool token0 is USDC. Using USDC balance ({amount0_desired_for_mint}) for amount0Desired.")
            logger.info(f"Pool token1 is cNGN. Using cNGN balance ({amount1_desired_for_mint}) for amount1Desired.")
        else:
            # This case should ideally not be reached if setUpClass correctly identifies and maps tokens.
            error_msg = "CRITICAL: Could not map actual pool token0/token1 to known cNGN/USDC addresses for minting amounts."
            logger.error(error_msg)
            self.fail(error_msg)
        # --- END OF MODIFICATION ---
        
        # Log the chosen amounts for clarity before constructing mint_params
        logger.info(f"Final amount0Desired for mint: {amount0_desired_for_mint}")
        logger.info(f"Final amount1Desired for mint: {amount1_desired_for_mint}")

        # The pre-mint balance check for the bot is now implicitly covered by fetching the balances above.
        # If those balances are 0, the asserts above will fail.

        mint_params = (
            self.actual_pool_token0_address, 
            self.actual_pool_token1_address,
            tick_spacing, 
            tick_lower, 
            tick_upper,
            amount0_desired_for_mint, # Using full balance
            amount1_desired_for_mint, # Using full balance
            0, # Setting amount0Min to 0 for simplicity, adjust if needed
            0, # Setting amount1Min to 0 for simplicity, adjust if needed
            self.bot_wallet_address, 
            int(time.time()) + 600, # 10 minutes from now
            0  # Setting sqrtPriceX96 to 0 for simplicity, adjust if needed
        )
        logger.info(f"Minting with params using full balances: {mint_params}")

        receipt = dex_utils.mint_new_position(
            self.w3, self.nft_manager_contract, self.pool_contract,
            mint_params, self.bot_account, self.bot_wallet_address
        )
        self.assertIsNotNone(receipt, "Mint transaction failed to return a receipt (using full balances).")
        if receipt: 
            self.assertEqual(receipt.status, 1, f"Mint transaction failed (status != 1) when using full balances. Receipt: {receipt}")

        # Allow some time for the node to process the new NFT position
        time.sleep(1) 
        new_token_ids = dex_utils.get_owner_token_ids(self.nft_manager_contract, self.bot_wallet_address)
        
        # Check if number of tokens increased
        # TestDexUtilsAnvil.initial_token_ids_count is set in test_03
        self.assertGreater(len(new_token_ids), TestDexUtilsAnvil.initial_token_ids_count, "Number of token IDs did not increase after minting with full balances.")
        
        # A more robust way to find the new token_id is to find the one not in the initial list
        # Assuming initial_token_ids_list was stored in test_03, e.g., TestDexUtilsAnvil.initial_token_ids_list
        initial_ids_set = set(getattr(TestDexUtilsAnvil, 'initial_token_ids_list', [])) 
        current_ids_set = set(new_token_ids)
        minted_token_id_set = current_ids_set - initial_ids_set
        
        self.assertEqual(len(minted_token_id_set), 1, "Expected exactly one new token ID after minting with full balances.")
        TestDexUtilsAnvil.current_test_token_id = list(minted_token_id_set)[0]
        
        logger.info(f"Mint successful with full balances. New position token ID: {TestDexUtilsAnvil.current_test_token_id}")


    def test_06_get_minted_position_details(self):
        # self.skipTest("Skipping get_minted_position_details until mint is reliably working and token ID is confirmed.")
        logger.info("Running test_06_get_minted_position_details...")
        self.assertIsNotNone(TestDexUtilsAnvil.current_test_token_id, "No token ID from mint test to check details (Test 05 likely failed or was skipped).")
        
        position_details = dex_utils.get_position_details(self.nft_manager_contract, TestDexUtilsAnvil.current_test_token_id)
        self.assertIsNotNone(position_details, "Failed to get details for minted position.")
        if position_details: 
            self.assertGreater(position_details['liquidity'], 0, "Minted position has no liquidity.")
            logger.info(f"Details of minted position {TestDexUtilsAnvil.current_test_token_id}: Liquidity={position_details['liquidity']}")
            TestDexUtilsAnvil.current_position_liquidity = position_details['liquidity']


    def test_07_decrease_liquidity_and_collect(self):
        # self.skipTest("Skipping decrease_liquidity_and_collect until mint and get_details are reliably working.")
        logger.info("Running test_07_decrease_liquidity_and_collect...")
        self.assertIsNotNone(TestDexUtilsAnvil.current_test_token_id, "No token ID from mint test (Test 05 likely failed or was skipped).")
        self.assertTrue(hasattr(TestDexUtilsAnvil, 'current_position_liquidity') and TestDexUtilsAnvil.current_position_liquidity is not None, 
                        "No liquidity info from minted position (Test 06 likely failed or was skipped).")
        
        token_id_to_modify = TestDexUtilsAnvil.current_test_token_id
        
        # Ensure current_position_liquidity is an int or can be compared to 0
        self.assertIsInstance(TestDexUtilsAnvil.current_position_liquidity, int, "current_position_liquidity is not an integer.")
        self.assertGreater(TestDexUtilsAnvil.current_position_liquidity, 0, "Position liquidity is zero, cannot decrease.")

        liquidity_to_remove = TestDexUtilsAnvil.current_position_liquidity // 2 

        if liquidity_to_remove == 0 and TestDexUtilsAnvil.current_position_liquidity > 0:
            liquidity_to_remove = TestDexUtilsAnvil.current_position_liquidity 

        self.assertGreater(liquidity_to_remove, 0, "Calculated liquidity_to_remove is zero, cannot test decrease.")

        logger.info(f"Decreasing liquidity by {liquidity_to_remove} for token ID {token_id_to_modify}")
        success = dex_utils.decrease_liquidity_and_collect_position(
            self.w3, self.nft_manager_contract, token_id_to_modify,
            liquidity_to_remove, 0, 0, 
            self.bot_wallet_address, self.bot_account, self.bot_wallet_address
        )
        self.assertTrue(success, "decrease_liquidity_and_collect_position failed.")

        updated_details = dex_utils.get_position_details(self.nft_manager_contract, token_id_to_modify)
        self.assertIsNotNone(updated_details, "Failed to get details after decreasing liquidity.")
        if updated_details: 
            self.assertEqual(updated_details['liquidity'], TestDexUtilsAnvil.current_position_liquidity - liquidity_to_remove, "Liquidity not decreased as expected.")
            logger.info(f"Liquidity after partial decrease: {updated_details['liquidity']}")
            TestDexUtilsAnvil.current_position_liquidity = updated_details['liquidity'] 


    def test_08_decrease_liquidity_to_zero_and_burn(self):
        # self.skipTest("Skipping decrease_liquidity_to_zero_and_burn until previous liquidity tests are reliably working.")
        logger.info("Running test_08_decrease_liquidity_to_zero_and_burn...")
        self.assertIsNotNone(TestDexUtilsAnvil.current_test_token_id, "No token ID from previous tests (Test 05 likely failed or was skipped).")
        self.assertTrue(hasattr(TestDexUtilsAnvil, 'current_position_liquidity') and TestDexUtilsAnvil.current_position_liquidity is not None, 
                        "No current liquidity info (Test 06/07 likely failed or was skipped).")

        token_id_to_burn = TestDexUtilsAnvil.current_test_token_id
        remaining_liquidity = TestDexUtilsAnvil.current_position_liquidity
        
        self.assertIsInstance(remaining_liquidity, int, "remaining_liquidity is not an integer.")


        if remaining_liquidity > 0:
            logger.info(f"Decreasing remaining liquidity ({remaining_liquidity}) to zero for token ID {token_id_to_burn}")
            success_decrease = dex_utils.decrease_liquidity_and_collect_position(
                self.w3, self.nft_manager_contract, token_id_to_burn,
                remaining_liquidity, 0, 0, self.bot_wallet_address,
                self.bot_account, self.bot_wallet_address
            )
            self.assertTrue(success_decrease, "Failed to decrease remaining liquidity to zero.")
        
        final_details = dex_utils.get_position_details(self.nft_manager_contract, token_id_to_burn)
        self.assertIsNotNone(final_details, "Failed to get details before burning.")
        if final_details: 
            self.assertEqual(final_details['liquidity'], 0, "Liquidity is not zero before burning.")
            logger.info(f"Liquidity is zero for token ID {token_id_to_burn}. Proceeding to burn.")

            burn_success = dex_utils.burn_nft_position(
                self.w3, self.nft_manager_contract, token_id_to_burn,
                self.bot_account, self.bot_wallet_address
            )
            self.assertTrue(burn_success, "burn_nft_position failed.")
            logger.info(f"Token ID {token_id_to_burn} burned successfully.")

            with self.assertRaises(Exception): 
                 self.nft_manager_contract.functions.ownerOf(token_id_to_burn).call()
            logger.info(f"Verified ownerOf for token ID {token_id_to_burn} raises an error (expected after burn).")
            TestDexUtilsAnvil.current_test_token_id = None 

    def test_09_execute_router_swap(self):
        logger.info("Running test_09_execute_router_swap...")
        self.assertIsNotNone(TestDexUtilsAnvil.cngn_actual_address, "cNGN address not set.")
        self.assertIsNotNone(TestDexUtilsAnvil.usdc_actual_address, "USDC address not set.")

        cngn_contract = load_contract(self.w3, TestDexUtilsAnvil.cngn_actual_address, config.MINIMAL_ERC20_ABI)
        usdc_contract = load_contract(self.w3, TestDexUtilsAnvil.usdc_actual_address, config.MINIMAL_ERC20_ABI)
        router_contract = load_contract(self.w3, config.ROUTER_ADDRESS, config.ROUTER_ABI)
        # Approve router to spend tokens
        amount_to_approve_router = 2**256 - 1
        logger.info(f"Approving cNGN ({TestDexUtilsAnvil.cngn_actual_address}) for Router ({config.ROUTER_ADDRESS})...")
        approve_cngn_router = dex_utils.approve_token_if_needed(
            self.w3, TestDexUtilsAnvil.cngn_actual_address, self.bot_wallet_address,
            config.ROUTER_ADDRESS, amount_to_approve_router, self.bot_account.key
        )
        self.assertTrue(approve_cngn_router, "cNGN approval for Router failed.")

        logger.info(f"Approving USDC ({TestDexUtilsAnvil.usdc_actual_address}) for Router ({config.ROUTER_ADDRESS})...")
        approve_usdc_router = dex_utils.approve_token_if_needed(
            self.w3, TestDexUtilsAnvil.usdc_actual_address, self.bot_wallet_address,
            config.ROUTER_ADDRESS, amount_to_approve_router, self.bot_account.key
        )
        self.assertTrue(approve_usdc_router, "USDC approval for Router failed.")

        # Swap 1: cNGN for USDC
        amount_cngn_to_swap_units = Decimal("10") # Swap 10 cNGN
        amount_cngn_to_swap_raw = int(amount_cngn_to_swap_units * (10**TestDexUtilsAnvil.cngn_actual_decimals))

        bal_cngn_before_swap1 = cngn_contract.functions.balanceOf(self.bot_wallet_address).call()
        bal_usdc_before_swap1 = usdc_contract.functions.balanceOf(self.bot_wallet_address).call()
        logger.info(f"Before swap 1 (cNGN->USDC): cNGN Bal={bal_cngn_before_swap1}, USDC Bal={bal_usdc_before_swap1}")

        self.assertGreaterEqual(bal_cngn_before_swap1, amount_cngn_to_swap_raw, "Insufficient cNGN balance for swap 1.")

        logger.info(f"Executing swap 1: {amount_cngn_to_swap_units} cNGN for USDC...")
        receipt_swap1 = dex_utils.execute_router_swap(
            w3=self.w3,
            router_contract=router_contract,
            account_details={"address": self.bot_wallet_address, "pk": self.bot_account.key.hex()},
            token_in_address=TestDexUtilsAnvil.cngn_actual_address,
            token_out_address=TestDexUtilsAnvil.usdc_actual_address,
            tick_spacing=10, # Hardcoded here to 10, unless changed by pool should be fine
            recipient_address=self.bot_wallet_address,
            amount_in=amount_cngn_to_swap_raw,
            amount_out_minimum=0, # Allow any amount out for test simplicity,
            sqrt_price_limit_x96= config.MIN_SQRT_RATIO + 1
        )
        self.assertIsNotNone(receipt_swap1, "execute_router_swap (cNGN->USDC) returned None.")
        self.assertEqual(receipt_swap1.status, 1, "Swap 1 (cNGN->USDC) transaction failed.")

        # Add a small delay for Anvil to process the state change if needed, though wait_for_transaction_receipt should handle it.
        # self.anvil_rpc_request("anvil_mine", [[1,1]]) # if state isn't updating fast enough
        time.sleep(1) # Small delay

        bal_cngn_after_swap1 = cngn_contract.functions.balanceOf(self.bot_wallet_address).call()
        bal_usdc_after_swap1 = usdc_contract.functions.balanceOf(self.bot_wallet_address).call()
        logger.info(f"After swap 1 (cNGN->USDC): cNGN Bal={bal_cngn_after_swap1}, USDC Bal={bal_usdc_after_swap1}")

        self.assertLess(bal_cngn_after_swap1, bal_cngn_before_swap1, "cNGN balance did not decrease after swap 1.")
        self.assertGreater(bal_usdc_after_swap1, bal_usdc_before_swap1, "USDC balance did not increase after swap 1.")

        # Swap 2: USDC for cNGN
        amount_usdc_to_swap_units = Decimal("10") # Swap 10 USDC
        amount_usdc_to_swap_raw = int(amount_usdc_to_swap_units * (10**TestDexUtilsAnvil.usdc_actual_decimals))

        self.assertGreaterEqual(bal_usdc_after_swap1, amount_usdc_to_swap_raw, "Insufficient USDC balance for swap 2.")

        logger.info(f"Executing swap 2: {amount_usdc_to_swap_units} USDC for cNGN...")
        receipt_swap2 = dex_utils.execute_router_swap(
            w3=self.w3,
            router_contract=router_contract,
            account_details={"address": self.bot_wallet_address, "pk": self.bot_account.key.hex()},
            token_in_address=TestDexUtilsAnvil.usdc_actual_address,
            token_out_address=TestDexUtilsAnvil.cngn_actual_address,
            tick_spacing=10, # hard coded here again
            recipient_address=self.bot_wallet_address,
            amount_in=amount_usdc_to_swap_raw,
            amount_out_minimum=0,
            sqrt_price_limit_x96=config.MAX_SQRT_RATIO-1
        )
        self.assertIsNotNone(receipt_swap2, "execute_router_swap (USDC->cNGN) returned None.")
        self.assertEqual(receipt_swap2.status, 1, "Swap 2 (USDC->cNGN) transaction failed.")
        
        time.sleep(1)

        bal_cngn_after_swap2 = cngn_contract.functions.balanceOf(self.bot_wallet_address).call()
        bal_usdc_after_swap2 = usdc_contract.functions.balanceOf(self.bot_wallet_address).call()
        logger.info(f"After swap 2 (USDC->cNGN): cNGN Bal={bal_cngn_after_swap2}, USDC Bal={bal_usdc_after_swap2}")

        self.assertGreater(bal_cngn_after_swap2, bal_cngn_after_swap1, "cNGN balance did not increase after swap 2.")
        self.assertLess(bal_usdc_after_swap2, bal_usdc_after_swap1, "USDC balance did not decrease after swap 2.")
        logger.info("Router swap tests completed successfully.")


if __name__ == "__main__":
    print("Starting Anvil Test Suite for dex_utils.py")
    print("--------------------------------------------------")
    print("IMPORTANT: Ensure Anvil is running in a separate terminal with a Base mainnet fork:")
    print(f"  anvil --fork-url YOUR_BASE_MAINNET_RPC_FROM_CONFIG_PY")
    original_rpc_for_display = config.RPC_URL
    if hasattr(TestDexUtilsAnvil, 'original_rpc_url') and TestDexUtilsAnvil.original_rpc_url is not None:
        original_rpc_for_display = TestDexUtilsAnvil.original_rpc_url
    print(f"  (Your config.py mainnet RPC_URL is expected to be: {original_rpc_for_display})")
    print("You also MUST replace placeholder CNGN_WHALE_ADDRESS and USDC_WHALE_ADDRESS in this test script with valid EOAs.")
    print("--------------------------------------------------")
    unittest.main()
