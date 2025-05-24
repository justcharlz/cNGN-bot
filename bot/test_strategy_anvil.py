# test_strategy_anvil.py
import unittest
import time
import logging
from decimal import Decimal, getcontext
import math 

from web3 import Web3

import config 
from blockchain_utils import get_web3_provider, get_wallet_credentials, load_contract
from strategy import Strategy, PriceProvider, CNGN_REFERENCE_ADDRESS, Q96_DEC # PriceProvider for type hints

# Configure logger for tests
# Basic config is in strategy.py, but ensure it's effective for tests too.
# If strategy.py's logger isn't configured when test_strategy_anvil is run directly as __main__,
# this ensures test logs are seen.
if not logging.getLogger().hasHandlers():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s][%(filename)s:%(lineno)s] %(message)s")
logger = logging.getLogger(__name__)

ANVIL_RPC_URL = "http://127.0.0.1:8545"

# Whale addresses from previous context, ensure these are correct for the pool tokens
TOKEN0_WHALE_ADDRESS = "0xF7Fceda4d895602de62E54E4289ECFA97B86EBea" # cNGN Whale
TOKEN1_WHALE_ADDRESS = "0x122fDD9fEcbc82F7d4237C0549a5057E31c8EF8D" # USDC Whale

MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342
MIN_SQRT_RATIO = 4295128739

ETH_TO_FUND_ACCOUNTS = Web3.to_wei(10, 'ether')
ETH_FOR_WHALE_OPERATIONS = Web3.to_wei(0.5, 'ether')
INITIAL_TOKEN_UNITS_FUNDING = Decimal("70000") # Slightly increased for more test swaps

original_rpc_url_for_strategy_testing = None

