import unittest
import time
import logging
from decimal import Decimal
from unittest.mock import patch
import pandas as pd
from web3 import Web3
import os

import bot.config as config
from bot.utils.blockchain_utils import get_web3_provider, get_wallet_credentials, load_contract
from bot.strategy import Strategy 
from bot.price_provider import PriceProvider 
import bot.utils.dex_utils as dex_utils # Ensure this import is present

# Configure logger for tests
if not logging.getLogger().hasHandlers():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s][%(filename)s:%(lineno)s] %(message)s")
logger = logging.getLogger(__name__)

ANVIL_RPC_URL = "http://127.0.0.1:8545"
TOKEN0_WHALE_ADDRESS = "0xF7Fceda4d895602de62E54E4289ECFA97B86EBea" 
TOKEN1_WHALE_ADDRESS = "0x122fDD9fEcbc82F7d4237C0549a5057E31c8EF8D" 

ETH_TO_FUND_ACCOUNTS = Web3.to_wei(10, 'ether')
ETH_FOR_WHALE_OPERATIONS = Web3.to_wei(0.5, 'ether')
INITIAL_TOKEN_UNITS_FUNDING = Decimal("1000000")
original_rpc_url_for_strategy_testing = None

class TestStrategyAnvil(unittest.TestCase):
    w3 = None
    bot_account_details = None
    pool_for_testing = None
    router_contract_for_tests = None
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
    default_start_block_for_tests = 28028682 
    
    price_cache_file = None

    @classmethod
    def setUpClass(cls):
        global original_rpc_url_for_strategy_testing
        logger.info("### Setting up Anvil Test Environment for Strategy Suite ###")
        original_rpc_url_for_strategy_testing = config.RPC_URL
        config.RPC_URL = ANVIL_RPC_URL

        try:
            cls.w3 = get_web3_provider()
            if not cls.w3.is_connected():
                raise ConnectionError(f"Failed to connect to Anvil at {ANVIL_RPC_URL}.")
            logger.info(f"Successfully connected to Anvil: {ANVIL_RPC_URL}, Chain ID: {cls.w3.eth.chain_id}")

            bot_acc_obj, bot_addr = get_wallet_credentials(config.DUMMY_PRIVATE_KEY)
            cls.bot_account_details = {"account": bot_acc_obj, "address": bot_addr, "pk": config.DUMMY_PRIVATE_KEY}
            logger.info(f"Bot Account: {cls.bot_account_details['address']}")
            cls._fund_account_eth(cls.bot_account_details['address'], ETH_TO_FUND_ACCOUNTS)

            swapper_acc_obj = cls.w3.eth.account.create()
            cls.swapper_account_details = {"account": swapper_acc_obj, "address": swapper_acc_obj.address, "pk": swapper_acc_obj.key.hex()}
            logger.info(f"Swapper Account: {cls.swapper_account_details['address']}")
            cls._fund_account_eth(cls.swapper_account_details['address'], ETH_TO_FUND_ACCOUNTS)

            cls.pool_for_testing  = load_contract(cls.w3, config.POOL_ADDRESS, config.MINIMAL_POOL_ABI)
            pool_info = dex_utils.get_pool_info(cls.pool_for_testing) # Use dex_utils to get info
            if not pool_info: raise RuntimeError(f"Failed to get pool info for {config.POOL_ADDRESS}")
            cls.pool_tick_spacing = pool_info['tickSpacing'] # Store tick spacing
            logger.info(f"Pool Tick Spacing: {cls.pool_tick_spacing}")


            cls.router_contract_for_tests = load_contract(cls.w3, config.ROUTER_ADDRESS, config.ROUTER_ABI)
            if not cls.router_contract_for_tests: raise RuntimeError(f"Failed to load router contract {config.ROUTER_ADDRESS}")
            cls.router_address_cs = Web3.to_checksum_address(config.ROUTER_ADDRESS)

            cls.pool_token0_addr = Web3.to_checksum_address(cls.pool_for_testing.functions.token0().call())
            cls.pool_token1_addr = Web3.to_checksum_address(cls.pool_for_testing.functions.token1().call())
            logger.info(f"Pool ({config.POOL_ADDRESS}) Tokens - Token0: {cls.pool_token0_addr}, Token1: {cls.pool_token1_addr}")

            cls.pool_token0_contract = load_contract(cls.w3, cls.pool_token0_addr, config.MINIMAL_ERC20_ABI)
            cls.pool_token1_contract = load_contract(cls.w3, cls.pool_token1_addr, config.MINIMAL_ERC20_ABI)
            cls.pool_token0_decimals = cls.pool_token0_contract.functions.decimals().call()
            cls.pool_token1_decimals = cls.pool_token1_contract.functions.decimals().call()
            # cls.pool_tick_spacing = cls.pool_for_testing.functions.tickSpacing().call() # Already got from pool_info
            # logger.info(f"Pool Tick Spacing: {cls.pool_tick_spacing}")


            amount_t0_fund = int(INITIAL_TOKEN_UNITS_FUNDING * (10**cls.pool_token0_decimals))
            cls._fund_account_erc20(cls.bot_account_details['address'], cls.pool_token0_addr, TOKEN0_WHALE_ADDRESS, amount_t0_fund, f"PoolToken0 (Bot)")
            cls._fund_account_erc20(cls.swapper_account_details['address'], cls.pool_token0_addr, TOKEN0_WHALE_ADDRESS, amount_t0_fund, f"PoolToken0 (Swapper)")
            amount_t1_fund = int(INITIAL_TOKEN_UNITS_FUNDING * (10**cls.pool_token1_decimals))
            cls._fund_account_erc20(cls.bot_account_details['address'], cls.pool_token1_addr, TOKEN1_WHALE_ADDRESS, amount_t1_fund, f"PoolToken1 (Bot)")
            cls._fund_account_erc20(cls.swapper_account_details['address'], cls.pool_token1_addr, TOKEN1_WHALE_ADDRESS, amount_t1_fund, f"PoolToken1 (Swapper)")

            cls._approve_erc20_for_spender(owner_acc_details=cls.swapper_account_details, token_contract=cls.pool_token0_contract, spender_address=cls.router_address_cs, amount=(2**256 - 1))
            cls._approve_erc20_for_spender(owner_acc_details=cls.swapper_account_details, token_contract=cls.pool_token1_contract, spender_address=cls.router_address_cs, amount=(2**256 - 1))
            cls._approve_erc20_for_spender(owner_acc_details=cls.bot_account_details, token_contract=cls.pool_token0_contract, spender_address=cls.router_address_cs, amount=(2**256 - 1))
            cls._approve_erc20_for_spender(owner_acc_details=cls.bot_account_details, token_contract=cls.pool_token1_contract, spender_address=cls.router_address_cs, amount=(2**256 - 1))

            current_dir = os.path.dirname(os.path.abspath(__file__))
            cls.cache_dir = os.path.join(current_dir, ".test_cache") 
            os.makedirs(cls.cache_dir, exist_ok=True)
            cls.price_cache_file = os.path.join(cls.cache_dir, f"price_cache_for_{config.POOL_ADDRESS.replace('0x', '')[:10]}.csv")
            logger.info(f"Price history cache will be stored/read from: {cls.price_cache_file}")
            logger.info(f"PriceProvider will aim to fetch initial history from block: {cls.default_start_block_for_tests} if cache is empty/stale.")

            cls.strategy_under_test = Strategy(
                sd_calculation_points=0, 
                sd_multiple=1.0,
                min_tick_width=cls.pool_tick_spacing * 20, 
                max_tick_width=None, 
                price_provider_start_block=cls.default_start_block_for_tests,
                price_provider_cache_path=cls.price_cache_file
            )
            logger.info("Default Strategy instance created; PriceProvider initial price fetch potentially used cache.")

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
                if token_addr_cs.lower() == config.TOKEN0_ADDRESS.lower() and whale_addr_cs.lower() == TOKEN0_WHALE_ADDRESS.lower(): # Assuming config.TOKEN0_ADDRESS is defined
                    logger.warning(f"Whale {whale_addr_cs} has insufficient {token_sym}. Attempting to mint more to whale...")
                    mint_tx_params = {'from': whale_addr_cs} 
                    # Ensure the token has a mint function callable by the whale (this is specific to test tokens)
                    # For generic ERC20s, this might fail. This part is highly dependent on the test token's ABI.
                    # If the token doesn't have `mint(address,uint256)`, this will error.
                    try:
                        token_contract.functions.mint(whale_addr_cs, amount_raw * 10).transact(mint_tx_params) 
                        whale_bal = token_contract.functions.balanceOf(whale_addr_cs).call()
                        if whale_bal < amount_raw:
                             raise ValueError(f"Whale {whale_addr_cs} still has insufficient {token_sym} ({whale_bal}) after mint attempt for requested {amount_raw}")
                    except Exception as e_mint:
                        logger.error(f"Could not mint to whale for {token_sym}: {e_mint}. This test might fail if whale balance is too low.")
                        raise ValueError(f"Whale {whale_addr_cs} has insufficient {token_sym} ({whale_bal}) for requested {amount_raw}, and minting failed or is not supported.") from e_mint

                else: # For other tokens or other whales, don't attempt to mint
                    raise ValueError(f"Whale {whale_addr_cs} has insufficient {token_sym} ({whale_bal}) for requested {amount_raw}")


            tx_params = {'from': whale_addr_cs, 'nonce': cls.w3.eth.get_transaction_count(whale_addr_cs)}
            latest_block = cls.w3.eth.get_block('latest')
            base_fee = latest_block.get('baseFeePerGas', cls.w3.to_wei(1, 'gwei'))
            tx_params['maxPriorityFeePerGas'] = cls.w3.to_wei(1.5, 'gwei')
            tx_params['maxFeePerGas'] = base_fee * 2 + tx_params['maxPriorityFeePerGas']
            transfer_fn = token_contract.functions.transfer(recipient_addr_cs, amount_raw)
            
            # Estimate gas for transfer
            try:
                estimated_gas_transfer = transfer_fn.estimate_gas(tx_params)
                tx_params['gas'] = int(estimated_gas_transfer * 1.2) # Add buffer
            except Exception as e_gas_est:
                logger.warning(f"Gas estimation for whale transfer failed: {e_gas_est}. Using fallback gas: 100000")
                tx_params['gas'] = 100000 # Fallback gas limit

            tx_hash = transfer_fn.transact(tx_params)
            receipt = cls.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            assert receipt.status == 1, f"ERC20 transfer from whale {whale_addr_cs} failed."
        finally:
            cls._anvil_rpc("anvil_stopImpersonatingAccount", [whale_addr_cs])
        recipient_bal = token_contract.functions.balanceOf(recipient_addr_cs).call()
        assert recipient_bal >= amount_raw, f"{token_sym} funding for {recipient_addr_cs} resulted in {recipient_bal}, needed at least {amount_raw}"
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


    def _create_strategy_instance_for_test(self, sd_points=0, sd_mult=1.0, min_width_ticks=None, 
                                           max_width_ticks=None, start_block=None, use_cache=True):
        """Helper to create a strategy instance with specific parameters for a test."""
        if min_width_ticks is None:
            min_width_ticks = self.pool_tick_spacing * 20 
        if start_block is None:
            start_block = self.default_start_block_for_tests

        cache_path_for_instance = self.price_cache_file if use_cache else None

        strategy = Strategy(
            sd_calculation_points=sd_points,
            sd_multiple=sd_mult,
            min_tick_width=min_width_ticks,
            max_tick_width=max_width_ticks,
            price_provider_start_block=start_block,
            price_provider_cache_path=cache_path_for_instance
        )
        if not strategy.price_provider.all_prices_chronological and use_cache:
             logger.info(f"Fetching initial prices for test-specific strategy instance (start_block: {start_block}, cache: {cache_path_for_instance})...")
             strategy.price_provider.fetch_initial_prices()
        elif not use_cache and not strategy.price_provider.all_prices_chronological:
            logger.info(f"Forcing initial price fetch for non-cached strategy instance (start_block: {start_block})...")
            strategy.price_provider.fetch_initial_prices()
        return strategy

    # _execute_pool_swap method is REMOVED from here

    # --- Test Cases Start Here ---
    def test_01_price_provider_setup_and_initial_full_fetch(self):
        logger.info("### Test 01: PriceProvider Initialization and Initial Full Price Fetch ###")
        pp = self.strategy_under_test.price_provider
        self.assertIsNotNone(pp, "PriceProvider instance is None within Strategy.")
        self.assertEqual(pp.cngn_reference_address, config.TOKEN0_ADDRESS.lower()) # Assuming config.TOKEN0_ADDRESS is defined
        self.assertTrue(pp.invert_price) 
        self.assertIsInstance(pp.all_prices_chronological, list, "Full price history is not a list.")
        logger.info(f"PriceProvider has {len(pp.all_prices_chronological)} initial prices from block ~{pp.start_block} (cache used if available).")
        self.assertIsNotNone(pp.last_processed_block, "PriceProvider.last_processed_block not set after initial fetch.")
        if pp.all_prices_chronological:
             self.assertGreaterEqual(pp.last_processed_block, pp.start_block, "Last processed block is before start block after initial fetch.")

    def test_02a_price_provider_fetch_new_prices_no_swaps(self):
        logger.info("### Test 02a: PriceProvider Fetch New Prices (No Intervening Swaps) ###")
        pp = self.strategy_under_test.price_provider
        initial_history_len = len(pp.all_prices_chronological)
        last_block_before_mine = pp.last_processed_block
        self._anvil_rpc("anvil_mine", [3,1])
        time.sleep(0.5) # Allow anvil to process blocks
        current_block_on_chain = self.w3.eth.block_number
        pp.fetch_new_prices() 
        self.assertEqual(len(pp.all_prices_chronological), initial_history_len)
        if last_block_before_mine is not None:
             self.assertGreaterEqual(pp.last_processed_block, last_block_before_mine)
        self.assertEqual(pp.last_processed_block, current_block_on_chain)


    def test_02b_price_provider_fetch_new_prices_with_swaps(self):
        logger.info("### Test 02b: PriceProvider Fetch New Prices (With Intervening Swaps) ###")
        pp = self.strategy_under_test.price_provider
        initial_history_len = len(pp.all_prices_chronological)
        amount_to_swap_raw = int(Decimal("10000") * (10**self.pool_token0_decimals)) 
        
        # Determine parameters for dex_utils.execute_router_swap
        token_in = self.pool_token0_addr
        token_out = self.pool_token1_addr
        sqrt_price_limit = config.MIN_SQRT_RATIO + 1 

        logger.info(f"Swapper executing ROUTER swap: Amount {amount_to_swap_raw} of token_in {token_in} to get {token_out}")
        dex_utils.execute_router_swap(
            w3=self.w3,
            router_contract=self.router_contract_for_tests,
            account_details=self.swapper_account_details,
            token_in_address=token_in,
            token_out_address=token_out,
            tick_spacing=self.pool_tick_spacing,
            recipient_address=self.swapper_account_details['address'],
            amount_in=amount_to_swap_raw,
            amount_out_minimum=0,
            sqrt_price_limit_x96=sqrt_price_limit
        )
        self._anvil_rpc("anvil_mine", [1,1]) # Mine block to include swap
        time.sleep(1) # Allow state to update

        pp.fetch_new_prices() 
        self.assertGreater(len(pp.all_prices_chronological), initial_history_len)

    def test_03_strategy_initial_position_check(self):
        logger.info("### Test 03: Strategy Initial Position Check (Should Be None) ###")
        s = self._create_strategy_instance_for_test(use_cache=True)
        s._get_existing_position() 
        self.assertIsNone(s.current_position_token_id)
        self.assertIsNone(s.current_position_details)

    def test_04a_sd_tick_calc_default_params(self):
        logger.info("### Test 04a: Strategy SD and Target Tick Calculation (Default Params) ###")
        s = self.strategy_under_test 
        pp = s.price_provider
        if len(pp.get_all_prices_for_sd()) < 3:
            amount_t0_swap = int(Decimal("50000") * (10**self.pool_token0_decimals))
            dex_utils.execute_router_swap(self.w3, self.router_contract_for_tests, self.swapper_account_details, self.pool_token0_addr, self.pool_token1_addr, self.pool_tick_spacing, self.swapper_account_details['address'], amount_t0_swap, 0, config.MIN_SQRT_RATIO + 1)
            self._anvil_rpc("anvil_mine", [1,1]); time.sleep(0.5)
            amount_t1_swap = int(Decimal("50000") * (10**self.pool_token1_decimals))
            dex_utils.execute_router_swap(self.w3, self.router_contract_for_tests, self.swapper_account_details, self.pool_token1_addr, self.pool_token0_addr, self.pool_tick_spacing, self.swapper_account_details['address'], amount_t1_swap, 0, config.MAX_SQRT_RATIO - 1)
            self._anvil_rpc("anvil_mine", [1,1]); time.sleep(0.5)
            pp.fetch_new_prices()
        self.assertGreaterEqual(len(pp.get_all_prices_for_sd()), 3, "Price history too short.")

        sd = s._calculate_price_sd(s.sd_calculation_points) 
        self.assertIsNotNone(sd, "SD calculation returned None.")
        self.assertGreater(sd, 0.0, "Standard deviation should be positive with varied prices.")
        current_tick, _, p_current_for_sd = s._get_current_tick_and_price_state()
        tick_l, tick_u = s._calculate_target_ticks(current_tick, p_current_for_sd)
        self.assertLess(tick_l, tick_u)
        self.assertEqual(tick_l % s.tick_spacing, 0)
        self.assertEqual(tick_u % s.tick_spacing, 0)
        # width = tick_u - tick_l # Variable width is defined but not used

    def test_04b_sd_tick_calc_recent_points(self):
        logger.info("### Test 04b: SD Tick Calculation (Recent Points) ###")
        s = self._create_strategy_instance_for_test(sd_points=5, use_cache=True)
        pp = s.price_provider
        for _ in range(max(0, 10 - len(pp.all_prices_chronological))): # Ensure enough history
            amount_t0_swap = int(Decimal("10000") * (10**self.pool_token0_decimals))
            dex_utils.execute_router_swap(self.w3, self.router_contract_for_tests, self.swapper_account_details, self.pool_token0_addr, self.pool_token1_addr, self.pool_tick_spacing, self.swapper_account_details['address'], amount_t0_swap, 0, config.MIN_SQRT_RATIO + 1)
            self._anvil_rpc("anvil_mine", [1,1]); time.sleep(0.1)
            amount_t1_swap = int(Decimal("10000") * (10**self.pool_token1_decimals))
            dex_utils.execute_router_swap(self.w3, self.router_contract_for_tests, self.swapper_account_details, self.pool_token1_addr, self.pool_token0_addr, self.pool_tick_spacing, self.swapper_account_details['address'], amount_t1_swap, 0, config.MAX_SQRT_RATIO - 1)
            self._anvil_rpc("anvil_mine", [1,1]); time.sleep(0.1)
        pp.fetch_new_prices()
        self.assertGreaterEqual(len(pp.all_prices_chronological), 5, "Not enough total history for recent points test.")

        with patch.object(pp, 'get_all_prices_for_sd') as mock_get_prices:
            mock_prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0] 
            mock_get_prices.return_value = mock_prices
            
            sd = s._calculate_price_sd(s.sd_calculation_points) 
            self.assertIsNotNone(sd)
            
            expected_sd_for_last_5 = pd.Series([102.0, 103.0, 104.0, 105.0, 106.0]).std(ddof=1) #ddof=0 for population, 1 for sample
            self.assertAlmostEqual(sd, expected_sd_for_last_5, places=5) 
            # self.assertAlmostEqual(sd, 1.581, places=3) # This might be too specific if pandas version or float precision varies slightly

        current_tick, _, p_current_for_sd = s._get_current_tick_and_price_state()
        tick_l, tick_u = s._calculate_target_ticks(current_tick, p_current_for_sd)
        self.assertLess(tick_l, tick_u)

    def test_04c_sd_tick_calc_sd_multiple(self):
        logger.info("### Test 04c: SD Tick Calculation (SD Multiple) ###")
        s_low_multiple = self._create_strategy_instance_for_test(sd_mult=0.5, use_cache=True)
        s_high_multiple = self._create_strategy_instance_for_test(sd_mult=2.0, use_cache=True)
        
        pp = s_low_multiple.price_provider 
        if len(pp.get_all_prices_for_sd()) < 10: 
            for i in range(5):
                amount_t0_swap = int(Decimal(str(10000+i*2000)) * (10**self.pool_token0_decimals))
                dex_utils.execute_router_swap(self.w3, self.router_contract_for_tests, self.swapper_account_details, self.pool_token0_addr, self.pool_token1_addr, self.pool_tick_spacing, self.swapper_account_details['address'], amount_t0_swap, 0, config.MIN_SQRT_RATIO + 1)
                self._anvil_rpc("anvil_mine", [1,1]); time.sleep(0.1)
                amount_t1_swap = int(Decimal(str(10000+i*2000)) * (10**self.pool_token1_decimals))
                dex_utils.execute_router_swap(self.w3, self.router_contract_for_tests, self.swapper_account_details, self.pool_token1_addr, self.pool_token0_addr, self.pool_tick_spacing, self.swapper_account_details['address'], amount_t1_swap, 0, config.MAX_SQRT_RATIO - 1)
                self._anvil_rpc("anvil_mine", [1,1]); time.sleep(0.1)
            pp.fetch_new_prices()
        self.assertGreaterEqual(len(pp.get_all_prices_for_sd()), 3)

        current_tick, _, p_current_for_sd = s_low_multiple._get_current_tick_and_price_state()
        tick_l_low, tick_u_low = s_low_multiple._calculate_target_ticks(current_tick, p_current_for_sd)
        width_low = tick_u_low - tick_l_low
        
        # Price might have moved, re-fetch for s_high_multiple
        current_tick_high, _, p_current_for_sd_high = s_high_multiple._get_current_tick_and_price_state() 
        tick_l_high, tick_u_high = s_high_multiple._calculate_target_ticks(current_tick_high, p_current_for_sd_high)
        width_high = tick_u_high - tick_l_high
        
        logger.info(f"Width (0.5x SD): {width_low}, Width (2.0x SD): {width_high}")
        # This assertion needs to be careful if min/max tick widths are hit
        if not (s_low_multiple.min_tick_width and s_low_multiple.min_tick_width >= width_low and s_low_multiple.min_tick_width >= width_high) and \
           not (s_high_multiple.max_tick_width and s_high_multiple.max_tick_width <= width_low and s_high_multiple.max_tick_width <= width_high) :
            self.assertGreater(width_high, width_low, "Higher SD multiple did not result in a wider tick range when not bounded by min/max width.")


    def test_05_first_position_creation(self):
        logger.info("### Test 05: Strategy First Position Creation ###")
        s = self._create_strategy_instance_for_test(
            min_width_ticks=self.pool_tick_spacing * 10, 
            max_width_ticks=self.pool_tick_spacing * 100,
            use_cache=True
        )
        existing_nft_ids = dex_utils.get_owner_token_ids(s.nft_manager_contract, s.wallet_address)
        for token_id in existing_nft_ids:
            details = dex_utils.get_position_details(s.nft_manager_contract, token_id)
            if details and Web3.to_checksum_address(details['token0']) == Web3.to_checksum_address(s.token0_address) and \
            Web3.to_checksum_address(details['token1']) == Web3.to_checksum_address(s.token1_address) and \
            details.get('tickSpacing') == s.tick_spacing:
                
                logger.warning(f"Test 05 Pre-run: Removing existing position {token_id} for bot wallet related to this pool.")
                s._remove_position(token_id) # _remove_position should handle all steps

        s._get_existing_position()
        self.assertIsNone(s.current_position_token_id, "A position already exists for bot wallet before the first creation test.")

        s._check_and_rebalance() 
        self.assertIsNotNone(s.current_position_token_id, "Strategy failed to create a new position token ID.")
        self.assertIsNotNone(s.current_position_details, "Strategy failed to update position details after creation.")
        if s.current_position_details: 
            self.assertGreater(s.current_position_details['liquidity'], 0, "Newly created position has zero liquidity.")
            logger.info(f"First position created: ID {s.current_position_token_id}, Liquidity {s.current_position_details['liquidity']}")
            current_tick_after_mint, _, _ = s._get_current_tick_and_price_state()
            pos_details = s.current_position_details
            self.assertTrue(pos_details['tickLower'] <= current_tick_after_mint < pos_details['tickUpper'],
                            f"Newly minted position range [{pos_details['tickLower']}, {pos_details['tickUpper']}] does not cover current_tick {current_tick_after_mint}.")

    def test_06_no_rebalance_when_price_in_range(self):
        logger.info("### Test 06: No Rebalance When Price is Within Range ###")
        s = self.strategy_under_test 
        s._get_existing_position()
        if not s.current_position_token_id: 
            logger.info("Test 06: No position found, creating one first.")
            s._check_and_rebalance()
            self.assertIsNotNone(s.current_position_token_id, "Failed to create pre-requisite position for Test 06.")
            self.assertIsNotNone(s.current_position_details, "Position details missing after creation for Test 06.")


        pos_id_before_check = s.current_position_token_id
        pos_liq_before_check = s.current_position_details['liquidity'] if s.current_position_details else 0 # Handle if details somehow None
        
        current_tick, _, _ = s._get_current_tick_and_price_state()
        pos_details = s.current_position_details
        self.assertTrue(pos_details['tickLower'] <= current_tick < pos_details['tickUpper'],
                        f"Test assumption failed: Current tick {current_tick} is outside existing range [{pos_details['tickLower']}, {pos_details['tickUpper']}] before 'no rebalance' check.")
        
        s._check_and_rebalance()
        self.assertEqual(s.current_position_token_id, pos_id_before_check, "Position ID changed when no rebalance was expected.")
        self.assertIsNotNone(s.current_position_details)
        if s.current_position_details:
             self.assertEqual(s.current_position_details['liquidity'], pos_liq_before_check)
        logger.info("No rebalance occurred, as expected.")

    def test_07_rebalance_after_price_moves_out(self):
        logger.info("### Test 07: Rebalance After Price Moves Out of Range ###")
        s = self.strategy_under_test
        s._get_existing_position()
        if s.current_position_token_id:
            s._remove_position(s.current_position_token_id)
        # ─── Step 0: create a fresh LP position with min_width=10 and fallback tick range ───
        s.min_tick_width = 10
        current_tick, _, _ = s._get_current_tick_and_price_state()
        tick_lower, tick_upper = s._fallback_ticks(current_tick)
        logger.info(f"Creating new LP position with min_width=10, tick range ({tick_lower}, {tick_upper})…")
        new_token_id = s._create_new_position(tick_lower, tick_upper)
        self.assertIsNotNone(new_token_id, "Failed to create new LP position for Test 07.")
        # after mint, Strategy._create_new_position will have populated `s.current_position_details`
        self.assertIsNotNone(s.current_position_details,
                            "Position details missing after LP creation for Test 07.")

        initial_pos_id = new_token_id
        initial_pos_range = (
            s.current_position_details['tickLower'],
            s.current_position_details['tickUpper'],
        )
        logger.info(f"Position before price move: ID {initial_pos_id}, Range {initial_pos_range}")

        # ─── Step 1: move price out of that range ───
        amount_to_swap_t1_raw = 16_000_000_000
        max_swap_attempts = 50
        price_moved_out = False
        current_tick_after_swap, _, _ = s._get_current_tick_and_price_state()

        # --- approve router ---
        amount_to_approve_router = 2**256 - 1
        logger.info(f"Approving cNGN ({self.pool_token0_addr}) for Router ({config.ROUTER_ADDRESS})…")
        self.assertTrue(
            dex_utils.approve_token_if_needed(
                self.w3,
                self.pool_token0_addr,
                self.swapper_account_details['address'],
                config.ROUTER_ADDRESS,
                amount_to_approve_router,
                self.swapper_account_details['pk'],
            ),
            "cNGN approval for Router failed."
        )

        logger.info(f"Approving USDC ({self.pool_token1_addr}) for Router ({config.ROUTER_ADDRESS})…")
        self.assertTrue(
            dex_utils.approve_token_if_needed(
                self.w3,
                self.pool_token1_addr,
                self.swapper_account_details['address'],
                config.ROUTER_ADDRESS,
                amount_to_approve_router,
                self.swapper_account_details['pk'],
            ),
            "USDC approval for Router failed."
        )

        for i in range(max_swap_attempts):
            prev_tick = current_tick_after_swap
            logger.info(
                f"Swap {i+1}/{max_swap_attempts} – "
                f"current tick {prev_tick}, target range {initial_pos_range}"
            )

            dex_utils.execute_router_swap(
                w3=self.w3,
                router_contract=self.router_contract_for_tests,
                account_details=self.swapper_account_details,
                token_in_address=self.pool_token0_addr,   # buy token1 to push cNGN price down 
                token_out_address=self.pool_token1_addr,
                tick_spacing=self.pool_tick_spacing,
                recipient_address=self.swapper_account_details['address'],
                amount_in=amount_to_swap_t1_raw,
                amount_out_minimum=0,
                sqrt_price_limit_x96=config.MIN_SQRT_RATIO + 1,
            )
            self._anvil_rpc("anvil_mine", [1, 1])
            time.sleep(3)

            s.price_provider.fetch_new_prices()
            current_tick_after_swap, _, p_oriented = s._get_current_tick_and_price_state()
            logger.info(
                f"Tick after swap {i+1}: {current_tick_after_swap} "
                f"(oriented price: {p_oriented:.6f}), prev: {prev_tick}"
            )

            if current_tick_after_swap != prev_tick:
                logger.info(f"Tick changed: {prev_tick} → {current_tick_after_swap}")
            else:
                logger.warning(
                    f"No tick change on swap {i+1}. "
                    "Check liquidity or increase swap size."
                )

            if not (initial_pos_range[0] <= current_tick_after_swap < initial_pos_range[1]):
                logger.info(
                    f"Price moved out of range: tick {current_tick_after_swap} "
                    f"is outside {initial_pos_range}."
                )
                price_moved_out = True
                break

            if i == max_swap_attempts - 1:
                logger.warning(
                    f"Reached max swaps; price still within {initial_pos_range}. "
                    f"Final tick: {current_tick_after_swap}"
                )

        self.assertTrue(
            price_moved_out,
            f"Failed to move price out of initial range {initial_pos_range} "
            f"after {max_swap_attempts} swaps; final tick {current_tick_after_swap}."
        )

        # ─── Step 2: trigger rebalance ───
        logger.info("Price moved out of range; running rebalance…")
        s._check_and_rebalance()
        s._get_existing_position()
        self.assertIsNotNone(s.current_position_token_id,
                            "Rebalance failed: no new position minted.")
        self.assertNotEqual(
            s.current_position_token_id,
            initial_pos_id,
            "Rebalance failed: position ID did not change."
        )
        self.assertIsNotNone(s.current_position_details,
                            "Rebalance failed: new position details missing.")
        if s.current_position_details:
            self.assertGreater(
                s.current_position_details['liquidity'],
                0,
                "Rebalanced position has zero liquidity."
            )
            new_range = (
                s.current_position_details['tickLower'],
                s.current_position_details['tickUpper'],
            )
            logger.info(
                f"Rebalanced to new position ID {s.current_position_token_id}, range {new_range}"
            )
            tick_post, _, _ = s._get_current_tick_and_price_state()
            self.assertTrue(
                new_range[0] <= tick_post < new_range[1],
                f"New range {new_range} does not cover tick {tick_post}."
            )

        # ─── Step 3: ensure old NFT was burned ───
        with self.assertRaises(Exception,
                            msg=f"Old position ID {initial_pos_id} should be burned."):
            s.nft_manager_contract.functions.ownerOf(initial_pos_id).call()
        logger.info(f"Verified old position {initial_pos_id} is no longer valid.")


    def test_08_position_removal(self):
        logger.info("### Test 08: Strategy Position Removal ###")
        s = self.strategy_under_test 
        s._get_existing_position()
        if not s.current_position_token_id:
            logger.info("Test 08: No position found, creating one for removal test...")
            s._check_and_rebalance() 
            self.assertIsNotNone(s.current_position_token_id, "Failed to create pre-requisite position for removal.")
        
        token_id_to_remove = s.current_position_token_id
        self.assertIsNotNone(token_id_to_remove, "Token ID to remove is None even after trying to ensure one exists.")

        removal_success = s._remove_position(token_id_to_remove)
        self.assertTrue(removal_success, "Strategy's _remove_position method failed.")
        
        # After removal, these should be reset in the strategy object
        self.assertIsNone(s.current_position_token_id, "current_position_token_id not None after removal")
        self.assertIsNone(s.current_position_details, "current_position_details not None after removal")
        
        with self.assertRaises(Exception): # Expecting an error when querying a burned/removed token
            s.nft_manager_contract.functions.ownerOf(token_id_to_remove).call()
        logger.info(f"Verified position ID {token_id_to_remove} is no longer valid after removal.")

    def test_09_fallback_tick_calculation_sd_invalid(self):
        logger.info("### Test 09: Fallback Tick Calculation (SD Invalid) ###")
        s = self._create_strategy_instance_for_test(min_width_ticks=self.pool_tick_spacing * 30, use_cache=True)
        pp = s.price_provider
        # original_full_history = list(pp.all_prices_chronological) # Not strictly needed for this patch
        
        with patch.object(pp, 'get_all_prices_for_sd', return_value=[1000.0, 1000.0]): # SD will be 0 or NaN
            current_tick, _, p_current_for_sd = s._get_current_tick_and_price_state()
            tick_l, tick_u = s._calculate_target_ticks(current_tick, p_current_for_sd) 

        self.assertLess(tick_l, tick_u)
        actual_width = tick_u - tick_l
        logger.info(f"Fallback ticks (due to invalid SD): [{tick_l}, {tick_u}], Width: {actual_width} (Target min: {s.min_tick_width})")
        
        # Fallback should respect min_tick_width and max_tick_width
        self.assertGreaterEqual(actual_width, s.min_tick_width, "Fallback width is less than min_tick_width.")
        if s.max_tick_width is not None:
            self.assertLessEqual(actual_width, s.max_tick_width, "Fallback width is greater than max_tick_width.")
        
        # Also ensure ticks are aligned with tick_spacing
        self.assertEqual(tick_l % s.tick_spacing, 0, "Fallback lower tick not aligned.")
        self.assertEqual(tick_u % s.tick_spacing, 0, "Fallback upper tick not aligned.")


if __name__ == "__main__":
    print("Starting Anvil Test Suite for strategy.py...")
    mainnet_rpc_from_config = config.RPC_URL if not original_rpc_url_for_strategy_testing else original_rpc_url_for_strategy_testing
    print(f"IMPORTANT: Ensure Anvil is running with: anvil --fork-url {mainnet_rpc_from_config}")
    
    # Standard unittest loader and runner
    loader = unittest.TestLoader()
    # Ensure tests run in defined order (01, 02a, 02b, etc.)
    loader.sortTestMethodsUsing = None # Reset to default or use a custom lambda if needed, e.g. lambda x, y: (x > y) - (x < y) for string comparison
    
    suite = loader.loadTestsFromTestCase(TestStrategyAnvil)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful(): 
        print("\n--- SOME TESTS FAILED ---")
    else: 
        print("\n--- ALL TESTS PASSED ---")