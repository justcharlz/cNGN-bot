# test_strategy_anvil.py
import unittest
import time
import logging
from decimal import Decimal, getcontext
import math 

from web3 import Web3

import config 
from blockchain_utils import get_web3_provider, get_wallet_credentials, load_contract
from strategy import Strategy, PriceProvider, CNGN_REFERENCE_ADDRESS, Q96_DEC 

# Configure logger for tests
if not logging.getLogger().hasHandlers():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s][%(filename)s:%(lineno)s] %(message)s")
logger = logging.getLogger(__name__)

ANVIL_RPC_URL = "http://127.0.0.1:8545"

TOKEN0_WHALE_ADDRESS = "0xF7Fceda4d895602de62E54E4289ECFA97B86EBea" # cNGN Whale
TOKEN1_WHALE_ADDRESS = "0x122fDD9fEcbc82F7d4237C0549a5057E31c8EF8D" # USDC Whale

MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342
MIN_SQRT_RATIO = 4295128739

ETH_TO_FUND_ACCOUNTS = Web3.to_wei(10, 'ether')
ETH_FOR_WHALE_OPERATIONS = Web3.to_wei(0.5, 'ether')
INITIAL_TOKEN_UNITS_FUNDING = Decimal("70000") 

original_rpc_url_for_strategy_testing = None

class TestStrategyAnvil(unittest.TestCase):
    w3 = None
    bot_account_details = None 
    pool_for_testing = None # Used for reading pool state and events
    router_contract_for_tests = None # Used for executing swaps
    router_address_cs = None
    
    pool_token0_addr = None
    pool_token1_addr = None
    pool_token0_contract = None
    pool_token1_contract = None
    pool_token0_decimals = None
    pool_token1_decimals = None
    pool_tick_spacing = None

    strategy_under_test: Strategy 

    swapper_account_details = None 
    
    TEST_PRICE_PROVIDER_START_BLOCK_OFFSET = 500 

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

            # Load Pool Contract (for state reads and event decoding)
            cls.pool_for_testing = load_contract(cls.w3, config.POOL_ADDRESS, config.MINIMAL_POOL_ABI) #
            if not cls.pool_for_testing:
                raise RuntimeError(f"Failed to load main pool contract {config.POOL_ADDRESS}")

            # Load Router Contract (for executing swaps)
            cls.router_contract_for_tests = load_contract(
                cls.w3, 
                config.ROUTER_ADDRESS, 
                config.ROUTER_ABI
            ) #
            if not cls.router_contract_for_tests:
                raise RuntimeError(f"Failed to load router contract {config.ROUTER_ADDRESS}") #
            cls.router_address_cs = Web3.to_checksum_address(config.ROUTER_ADDRESS) #


            cls.pool_token0_addr = Web3.to_checksum_address(cls.pool_for_testing.functions.token0().call())
            cls.pool_token1_addr = Web3.to_checksum_address(cls.pool_for_testing.functions.token1().call())
            logger.info(f"Pool ({config.POOL_ADDRESS}) Tokens - Token0: {cls.pool_token0_addr}, Token1: {cls.pool_token1_addr}")

            cls.pool_token0_contract = load_contract(cls.w3, cls.pool_token0_addr, config.MINIMAL_ERC20_ABI)
            cls.pool_token1_contract = load_contract(cls.w3, cls.pool_token1_addr, config.MINIMAL_ERC20_ABI)
            cls.pool_token0_decimals = cls.pool_token0_contract.functions.decimals().call()
            cls.pool_token1_decimals = cls.pool_token1_contract.functions.decimals().call()
            
            cls.pool_tick_spacing = cls.pool_for_testing.functions.tickSpacing().call() #
            logger.info(f"Pool Tick Spacing: {cls.pool_tick_spacing}")


            # Fund Bot and Swapper with Pool Tokens
            amount_t0_fund = int(INITIAL_TOKEN_UNITS_FUNDING * (10**cls.pool_token0_decimals))
            cls._fund_account_erc20(cls.bot_account_details['address'], cls.pool_token0_addr, TOKEN0_WHALE_ADDRESS, amount_t0_fund, f"PoolToken0 (Bot)")
            cls._fund_account_erc20(cls.swapper_account_details['address'], cls.pool_token0_addr, TOKEN0_WHALE_ADDRESS, amount_t0_fund, f"PoolToken0 (Swapper)")

            amount_t1_fund = int(INITIAL_TOKEN_UNITS_FUNDING * (10**cls.pool_token1_decimals))
            cls._fund_account_erc20(cls.bot_account_details['address'], cls.pool_token1_addr, TOKEN1_WHALE_ADDRESS, amount_t1_fund, f"PoolToken1 (Bot)")
            cls._fund_account_erc20(cls.swapper_account_details['address'], cls.pool_token1_addr, TOKEN1_WHALE_ADDRESS, amount_t1_fund, f"PoolToken1 (Swapper)")
            
            # Approve Router to spend Swapper's tokens
            cls._approve_erc20_for_spender(
                owner_acc_details=cls.swapper_account_details, 
                token_contract=cls.pool_token0_contract, 
                spender_address=cls.router_address_cs, # Approve Router
                amount=(2**256 - 1)
            )
            cls._approve_erc20_for_spender(
                owner_acc_details=cls.swapper_account_details,
                token_contract=cls.pool_token1_contract,
                spender_address=cls.router_address_cs, # Approve Router
                amount=(2**256 - 1)
            )
            
            default_start_block_for_testing = 28028682
            logger.info(f"PriceProvider will fetch initial history from block: {default_start_block_for_testing}")

            cls.strategy_under_test = Strategy(
                sd_min_width_ticks=cls.pool_tick_spacing * 20, 
                price_provider_start_block=default_start_block_for_testing 
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
            estimated_gas = approve_fn.estimate_gas({'from': owner_acc_details['address']})
            final_tx_params['gas'] = int(estimated_gas * 1.2) 
        except Exception as e:
            logger.error(f"Gas estimation for approval failed: {e}. Using fallback: 100,000")
            final_tx_params['gas'] = 100000 

        transaction_to_sign = approve_fn.build_transaction(final_tx_params)
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
        # Determine tokenIn and tokenOut for the router based on the direction
        if execute_as_zero_for_one: # Selling token0 (pool_token0_addr) for token1
            token_in_for_router = self.pool_token0_addr
            token_out_for_router = self.pool_token1_addr
            assert token_in_addr_cs.lower() == token_in_for_router.lower(), \
                f"Mismatch in token_in for zeroForOne swap. Expected {token_in_for_router}, got {token_in_addr_cs}"
        else: # Selling token1 (pool_token1_addr) for token0
            token_in_for_router = self.pool_token1_addr
            token_out_for_router = self.pool_token0_addr
            assert token_in_addr_cs.lower() == token_in_for_router.lower(), \
                f"Mismatch in token_in for non-zeroForOne swap. Expected {token_in_for_router}, got {token_in_addr_cs}"

        logger.info(f"Swapper executing ROUTER swap: Amount {amount_in_raw} of token_in {token_in_for_router} to get {token_out_for_router} (zeroForOne={execute_as_zero_for_one})")

        recipient_address = self.swapper_account_details['address']
        deadline = int(time.time()) + 600  # 10 minutes from now
        amount_in_int = int(amount_in_raw)
        amount_out_minimum = 0 # For testing, we usually don't care about slippage with a fixed amount in

        # sqrtPriceLimitX96:
        # If selling token0 (zeroForOne=True), price (token0/token1) moves down, so sqrtPriceX96 moves down. Limit is a floor.
        # If selling token1 (zeroForOne=False), price (token0/token1) moves up, so sqrtPriceX96 moves up. Limit is a ceiling.
        sqrt_price_limit_x96_for_router = (MIN_SQRT_RATIO + 1) if execute_as_zero_for_one else (MAX_SQRT_RATIO - 1)

        params_tuple = (
            Web3.to_checksum_address(token_in_for_router),
            Web3.to_checksum_address(token_out_for_router),
            self.pool_tick_spacing, # Loaded in setUpClass
            recipient_address,
            deadline,
            amount_in_int,
            amount_out_minimum,
            sqrt_price_limit_x96_for_router
        )
        
        base_tx_params = {
            'from': self.swapper_account_details['address'],
            'nonce': self.w3.eth.get_transaction_count(self.swapper_account_details['address']),
            'chainId': self.w3.eth.chain_id,
        }
        latest_block = self.w3.eth.get_block('latest')
        base_fee = latest_block.get('baseFeePerGas', self.w3.to_wei(1, 'gwei'))
        base_tx_params['maxPriorityFeePerGas'] = self.w3.to_wei(1.5, 'gwei')
        base_tx_params['maxFeePerGas'] = base_fee * 2 + base_tx_params['maxPriorityFeePerGas']

        router_swap_function_call = self.router_contract_for_tests.functions.exactInputSingle(params_tuple) #

        final_tx_params = base_tx_params.copy()
        final_tx_params['gas'] = 1_500_000 # Generous gas limit for router swap

        transaction_to_sign = router_swap_function_call.build_transaction(final_tx_params)
        
        pk_hex = self.swapper_account_details['pk']
        signed_tx = self.w3.eth.account.sign_transaction(transaction_to_sign, pk_hex)
        tx_hash_hex = "" 
        try:
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            tx_hash_hex = tx_hash.hex()
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        except Exception as e_tx:
            logger.error(f"Error sending transaction or waiting for receipt (Hash if available: {tx_hash_hex}): {e_tx}", exc_info=True)
            raise

        if receipt.status == 0:
            logger.error(f"Router Swap transaction {tx_hash_hex} FAILED. Receipt: {receipt}")
            try:
                trace_options = {} 
                trace = self.w3.provider.make_request("debug_traceTransaction", [tx_hash_hex, trace_options])
                import json 
                logger.error(f"Anvil debug_traceTransaction for {tx_hash_hex}:\n{json.dumps(trace, indent=2)}")
            except Exception as e_trace:
                logger.error(f"Failed to get debug_traceTransaction for {tx_hash_hex}: {e_trace}")
        
        self.assertEqual(receipt.status, 1, f"Router Swap transaction failed. Hash: {tx_hash_hex}. Receipt: {receipt}")
        logger.info(f"Router Swap executed successfully. Hash: {tx_hash_hex}")

        # Log amountOut by finding the Swap event from the pool in the receipt
        # The router calls the pool, and the pool emits the Swap event.
        for log_entry in receipt.logs:
            if log_entry['address'].lower() == config.POOL_ADDRESS.lower(): # Check if log is from our pool
                try:
                    # process_log needs the contract object that defines the event
                    event_data = self.pool_for_testing.events.Swap().process_log(log_entry) #
                    amount0_delta = event_data['args']['amount0']
                    amount1_delta = event_data['args']['amount1']
                    logger.info(f"Swap Event from Pool {config.POOL_ADDRESS} in tx {tx_hash_hex}: amount0_delta={amount0_delta}, amount1_delta={amount1_delta}") #
                    # Depending on which token was tokenIn, one of these will be positive (amount out) and the other negative (amount in)
                    # For exactInputSingle, amountIn is positive if tokenIn=token0, negative if tokenIn=token1
                    # amountOut is positive if tokenOut=token1, negative if tokenOut=token0
                    # From the perspective of the pool event:
                    # amount0 is positive if token0 is transferred *out* of the pool
                    # amount1 is positive if token1 is transferred *out* of the pool
                    if execute_as_zero_for_one: # token0 in, token1 out
                        # amount0 should be negative (sent to pool), amount1 positive (received from pool)
                        logged_amount_in = -amount0_delta
                        logged_amount_out = amount1_delta
                    else: # token1 in, token0 out
                        # amount1 should be negative (sent to pool), amount0 positive (received from pool)
                        logged_amount_in = -amount1_delta
                        logged_amount_out = amount0_delta
                    
                    logger.info(f"Logged amounts from Swap event: Actual Amount In ({token_in_for_router}): {logged_amount_in}, Actual Amount Out ({token_out_for_router}): {logged_amount_out}")

                except Exception as e_log_proc: # Catch errors during log processing (e.g. topic mismatch)
                    # This might happen if the log isn't a Swap event or ABI is mismatched
                    logger.debug(f"Could not process a log entry from {log_entry['address']} as Swap event: {e_log_proc}")
        
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
        logger.info(f"PriceProvider has {len(pp.all_prices_chronological)} initial prices from block ~{pp.start_block}.")
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
        time.sleep(0.5) 
        current_block_on_chain = self.w3.eth.block_number

        pp.fetch_new_prices()
        
        self.assertEqual(len(pp.all_prices_chronological), initial_history_len, 
                         "Full price history length changed when no new swaps were expected.")
        if last_block_before_mine is not None: 
             self.assertGreaterEqual(pp.last_processed_block, last_block_before_mine)
        self.assertEqual(pp.last_processed_block, current_block_on_chain, "last_processed_block not updated to current chain height after scan.")

    def test_02b_price_provider_fetch_new_prices_with_swaps(self):
        logger.info("### Test 02b: PriceProvider Fetch New Prices (With Intervening Swaps) ###")
        pp = self.strategy_under_test.price_provider
        initial_history_len = len(pp.all_prices_chronological)

        # For zeroForOne=True, token_in is token0
        token_to_swap_contract = self.pool_token0_contract
        token_to_swap_addr = self.pool_token0_addr
        amount_to_swap = int(Decimal("1") * (10**self.pool_token0_decimals))

        swapper_balance = token_to_swap_contract.functions.balanceOf(
            self.swapper_account_details['address']
        ).call()
        logger.info(f"PRE-SWAP CHECK: Swapper balance of {token_to_swap_addr}: {swapper_balance}. Need: {amount_to_swap}")
        self.assertGreaterEqual(swapper_balance, amount_to_swap, "PRE-SWAP FAIL: Insufficient swapper balance.")

        pool_allowance = token_to_swap_contract.functions.allowance(
            self.swapper_account_details['address'], 
            self.router_address_cs  # spender (the router)
        ).call()
        logger.info(f"PRE-SWAP CHECK: Router allowance for swapper's {token_to_swap_addr}: {pool_allowance}. Need: {amount_to_swap}")
        self.assertGreaterEqual(pool_allowance, amount_to_swap, "PRE-SWAP FAIL: Insufficient router allowance.")

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

        if len(pp.get_all_prices_for_sd()) < 3:
            logger.info("Populating price history with a few swaps for SD test...")
            self._execute_pool_swap(self.pool_token0_addr, int(Decimal("50") * (10**self.pool_token0_decimals)), True)
            self._execute_pool_swap(self.pool_token1_addr, int(Decimal("50") * (10**self.pool_token1_decimals)), False)
            self._execute_pool_swap(self.pool_token0_addr, int(Decimal("60") * (10**self.pool_token0_decimals)), True)
            pp.fetch_new_prices() 
        
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
        # It's possible for current_tick to be equal to tick_upper if it's rounded up.
        # A more robust check is that the range [tick_l, tick_u) should ideally contain current_tick,
        # or current_tick should be very close if it's at the edge.
        # The strategy aims for tick_l <= current_tick < tick_u for an *active* position.
        # The calculated target ticks might place the current_tick just at the edge or slightly outside
        # before alignment.
        # self.assertTrue(tick_l <= current_tick < tick_u + s.tick_spacing) # Original check
        # For calculated target_ticks, it might be that current_tick is near the center,
        # or one of the bounds is chosen based on current_tick.
        # More directly: the position created from these ticks should cover the current_tick.
        # This is verified more directly in test_05.

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
        self.assertTrue(pos_details['tickLower'] <= current_tick_after_mint < pos_details['tickUpper'], # V3 positions are active if tickLower <= currentTick < tickUpper
                        f"Newly minted position range [{pos_details['tickLower']}, {pos_details['tickUpper']}] does not cover current_tick {current_tick_after_mint}.")


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
        logger.info("### Test 07: Rebalance After Price Moves Out of Range ###")
        s = self.strategy_under_test
        s._get_existing_position()
        self.assertIsNotNone(s.current_position_token_id, "No initial position found for rebalance test.")
        initial_pos_id = s.current_position_token_id
        initial_pos_range = (s.current_position_details['tickLower'], s.current_position_details['tickUpper'])
        logger.info(f"Position before price move: ID {initial_pos_id}, Range {initial_pos_range}")

        amount_to_swap_t0 = int(Decimal("20000") * (10**self.pool_token0_decimals)) 
        max_swap_attempts = 10
        price_moved_out = False
        current_tick_after_swap, _, _ = s._get_current_tick_and_price_state() 
        
        for i in range(max_swap_attempts):
            logger.info(f"Executing swap {i+1}/{max_swap_attempts} to move price out of range {initial_pos_range}...")
            # Determine which token to sell to move the price *out* of range.
            # If current_tick is too high, sell token0 (execute_as_zero_for_one=True) to lower it.
            # If current_tick is too low, sell token1 (execute_as_zero_for_one=False) to raise it.
            # For this test, we'll try to move it lower by selling token0.
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
            self.assertTrue(new_pos_range[0] <= tick_after_rebalance_logic < new_pos_range[1],
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

        original_full_history = list(pp.all_prices_chronological) 
        
        pp.all_prices_chronological = [
            {'price': 1000.0, 'block': 1, 'logIndex': 0},
            {'price': 1000.0, 'block': 2, 'logIndex': 0}
        ] 

        current_tick, _, p_current_for_sd = s._get_current_tick_and_price_state()
        self.assertIsNotNone(current_tick)
        self.assertIsNotNone(p_current_for_sd)

        tick_l, tick_u = s._calculate_target_ticks(current_tick, p_current_for_sd) 
        
        self.assertIsNotNone(tick_l, "Fallback tick_lower is None.")
        self.assertIsNotNone(tick_u, "Fallback tick_upper is None.")
        self.assertLess(tick_l, tick_u, "Fallback tick_lower not less than tick_upper.")
        
        actual_width = tick_u - tick_l
        # Allow for slight rounding differences due to tick_spacing alignment
        self.assertGreaterEqual(actual_width, s.sd_min_width_ticks - s.tick_spacing, 
                                f"Fallback width {actual_width} too small vs expected ~{s.sd_min_width_ticks}")
        self.assertLessEqual(actual_width, s.sd_min_width_ticks + s.tick_spacing,
                                f"Fallback width {actual_width} too large vs expected ~{s.sd_min_width_ticks}")

        logger.info(f"Fallback ticks (due to invalid SD from insufficient full history): [{tick_l}, {tick_u}], Width: {actual_width}, Target Width: {s.sd_min_width_ticks}")

        pp.all_prices_chronological = original_full_history
        pp.fetch_new_prices() 


if __name__ == "__main__":
    print("Starting Anvil Test Suite for strategy.py (Full Price History SD, Router Swaps)...")
    print("----------------------------------------------------------------------")
    print("IMPORTANT: Ensure Anvil is running in a separate terminal with a")
    mainnet_rpc = config.RPC_URL if not original_rpc_url_for_strategy_testing else original_rpc_url_for_strategy_testing
    print(f"   Base mainnet fork: anvil --fork-url {mainnet_rpc}")
    print("   Ensure whale addresses in this script are valid and have funds on Base for the forked block.")
    print("   Ensure config.MINIMAL_POOL_ABI includes the 'Swap' event.")
    print("----------------------------------------------------------------------")
    
    loader = unittest.TestLoader()
    # Ensure tests run in defined order (e.g., test_01_..., test_02_...)
    loader.sortTestMethodsUsing = lambda x, y: (x > y) - (x < y) 
    suite = loader.loadTestsFromTestCase(TestStrategyAnvil)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    if not result.wasSuccessful():
        print("\n--- SOME TESTS FAILED ---")
    else:
        print("\n--- ALL TESTS PASSED ---")