class TestStrategyAnvil(unittest.TestCase):
    w3 = None
    bot_account_details = None 
    pool_for_testing = None    
    
    pool_token0_addr = None
    pool_token1_addr = None
    pool_token0_contract = None
    pool_token1_contract = None
    pool_token0_decimals = None
    pool_token1_decimals = None

    strategy_under_test: Strategy # Type hint for clarity
    # price_provider_on_strategy: PriceProvider # Access via strategy_under_test.price_provider

    swapper_account_details = None 
    swappable_pool_contract_for_tests = None # Assumes config.MINIMAL_POOL_ABI now includes 'swap'

    TEST_PRICE_PROVIDER_START_BLOCK_OFFSET = 500 # Fetch last 500 blocks for tests for PriceProvider

    @classmethod
    def setUpClass(cls):
        global original_rpc_url_for_strategy_testing
        logger.info("### Setting up Anvil Test Environment for Strategy Suite (Full History SD) ###")
        original_rpc_url_for_strategy_testing = config.RPC_URL
        config.RPC_URL = ANVIL_RPC_URL 

        try:
            cls.w3 = get_web3_provider()
            if not cls.w3.is_connected():
                raise ConnectionError(f"Failed to connect to Anvil at {ANVIL_RPC_URL}.")
            logger.info(f"Successfully connected to Anvil: {ANVIL_RPC_URL}, Chain ID: {cls.w3.eth.chain_id}")

            # Bot Account Setup
            bot_acc_obj, bot_addr = get_wallet_credentials(config.DUMMY_PRIVATE_KEY)
            cls.bot_account_details = {"account": bot_acc_obj, "address": bot_addr, "pk": config.DUMMY_PRIVATE_KEY}
            logger.info(f"Bot Account: {cls.bot_account_details['address']}")
            cls._fund_account_eth(cls.bot_account_details['address'], ETH_TO_FUND_ACCOUNTS)

            # Swapper Account Setup
            swapper_acc_obj = cls.w3.eth.account.create() 
            cls.swapper_account_details = {"account": swapper_acc_obj, "address": swapper_acc_obj.address, "pk": swapper_acc_obj.key.hex()}
            logger.info(f"Swapper Account: {cls.swapper_account_details['address']}")
            cls._fund_account_eth(cls.swapper_account_details['address'], ETH_TO_FUND_ACCOUNTS)

            # Load Pool and Token Contracts & Details
            cls.pool_for_testing = load_contract(cls.w3, config.POOL_ADDRESS, config.MINIMAL_POOL_ABI)
            if not cls.pool_for_testing:
                raise RuntimeError(f"Failed to load main pool contract {config.POOL_ADDRESS}")

            cls.pool_token0_addr = Web3.to_checksum_address(cls.pool_for_testing.functions.token0().call())
            cls.pool_token1_addr = Web3.to_checksum_address(cls.pool_for_testing.functions.token1().call())
            logger.info(f"Pool ({config.POOL_ADDRESS}) Tokens - Token0: {cls.pool_token0_addr}, Token1: {cls.pool_token1_addr}")

            cls.pool_token0_contract = load_contract(cls.w3, cls.pool_token0_addr, config.MINIMAL_ERC20_ABI)
            cls.pool_token1_contract = load_contract(cls.w3, cls.pool_token1_addr, config.MINIMAL_ERC20_ABI)
            cls.pool_token0_decimals = cls.pool_token0_contract.functions.decimals().call()
            cls.pool_token1_decimals = cls.pool_token1_contract.functions.decimals().call()

            # Fund Bot and Swapper with Pool Tokens
            amount_t0_fund = int(INITIAL_TOKEN_UNITS_FUNDING * (10**cls.pool_token0_decimals))
            cls._fund_account_erc20(cls.bot_account_details['address'], cls.pool_token0_addr, TOKEN0_WHALE_ADDRESS, amount_t0_fund, f"PoolToken0 (Bot)")
            cls._fund_account_erc20(cls.swapper_account_details['address'], cls.pool_token0_addr, TOKEN0_WHALE_ADDRESS, amount_t0_fund, f"PoolToken0 (Swapper)")

            amount_t1_fund = int(INITIAL_TOKEN_UNITS_FUNDING * (10**cls.pool_token1_decimals))
            cls._fund_account_erc20(cls.bot_account_details['address'], cls.pool_token1_addr, TOKEN1_WHALE_ADDRESS, amount_t1_fund, f"PoolToken1 (Bot)")
            cls._fund_account_erc20(cls.swapper_account_details['address'], cls.pool_token1_addr, TOKEN1_WHALE_ADDRESS, amount_t1_fund, f"PoolToken1 (Swapper)")
            
            pool_checksum_addr = Web3.to_checksum_address(config.POOL_ADDRESS)
            cls._approve_erc20_for_spender(
                owner_acc_details=cls.swapper_account_details, 
                token_contract=cls.pool_token0_contract, 
                spender_address=pool_checksum_addr, 
                amount=(2**256 - 1)
            )
            cls._approve_erc20_for_spender(
                owner_acc_details=cls.swapper_account_details,
                token_contract=cls.pool_token1_contract,
                spender_address=pool_checksum_addr,
                amount=(2**256 - 1)
            )
            
            cls.swappable_pool_contract_for_tests = cls.w3.eth.contract(address=config.POOL_ADDRESS, abi=config.MINIMAL_POOL_ABI)
            if 'swap' not in cls.swappable_pool_contract_for_tests.functions:
                 logger.warning(f"The ABI for pool {config.POOL_ADDRESS} (from config.MINIMAL_POOL_ABI) does not seem to include the 'swap' function. Swap tests might fail.")


            default_start_block_for_testing = 28028682
            logger.info(f"PriceProvider will fetch initial history from block: {default_start_block_for_testing}")

            pool_tick_spacing = cls.pool_for_testing.functions.tickSpacing().call()
            cls.strategy_under_test = Strategy(
                sd_min_width_ticks=pool_tick_spacing * 20, # e.g., +/- 10 tick spacings from center
                price_provider_start_block=default_start_block_for_testing #
            )
            logger.info("Strategy instance created; PriceProvider initial price fetch complete.")

        except Exception as e_setup:
            logger.critical(f"FATAL ERROR during TestStrategyAnvil.setUpClass: {e_setup}", exc_info=True)
            if original_rpc_url_for_strategy_testing is not None: 
                config.RPC_URL = original_rpc_url_for_strategy_testing
            raise 

    @classmethod
    def tearDownClass(cls):
        global original_rpc_url_for_strategy_testing
        logger.info("### Tearing down Anvil Test Environment for Strategy Suite ###")
        if original_rpc_url_for_strategy_testing is not None:
            config.RPC_URL = original_rpc_url_for_strategy_testing 
            logger.info(f"Original RPC URL ({config.RPC_URL}) restored.")
        else:
            logger.warning("Original RPC URL was not captured; cannot restore.")

    @classmethod
    def _anvil_rpc(cls, method, params=[]):
        return cls.w3.provider.make_request(method, params)

    @classmethod
    def _fund_account_eth(cls, account_address_cs, amount_wei):
        logger.debug(f"Funding {account_address_cs} with {cls.w3.from_wei(amount_wei, 'ether')} ETH...")
        cls._anvil_rpc("anvil_setBalance", [account_address_cs, hex(amount_wei)])
        balance = cls.w3.eth.get_balance(account_address_cs)
        assert balance >= amount_wei, f"ETH funding failed for {account_address_cs}"
        logger.debug(f"ETH Balance for {account_address_cs}: {cls.w3.from_wei(balance, 'ether')} ETH")

    @classmethod
    def _fund_account_erc20(cls, recipient_addr_cs, token_addr_cs, whale_addr_cs, amount_raw, token_sym=""):
        logger.debug(f"Funding {recipient_addr_cs} with {amount_raw} of {token_sym} ({token_addr_cs}) from whale {whale_addr_cs}")
        token_contract = load_contract(cls.w3, token_addr_cs, config.MINIMAL_ERC20_ABI)
        cls._fund_account_eth(whale_addr_cs, ETH_FOR_WHALE_OPERATIONS)
        cls._anvil_rpc("anvil_impersonateAccount", [whale_addr_cs])
        try:
            whale_bal = token_contract.functions.balanceOf(whale_addr_cs).call()
            if whale_bal < amount_raw:
                raise ValueError(f"Whale {whale_addr_cs} has insufficient {token_sym} ({whale_bal}) for requested {amount_raw}")
            tx_params = {'from': whale_addr_cs, 'nonce': cls.w3.eth.get_transaction_count(whale_addr_cs)}
            latest_block = cls.w3.eth.get_block('latest')
            base_fee = latest_block.get('baseFeePerGas', cls.w3.to_wei(1, 'gwei'))
            tx_params['maxPriorityFeePerGas'] = cls.w3.to_wei(1.5, 'gwei')
            tx_params['maxFeePerGas'] = base_fee * 2 + tx_params['maxPriorityFeePerGas']
            transfer_fn = token_contract.functions.transfer(recipient_addr_cs, amount_raw)
            tx_params['gas'] = int(transfer_fn.estimate_gas(tx_params) * 1.2)
            tx_hash = transfer_fn.transact(tx_params)
            receipt = cls.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            assert receipt.status == 1, f"ERC20 transfer from whale {whale_addr_cs} failed."
        finally:
            cls._anvil_rpc("anvil_stopImpersonatingAccount", [whale_addr_cs])
        recipient_bal = token_contract.functions.balanceOf(recipient_addr_cs).call()
        assert recipient_bal >= amount_raw, f"{token_sym} funding failed for {recipient_addr_cs}"
        logger.debug(f"{token_sym} Balance for {recipient_addr_cs}: {recipient_bal}")

    @classmethod
    def _approve_erc20_for_spender(cls, owner_acc_details, token_contract, spender_address, amount):
        logger.debug(f"Approving {spender_address} for {amount} of {token_contract.address} by {owner_acc_details['address']}")
        
        # Base transaction parameters
        base_tx_params = {
            'from': owner_acc_details['address'],
            'nonce': cls.w3.eth.get_transaction_count(owner_acc_details['address']),
            'chainId': cls.w3.eth.chain_id
        }
        latest_block = cls.w3.eth.get_block('latest')
        base_fee = latest_block.get('baseFeePerGas', cls.w3.to_wei(1, 'gwei'))
        base_tx_params['maxPriorityFeePerGas'] = cls.w3.to_wei(1.5, 'gwei')
        base_tx_params['maxFeePerGas'] = base_fee * 2 + base_tx_params['maxPriorityFeePerGas']

        approve_fn = token_contract.functions.approve(spender_address, amount)
        
        final_tx_params = base_tx_params.copy()
        try:
            # Estimate gas. 'from' is needed if the contract function uses msg.sender.
            # estimate_gas on a function object doesn't need 'to' in its params.
            estimated_gas = approve_fn.estimate_gas({'from': owner_acc_details['address']})
            final_tx_params['gas'] = int(estimated_gas * 1.2) # Add 20% buffer
        except Exception as e:
            logger.error(f"Gas estimation for approval failed: {e}. Using fallback: 100,000")
            final_tx_params['gas'] = 100000 # Fallback gas for approval

        # Build the transaction; 'to' will be correctly set by build_transaction.
        transaction_to_sign = approve_fn.build_transaction(final_tx_params)

        # Convert private key from hex string to bytes
        pk_hex = owner_acc_details['pk']

        signed_tx = cls.w3.eth.account.sign_transaction(transaction_to_sign, pk_hex)
        tx_hash = cls.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        receipt = cls.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        assert receipt.status == 1, (
            f"Approval failed for token {token_contract.address} to spender {spender_address}. "
            f"TxHash: {tx_hash.hex()}. Receipt: {receipt}"
        )
        logger.debug(f"Approval successful for {token_contract.address} to spender {spender_address}")

    def _execute_pool_swap(self, token_in_addr_cs, amount_in_raw, execute_as_zero_for_one):
        logger.info(f"Swapper executing swap: Amount {amount_in_raw} of token_in {token_in_addr_cs} (zeroForOne={execute_as_zero_for_one})")
        sqrt_price_limit = (MIN_SQRT_RATIO + 1) if execute_as_zero_for_one else (MAX_SQRT_RATIO - 1)
        
        recipient_address = self.swapper_account_details['address']

        base_tx_params = {
            'from': self.swapper_account_details['address'],
            'nonce': self.w3.eth.get_transaction_count(self.swapper_account_details['address']),
            'chainId': self.w3.eth.chain_id,
        }
        latest_block = self.w3.eth.get_block('latest')
        base_fee = latest_block.get('baseFeePerGas', self.w3.to_wei(1, 'gwei'))
        base_tx_params['maxPriorityFeePerGas'] = self.w3.to_wei(1.5, 'gwei')
        base_tx_params['maxFeePerGas'] = base_fee * 2 + base_tx_params['maxPriorityFeePerGas']

        swap_function_call = self.swappable_pool_contract_for_tests.functions.swap(
            recipient_address,
            execute_as_zero_for_one,
            int(amount_in_raw),
            sqrt_price_limit,
            b''
        )

        final_tx_params = base_tx_params.copy()
        final_tx_params['gas'] = 1_000_000 

        transaction_to_sign = swap_function_call.build_transaction(final_tx_params)
        
        pk_hex = self.swapper_account_details['pk']

        signed_tx = self.w3.eth.account.sign_transaction(transaction_to_sign, pk_hex)
        tx_hash_hex = "" # Initialize to ensure it's available for logging even if send_raw_transaction fails
        try:
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            tx_hash_hex = tx_hash.hex()
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        except Exception as e_tx:
            logger.error(f"Error sending transaction or waiting for receipt (Hash if available: {tx_hash_hex}): {e_tx}", exc_info=True)
            raise

        if receipt.status == 0:
            logger.error(f"Swap transaction {tx_hash_hex} FAILED. Receipt: {receipt}")
            try:
                # Attempt to get debug trace from Anvil. The options dict can be empty.
                trace_options = {} # You can add options like {'disableStorage': True, 'disableMemory': True, 'disableStack': True} to make trace smaller
                trace = self.w3.provider.make_request("debug_traceTransaction", [tx_hash_hex, trace_options])
                import json # For pretty printing
                logger.error(f"Anvil debug_traceTransaction for {tx_hash_hex}:\n{json.dumps(trace, indent=2)}")
            except Exception as e_trace:
                logger.error(f"Failed to get debug_traceTransaction for {tx_hash_hex}: {e_trace}")
        
        self.assertEqual(receipt.status, 1, f"Swap transaction failed. Hash: {tx_hash_hex}. Receipt: {receipt}")
        logger.info(f"Swap executed successfully. Hash: {tx_hash_hex}")
        
        self._anvil_rpc("anvil_mine", [1,1])
        time.sleep(1)

    # --- Test Cases Start Here ---
    def test_01_price_provider_setup_and_initial_full_fetch(self):
        logger.info("### Test 01: PriceProvider Initialization and Initial Full Price Fetch ###")
        pp = self.strategy_under_test.price_provider
        self.assertIsNotNone(pp, "PriceProvider instance is None within Strategy.")
        
        self.assertEqual(pp.cngn_reference_address, CNGN_REFERENCE_ADDRESS.lower())
        self.assertEqual(self.pool_token0_addr.lower(), CNGN_REFERENCE_ADDRESS.lower())
        self.assertTrue(pp.invert_price)

        self.assertIsInstance(pp.all_prices_chronological, list, "Full price history is not a list.")
        # The number of prices depends on activity in the self.TEST_PRICE_PROVIDER_START_BLOCK_OFFSET range.
        # On a mainnet fork, there should be some swap activity.
        # For a robust test, we could make a few swaps *before* strategy init, if start_block is current_block.
        # Since fetch_initial_prices is called in Strategy.__init__, we check its outcome.
        logger.info(f"PriceProvider has {len(pp.all_prices_chronological)} initial prices from block ~{pp.start_block}.")
        # Cannot assert > 0 strictly, as the forked window might have no swaps.
        # But if it does, last_processed_block should be at least the start_block.
        self.assertIsNotNone(pp.last_processed_block, "PriceProvider.last_processed_block not set after initial fetch.")
        if pp.all_prices_chronological:
             self.assertGreaterEqual(pp.last_processed_block, pp.start_block, "Last processed block is before start block after initial fetch.")
        else:
            logger.warning("Initial full price history is empty. Subsequent SD tests might use fallbacks or have limited data.")


    def test_02a_price_provider_fetch_new_prices_no_swaps(self):
        logger.info("### Test 02a: PriceProvider Fetch New Prices (No Intervening Swaps) ###")
        pp = self.strategy_under_test.price_provider
        initial_history_len = len(pp.all_prices_chronological)
        last_block_before_mine = pp.last_processed_block

        self._anvil_rpc("anvil_mine", [3,1]) 
        time.sleep(0.5) # Ensure block timestamp advances if Anvil is super fast
        current_block_on_chain = self.w3.eth.block_number

        pp.fetch_new_prices()
        
        self.assertEqual(len(pp.all_prices_chronological), initial_history_len, 
                         "Full price history length changed when no new swaps were expected.")
        if last_block_before_mine is not None: # Can be None if initial fetch found nothing and set last_processed_block to latest
             self.assertGreaterEqual(pp.last_processed_block, last_block_before_mine)
        self.assertEqual(pp.last_processed_block, current_block_on_chain, "last_processed_block not updated to current chain height after scan.")

    def test_02b_price_provider_fetch_new_prices_with_swaps(self):
        logger.info("### Test 02b: PriceProvider Fetch New Prices (With Intervening Swaps) ###")
        pp = self.strategy_under_test.price_provider
        initial_history_len = len(pp.all_prices_chronological)

        # Determine input token and amount
        # For zeroForOne=True, token_in is token0
        token_to_swap_contract = self.pool_token0_contract
        token_to_swap_addr = self.pool_token0_addr
        amount_to_swap = int(Decimal("1") * (10**self.pool_token0_decimals))

        # Check balance
        swapper_balance = token_to_swap_contract.functions.balanceOf(
            self.swapper_account_details['address']
        ).call()
        logger.info(f"PRE-SWAP CHECK: Swapper balance of {token_to_swap_addr}: {swapper_balance}. Need: {amount_to_swap}")
        self.assertGreaterEqual(swapper_balance, amount_to_swap, "PRE-SWAP FAIL: Insufficient swapper balance.")

        # Check allowance
        pool_allowance = token_to_swap_contract.functions.allowance(
            self.swapper_account_details['address'], # owner
            self.swappable_pool_contract_for_tests.address  # spender (the pool)
        ).call()
        logger.info(f"PRE-SWAP CHECK: Pool allowance for swapper's {token_to_swap_addr}: {pool_allowance}. Need: {amount_to_swap}")
        self.assertGreaterEqual(pool_allowance, amount_to_swap, "PRE-SWAP FAIL: Insufficient pool allowance.")


        # Execute a swap to generate new log(s)
        self._execute_pool_swap(token_to_swap_addr, amount_to_swap, execute_as_zero_for_one=True)
        
        pp.fetch_new_prices()
        
        self.assertGreater(len(pp.all_prices_chronological), initial_history_len,
                           "Full price history did not grow after executing a swap and fetching new prices.")

    def test_03_strategy_initial_position_check(self):
        logger.info("### Test 03: Strategy Initial Position Check (Should Be None) ###")
        self.strategy_under_test._get_existing_position()
        self.assertIsNone(self.strategy_under_test.current_position_token_id)
        self.assertIsNone(self.strategy_under_test.current_position_details)

    def test_04_sd_and_tick_calculation_logic_full_history(self):
        logger.info("### Test 04: Strategy SD and Target Tick Calculation (Full History) ###")
        s = self.strategy_under_test
        pp = s.price_provider

        # Ensure PriceProvider has some history. If initial fetch got nothing, make some swaps.
        if len(pp.get_all_prices_for_sd()) < 3:
            logger.info("Populating price history with a few swaps for SD test...")
            self._execute_pool_swap(self.pool_token0_addr, int(Decimal("50") * (10**self.pool_token0_decimals)), True)
            self._execute_pool_swap(self.pool_token1_addr, int(Decimal("50") * (10**self.pool_token1_decimals)), False)
            self._execute_pool_swap(self.pool_token0_addr, int(Decimal("60") * (10**self.pool_token0_decimals)), True)
            pp.fetch_new_prices() # Fetch these new prices
        
        self.assertGreaterEqual(len(pp.get_all_prices_for_sd()), 3, "Price history still too short for SD test after attempting swaps.")

        sd = s._calculate_price_sd()
        self.assertIsNotNone(sd, "SD calculation returned None with sufficient data from full history.")
        self.assertGreaterEqual(sd, 0.0, "Standard deviation cannot be negative.")

        current_tick, _, p_current_for_sd = s._get_current_tick_and_price_state()
        self.assertIsNotNone(current_tick)
        self.assertIsNotNone(p_current_for_sd)

        tick_l, tick_u = s._calculate_target_ticks(current_tick, p_current_for_sd)
        self.assertIsNotNone(tick_l)
        self.assertIsNotNone(tick_u)
        self.assertLess(tick_l, tick_u, f"tick_lower ({tick_l}) must be less than tick_upper ({tick_u}).")
        self.assertEqual(tick_l % s.tick_spacing, 0)
        self.assertEqual(tick_u % s.tick_spacing, 0)
        self.assertTrue(tick_l <= current_tick < tick_u + s.tick_spacing)


    def test_05_first_position_creation(self):
        logger.info("### Test 05: Strategy First Position Creation ###")
        s = self.strategy_under_test
        s._get_existing_position() 
        self.assertIsNone(s.current_position_token_id, "A position already exists before the first creation test.")
        s._check_and_rebalance() 
        self.assertIsNotNone(s.current_position_token_id, "Strategy failed to create a new position token ID.")
        self.assertIsNotNone(s.current_position_details, "Strategy failed to update position details after creation.")
        self.assertGreater(s.current_position_details['liquidity'], 0, "Newly created position has zero liquidity.")
        logger.info(f"First position created: ID {s.current_position_token_id}, Liquidity {s.current_position_details['liquidity']}")
        current_tick_after_mint, _, _ = s._get_current_tick_and_price_state()
        pos_details = s.current_position_details
        self.assertTrue(pos_details['tickLower'] <= current_tick_after_mint < pos_details['tickUpper'] + s.tick_spacing,
                        "Newly minted position is not around the current tick.")


    def test_06_no_rebalance_when_price_in_range(self):
        logger.info("### Test 06: No Rebalance When Price is Within Range ###")
        s = self.strategy_under_test
        s._get_existing_position() 
        self.assertIsNotNone(s.current_position_token_id, "No position exists to test 'no rebalance' scenario.")
        pos_id_before_check = s.current_position_token_id
        pos_liq_before_check = s.current_position_details['liquidity']
        current_tick, _, _ = s._get_current_tick_and_price_state()
        pos_details = s.current_position_details
        self.assertTrue(pos_details['tickLower'] <= current_tick < pos_details['tickUpper'],
                        f"Test assumption failed: Current tick {current_tick} is outside existing range [{pos_details['tickLower']}, {pos_details['tickUpper']}] before 'no rebalance' check.")
        s._check_and_rebalance() 
        self.assertEqual(s.current_position_token_id, pos_id_before_check, "Position ID changed when no rebalance was expected.")
        self.assertIsNotNone(s.current_position_details, "Position details became None unexpectedly.")
        if s.current_position_details:
             self.assertEqual(s.current_position_details['liquidity'], pos_liq_before_check, "Position liquidity changed when no rebalance was expected.")
        logger.info("No rebalance occurred, as expected.")


    def test_07_rebalance_after_price_moves_out(self):
        # ... (This test remains structurally similar, ensuring swaps are effective)
        logger.info("### Test 07: Rebalance After Price Moves Out of Range ###")
        s = self.strategy_under_test
        s._get_existing_position()
        self.assertIsNotNone(s.current_position_token_id, "No initial position found for rebalance test.")
        initial_pos_id = s.current_position_token_id
        initial_pos_range = (s.current_position_details['tickLower'], s.current_position_details['tickUpper'])
        logger.info(f"Position before price move: ID {initial_pos_id}, Range {initial_pos_range}")

        # Try to move price significantly lower
        amount_to_swap_t0 = int(Decimal("20000") * (10**self.pool_token0_decimals)) # Increased swap amount
        max_swap_attempts = 10
        price_moved_out = False
        current_tick_after_swap = s._get_current_tick_and_price_state()[0] # Get current tick before loop
        for i in range(max_swap_attempts):
            logger.info(f"Executing swap {i+1}/{max_swap_attempts} to move price out of range {initial_pos_range}...")
            self._execute_pool_swap(self.pool_token0_addr, amount_to_swap_t0, execute_as_zero_for_one=True)
            current_tick_after_swap, _, _ = s._get_current_tick_and_price_state()
            logger.info(f"Tick after swap {i+1}: {current_tick_after_swap}")
            if not (initial_pos_range[0] <= current_tick_after_swap < initial_pos_range[1]):
                price_moved_out = True
                break
        self.assertTrue(price_moved_out, f"Failed to move price out of initial range {initial_pos_range} after {max_swap_attempts} swaps. Final tick: {current_tick_after_swap}")
        logger.info("Price moved out of range. Triggering rebalance check...")
        s._check_and_rebalance() 
        self.assertIsNotNone(s.current_position_token_id, "Rebalance failed: No new position ID.")
        self.assertNotEqual(s.current_position_token_id, initial_pos_id, "Rebalance failed: Position ID did not change.")
        self.assertIsNotNone(s.current_position_details, "Rebalance failed: New position details are None.")
        if s.current_position_details: 
            self.assertGreater(s.current_position_details['liquidity'], 0, "Rebalanced position has zero liquidity.")
            new_pos_range = (s.current_position_details['tickLower'], s.current_position_details['tickUpper'])
            logger.info(f"Rebalanced to new position ID {s.current_position_token_id}, New Range {new_pos_range}")
            tick_after_rebalance_logic, _, _ = s._get_current_tick_and_price_state()
            self.assertTrue(new_pos_range[0] <= tick_after_rebalance_logic < new_pos_range[1] + s.tick_spacing,
                            f"New position range {new_pos_range} does not appropriately cover current tick {tick_after_rebalance_logic}.")
        nft_m_contract = load_contract(self.w3, config.NONFUNGIBLE_POSITION_MANAGER_ADDRESS, config.MINIMAL_NONFUNGIBLE_POSITION_MANAGER_ABI)
        with self.assertRaises(Exception, msg="Old position NFT was not burned or ownerOf check failed."):
            nft_m_contract.functions.ownerOf(initial_pos_id).call()
        logger.info(f"Verified old position ID {initial_pos_id} is no longer valid (burned).")

    def test_08_position_removal(self):
        logger.info("### Test 08: Strategy Position Removal ###")
        s = self.strategy_under_test
        s._get_existing_position()
        if not s.current_position_token_id:
            logger.info("No position found, creating one for removal test...")
            s._check_and_rebalance()
            self.assertIsNotNone(s.current_position_token_id, "Failed to create pre-requisite position for removal.")
        token_id_to_remove = s.current_position_token_id
        logger.info(f"Attempting to remove position ID: {token_id_to_remove}")
        removal_success = s._remove_position(token_id_to_remove)
        self.assertTrue(removal_success, "Strategy's _remove_position method failed.")
        self.assertIsNone(s.current_position_token_id, "current_position_token_id not cleared after removal.")
        self.assertIsNone(s.current_position_details, "current_position_details not cleared after removal.")
        nft_m_contract = load_contract(self.w3, config.NONFUNGIBLE_POSITION_MANAGER_ADDRESS, config.MINIMAL_NONFUNGIBLE_POSITION_MANAGER_ABI)
        with self.assertRaises(Exception, msg="Position NFT was not burned or ownerOf check failed after _remove_position."):
            nft_m_contract.functions.ownerOf(token_id_to_remove).call()
        logger.info(f"Verified position ID {token_id_to_remove} is no longer valid after removal.")


    def test_09_fallback_tick_calculation_full_history_sd_invalid(self):
        logger.info("### Test 09: Fallback Tick Calculation (Full History SD Invalid) ###")
        s = self.strategy_under_test
        pp = s.price_provider

        # Save original full history to restore later
        original_full_history = list(pp.all_prices_chronological) 
        
        # Force conditions for SD to be invalid: e.g. set all_prices_chronological to have < 3 data points
        pp.all_prices_chronological = [
            {'price': 1000.0, 'block': 1, 'logIndex': 0},
            {'price': 1000.0, 'block': 2, 'logIndex': 0}
        ] # Only two points

        current_tick, _, p_current_for_sd = s._get_current_tick_and_price_state()
        self.assertIsNotNone(current_tick)
        self.assertIsNotNone(p_current_for_sd)

        tick_l, tick_u = s._calculate_target_ticks(current_tick, p_current_for_sd) # Should use fallback
        
        self.assertIsNotNone(tick_l, "Fallback tick_lower is None.")
        self.assertIsNotNone(tick_u, "Fallback tick_upper is None.")
        self.assertLess(tick_l, tick_u, "Fallback tick_lower not less than tick_upper.")
        
        expected_width = s.sd_min_width_ticks
        actual_width = tick_u - tick_l
        self.assertGreaterEqual(actual_width, s.sd_min_width_ticks - s.tick_spacing*2)
        self.assertGreaterEqual(actual_width, s.tick_spacing)
        logger.info(f"Fallback ticks (due to invalid SD from insufficient full history): [{tick_l}, {tick_u}], Width: {actual_width}")

        # Restore original full history
        pp.all_prices_chronological = original_full_history
        # It's also good to re-fetch new prices to ensure last_processed_block is consistent if other tests follow
        # Or re-initialize PriceProvider if state becomes too complex to manage across tests.
        # For now, simple restoration. For more complex state, consider finer-grained setup/teardown for price_provider state.
        pp.fetch_new_prices() # Refresh to align last_processed_block


if __name__ == "__main__":
    print("Starting Anvil Test Suite for strategy.py (Full Price History SD)...")

    print("----------------------------------------------------------------------")
    print("IMPORTANT: Ensure Anvil is running in a separate terminal with a")
    print(f"   Base mainnet fork: anvil --fork-url <YOUR_BASE_MAINNET_RPC_URL>")
    print(f"   (e.g., RPC from config.py: {config.RPC_URL if not original_rpc_url_for_strategy_testing else original_rpc_url_for_strategy_testing} )")
    print("   Ensure whale addresses in this script are valid and have funds on Base for the forked block.")
    print("   Ensure config.MINIMAL_POOL_ABI includes the 'swap' function if not already present.")
    print("----------------------------------------------------------------------")
    
    loader = unittest.TestLoader()
    loader.sortTestMethodsUsing = lambda x, y: (x > y) - (x < y) 
    suite = loader.loadTestsFromTestCase(TestStrategyAnvil)
    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite)