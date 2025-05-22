import time
import logging
from decimal import Decimal, getcontext
import math
import pandas as pd

import config
from blockchain_utils import get_web3_provider, get_wallet_credentials, load_contract
import dex_utils

# Configure logger for the strategy module
logger = logging.getLogger(__name__)
# Ensure basicConfig is called, e.g. if __main__ or by the importing module like test suite
if not logger.hasHandlers(): # Avoid adding multiple handlers if imported
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s][%(filename)s:%(lineno)s] %(message)s")


Q96_DEC = Decimal(2**96) # For sqrtPriceX96 conversion
CNGN_REFERENCE_ADDRESS = "0x46C85152bFe9f96829aA94755D9f915F9B10EF5F".lower() #


class PriceProvider:
    """
    Handles fetching and maintaining a complete history of pool prices 
    from a specified start block.
    """
    def __init__(self, w3, pool_contract, cngn_reference_address, swap_event_topic0, initial_start_block):
        self.logger = logging.getLogger(__name__ + ".PriceProvider")
        self.w3 = w3
        self.pool_contract = pool_contract
        # Stores all prices with metadata: list of {'price': float, 'block': int, 'logIndex': int}
        self.all_prices_chronological = [] 
        self.cngn_reference_address = cngn_reference_address.lower()
        self.swap_event_topic0 = swap_event_topic0
        
        self.start_block = initial_start_block
        self.last_processed_block = None # Will be set after initial fetch
        self._determine_price_inversion()

    def _determine_price_inversion(self):
        """Determines if the raw pool price (token0/token1) needs to be inverted."""
        try:
            pool_token0_addr = self.pool_contract.functions.token0().call().lower() #
            if pool_token0_addr == self.cngn_reference_address: #
                self.invert_price = True
            else:
                self.invert_price = False
            self.logger.info(f"Price inversion for price calculations set to {self.invert_price} (based on token0: {pool_token0_addr}, ref cNGN: {self.cngn_reference_address})")
        except Exception as e:
            self.logger.error(f"Error determining price inversion: {e}. Defaulting to False.")
            self.invert_price = False

    def _process_log_entry_to_price(self, log_entry):
        """Decodes a single log entry and calculates the price, handling inversion."""
        try:
            event_data = self.pool_contract.events.Swap().process_log(log_entry) #
            sqrt_price_x96 = Decimal(event_data['args']['sqrtPriceX96']) #
            price = (sqrt_price_x96 / Q96_DEC) ** 2 #
            
            if self.invert_price: #
                if price != Decimal(0):
                    price = Decimal(1) / price
                else:
                    self.logger.warning("Price is zero before inversion, cannot invert. Skipping log.")
                    return None 
            return float(price)
        except Exception as e:
            self.logger.warning(f"Error processing log entry to price: {e}. Log: {log_entry}")
            return None

    def _fetch_logs_in_batches(self, from_block, to_block, batch_size=10000): # for BATCH_SIZE
        """Helper to fetch logs in manageable batches for a given range."""
        all_logs = []
        self.logger.info(f"Batch fetching Swap logs from block {from_block} to {to_block} (batch size: {batch_size})")
        for current_batch_start in range(from_block, to_block + 1, batch_size):
            current_batch_end = min(current_batch_start + batch_size - 1, to_block)
            self.logger.debug(f"Fetching log batch: {current_batch_start} -> {current_batch_end}")
            try:
                logs_batch = self.w3.eth.get_logs({ #
                    "address": self.pool_contract.address,
                    "fromBlock": current_batch_start,
                    "toBlock": current_batch_end,
                    "topics": [self.swap_event_topic0] #
                })
                all_logs.extend(logs_batch)
            except Exception as e:
                self.logger.error(f"Error fetching log batch {current_batch_start}-{current_batch_end}: {e}", exc_info=True)
                # Depending on strategy, might stop or continue with partial data for the range.
                # For full history, it's better to halt or have a retry mechanism. For now, break.
                break 
        return all_logs

    def fetch_initial_prices(self):
        """
        Fetches all historical prices from self.start_block up to the current chain head.
        Populates self.all_prices_chronological and updates self.last_processed_block.
        """
        self.logger.info(f"Fetching ALL historical prices from start_block: {self.start_block}.")
        self.all_prices_chronological = [] 
        
        latest_block_on_chain = self.w3.eth.block_number
        
        if self.start_block > latest_block_on_chain:
            self.logger.warning(f"Start block {self.start_block} is ahead of current chain head {latest_block_on_chain}. No initial prices to fetch.")
            self.last_processed_block = latest_block_on_chain 
            return

        logs = self._fetch_logs_in_batches(self.start_block, latest_block_on_chain)
        
        temp_processed_prices = []
        for log_entry in logs:
            price = self._process_log_entry_to_price(log_entry)
            if price is not None:
                temp_processed_prices.append({
                    'price': price, 
                    'block': log_entry['blockNumber'], 
                    'logIndex': log_entry['logIndex']
                })
        
        temp_processed_prices.sort(key=lambda x: (x['block'], x['logIndex'])) #
        self.all_prices_chronological = temp_processed_prices
        
        if self.all_prices_chronological:
            # The last processed block is the block of the newest entry in our complete history,
            # or latest_block_on_chain if history is still empty (meaning no relevant logs found).
            self.last_processed_block = max(self.all_prices_chronological[-1]['block'], latest_block_on_chain if not logs else 0)
        else:
            self.last_processed_block = latest_block_on_chain
        
        self.logger.info(f"Initialized full price history with {len(self.all_prices_chronological)} prices. Last processed block for initial scan: {self.last_processed_block}")

    def fetch_new_prices(self):
        """Fetches new swap prices since `self.last_processed_block` and appends to history."""
        if self.last_processed_block is None:
            self.logger.warning("last_processed_block is None. Triggering initial full price fetch.")
            self.fetch_initial_prices() 
            return

        current_block_on_chain = self.w3.eth.block_number
        from_block_for_new = self.last_processed_block + 1
        
        if from_block_for_new > current_block_on_chain:
            self.logger.debug(f"No new blocks to process. Last processed: {self.last_processed_block}, Current on chain: {current_block_on_chain}")
            return

        self.logger.info(f"Fetching new prices from block {from_block_for_new} to {current_block_on_chain}")
        
        logs = self._fetch_logs_in_batches(from_block_for_new, current_block_on_chain)
            
        newly_added_prices_metadata = []
        for log_entry in logs:
            price = self._process_log_entry_to_price(log_entry)
            if price is not None:
                newly_added_prices_metadata.append({
                    'price': price,
                    'block': log_entry['blockNumber'],
                    'logIndex': log_entry['logIndex']
                })
        
        if newly_added_prices_metadata:
            # New logs are from a chronological fetch, so they should be in order already.
            # If _fetch_logs_in_batches has complex internal ordering, explicit sort here might be needed.
            # Assuming batches are appended chronologically, and logs within batches are chronological.
            newly_added_prices_metadata.sort(key=lambda x: (x['block'], x['logIndex'])) # Ensure strict order
            
            self.all_prices_chronological.extend(newly_added_prices_metadata)
            self.logger.info(f"Added {len(newly_added_prices_metadata)} new prices. Total price history size: {len(self.all_prices_chronological)}.")
        
        self.last_processed_block = current_block_on_chain

    def get_all_prices_for_sd(self):
        """Returns a list of just the price values from the stored chronological history."""
        return [item['price'] for item in self.all_prices_chronological]


class Strategy:
    def __init__(self, sd_min_width_ticks=100, price_provider_start_block=None): # Removed sd_price_history_size
        self.logger = logging.getLogger(__name__ + ".Strategy") # Specific logger for Strategy
        self.logger.info("Initializing strategy...")
        self.w3 = get_web3_provider()
        self.account, self.wallet_address = get_wallet_credentials(config.DUMMY_PRIVATE_KEY)

        self.pool_contract = load_contract(self.w3, config.POOL_ADDRESS, config.MINIMAL_POOL_ABI)
        self.nft_manager_contract = load_contract(self.w3, config.NONFUNGIBLE_POSITION_MANAGER_ADDRESS, config.MINIMAL_NONFUNGIBLE_POSITION_MANAGER_ABI)

        if not self.pool_contract or not self.nft_manager_contract:
            self.logger.error("Failed to load critical contracts. Exiting.")
            raise ConnectionError("Strategy: Critical contract loading failed.")

        self.pool_info = dex_utils.get_pool_info(self.pool_contract)
        if not self.pool_info:
            self.logger.error("Failed to get pool info. Exiting.")
            raise ValueError("Strategy: Could not retrieve pool information.")

        self.token0_address = self.pool_info['token0']
        self.token1_address = self.pool_info['token1']
        self.tick_spacing = self.pool_info['tickSpacing'] #
        self.pool_fee = self.pool_info['fee'] #

        self.token0_contract = load_contract(self.w3, self.token0_address, config.MINIMAL_ERC20_ABI)
        self.token1_contract = load_contract(self.w3, self.token1_address, config.MINIMAL_ERC20_ABI)
        if not self.token0_contract or not self.token1_contract:
            self.logger.error("Failed to load token contracts. Exiting.")
            raise ConnectionError("Strategy: Token contract loading failed.")

        swap_event_sig_text = "Swap(address,address,int256,int256,uint160,uint128,int24)" #
        self.swap_event_topic0 = self.w3.keccak(text=swap_event_sig_text).hex() #

        # Determine start_block for PriceProvider
        default_start_block = 28028682
        resolved_start_block = price_provider_start_block if price_provider_start_block is not None else default_start_block
        
        self.price_provider = PriceProvider(self.w3, 
                                            self.pool_contract, 
                                            CNGN_REFERENCE_ADDRESS, 
                                            self.swap_event_topic0,
                                            initial_start_block=resolved_start_block)
        
        self.logger.info(f"Performing initial full price history fetch via PriceProvider from block {resolved_start_block}...")
        self.price_provider.fetch_initial_prices()
        if not self.price_provider.all_prices_chronological:
            self.logger.warning("Initial full price history from PriceProvider is empty. SD calculation might be unavailable initially.")

        self.sd_min_width_ticks = sd_min_width_ticks

        self.logger.info(f"Strategy initialized for wallet: {self.wallet_address}")
        self.logger.info(f"Monitoring Pool: {config.POOL_ADDRESS}")
        self.logger.info(f"Token0: {self.token0_address}, Token1: {self.token1_address}")
        self.logger.info(f"TickSpacing: {self.tick_spacing}, Fee: {self.pool_fee}")
        self.logger.info(f"Min tick width for SD fallback: {sd_min_width_ticks}")


    def _calculate_price_sd(self):
        """Calculates standard deviation from PriceProvider's full historical data."""
        min_points_for_sd = 3 
        current_price_values = self.price_provider.get_all_prices_for_sd()
        
        if len(current_price_values) < min_points_for_sd:
            self.logger.warning(f"Not enough price data points ({len(current_price_values)}) from PriceProvider to calculate SD over all history. Need at least {min_points_for_sd}.")
            return None
        
        prices_series = pd.Series(current_price_values, dtype=float)
        sd = prices_series.std(ddof=1) 
        if pd.isna(sd): 
            self.logger.warning(f"Standard deviation over all history resulted in NaN (history size: {len(current_price_values)}). This can happen if all values are the same or too few unique values.")
            return None 
        self.logger.info(f"Calculated SD over all ({len(prices_series)}) prices: {sd:.8f} via PriceProvider.")
        return sd

    def _price_to_tick(self, price_decimal):
        """Converts price to tick. Assumes price = 1.0001^tick for raw price (token0/token1)."""
        if price_decimal <= Decimal(0):
            self.logger.error(f"Price must be positive to convert to tick. Got {price_decimal}")
            raise ValueError("Invalid price for tick conversion: must be positive.")
        return math.log(float(price_decimal)) / math.log(1.0001)

    def _tick_to_price(self, tick):
        """Converts tick to price. Price = 1.0001^tick (raw price: token0/token1)."""
        return Decimal(1.0001)**Decimal(tick)

    def _get_current_tick_and_price_state(self):
        """Fetches current tick, raw sqrtPriceX96, and price oriented for SD calculation."""
        pool_state = dex_utils.get_current_pool_state(self.pool_contract)
        if not pool_state:
            self.logger.error("Could not get current pool state.")
            return None, None, None
        current_tick = pool_state['tick']
        current_sqrt_price_x96 = Decimal(pool_state['sqrtPriceX96'])
        p_current_raw = (current_sqrt_price_x96 / Q96_DEC) ** 2
        if self.price_provider.invert_price:
            if p_current_raw == Decimal(0):
                self.logger.error("Current raw price (token0/token1) is 0, cannot invert for SD-oriented price.")
                return current_tick, current_sqrt_price_x96, None 
            p_current_for_sd_orientation = Decimal(1) / p_current_raw
        else:
            p_current_for_sd_orientation = p_current_raw
        self.logger.info(f"Current tick: {current_tick}, Price (oriented for SD, inv={self.price_provider.invert_price}): {p_current_for_sd_orientation:.8f}")
        return current_tick, current_sqrt_price_x96, p_current_for_sd_orientation


    def _calculate_target_ticks(self, current_tick, p_current_for_sd_orientation):
        """Calculates target ticks based on current price and its standard deviation."""
        # ... (implementation remains largely the same, ensures fetch_new_prices is called)
        if p_current_for_sd_orientation is None:
            self.logger.error("p_current_for_sd_orientation is None, cannot calculate SD-based ticks. Using fallback.")
            return self._fallback_ticks(current_tick)

        self.price_provider.fetch_new_prices() 
        price_sd_float = self._calculate_price_sd()

        if price_sd_float is None or price_sd_float <= 0:
            self.logger.warning(f"Invalid price_sd ({price_sd_float}). Falling back to fixed min_width_ticks ({self.sd_min_width_ticks}).")
            return self._fallback_ticks(current_tick)
        
        price_sd_decimal = Decimal(str(price_sd_float))
        target_price_low_oriented = p_current_for_sd_orientation - price_sd_decimal
        target_price_high_oriented = p_current_for_sd_orientation + price_sd_decimal
        self.logger.info(f"Target price range (oriented as per SD calc): Low={target_price_low_oriented:.8f}, High={target_price_high_oriented:.8f}")

        if target_price_low_oriented <= Decimal(0):
            self.logger.warning(f"Calculated target_price_low_oriented ({target_price_low_oriented}) is <= 0. Adjusting to p_current_for_sd_orientation / 2.")
            target_price_low_oriented = p_current_for_sd_orientation / Decimal(2)
            if target_price_low_oriented <= Decimal(0): 
                target_price_low_oriented = Decimal("1e-18")

        if self.price_provider.invert_price:
            if target_price_high_oriented == Decimal(0):
                 self.logger.error("Cannot derive raw_price_for_tick_lower due to zero target_price_high_oriented. Using fallback.")
                 return self._fallback_ticks(current_tick)
            raw_price_for_tick_lower = Decimal(1) / target_price_high_oriented
            if target_price_low_oriented == Decimal(0):
                self.logger.error("Cannot derive raw_price_for_tick_upper due to zero target_price_low_oriented. Using fallback.")
                return self._fallback_ticks(current_tick)
            raw_price_for_tick_upper = Decimal(1) / target_price_low_oriented
        else:
            raw_price_for_tick_lower = target_price_low_oriented
            raw_price_for_tick_upper = target_price_high_oriented

        try:
            if raw_price_for_tick_lower >= raw_price_for_tick_upper:
                self.logger.warning(f"Raw prices for tick conversion are invalid (Lower {raw_price_for_tick_lower} >= Upper {raw_price_for_tick_upper}). Likely due to very small SD. Using fallback.")
                return self._fallback_ticks(current_tick)
            tick_lower_raw = self._price_to_tick(raw_price_for_tick_lower)
            tick_upper_raw = self._price_to_tick(raw_price_for_tick_upper)
        except ValueError as e:
            self.logger.error(f"Error converting calculated price to tick: {e}. Using fallback.")
            return self._fallback_ticks(current_tick)

        tick_lower_aligned = math.floor(tick_lower_raw / self.tick_spacing) * self.tick_spacing
        tick_upper_aligned = math.ceil(tick_upper_raw / self.tick_spacing) * self.tick_spacing
        
        if tick_lower_aligned >= tick_upper_aligned:
            self.logger.warning(f"Aligned ticks overlap or inverted (Lower={tick_lower_aligned}, Upper={tick_upper_aligned}). Adjusting using fallback around current tick.")
            return self._fallback_ticks(current_tick)
        
        if (tick_upper_aligned - tick_lower_aligned) < self.sd_min_width_ticks:
            self.logger.info(f"SD-based tick range ({tick_upper_aligned - tick_lower_aligned}) is narrower than min_width_ticks ({self.sd_min_width_ticks}). Expanding range.")
            tick_center_of_narrow_range = (tick_lower_aligned + tick_upper_aligned) / 2.0
            half_min_width = self.sd_min_width_ticks / 2.0
            expanded_tick_lower_raw = tick_center_of_narrow_range - half_min_width
            expanded_tick_upper_raw = tick_center_of_narrow_range + half_min_width
            tick_lower_aligned = math.floor(expanded_tick_lower_raw / self.tick_spacing) * self.tick_spacing
            tick_upper_aligned = math.ceil(expanded_tick_upper_raw / self.tick_spacing) * self.tick_spacing
            if tick_lower_aligned >= tick_upper_aligned:
                tick_upper_aligned = tick_lower_aligned + self.tick_spacing

        self.logger.info(f"SD-based target ticks: AlignedLower={int(tick_lower_aligned)}, AlignedUpper={int(tick_upper_aligned)}")
        return int(tick_lower_aligned), int(tick_upper_aligned)

    def _fallback_ticks(self, current_tick):
        """Provides fallback tick calculation using sd_min_width_ticks centered around current_tick."""
        self.logger.info(f"Using fallback tick calculation: {self.sd_min_width_ticks} wide around current_tick {current_tick}.")
        aligned_current_mid_tick = round(current_tick / self.tick_spacing) * self.tick_spacing
        half_min_width = self.sd_min_width_ticks / 2.0
        raw_lower = aligned_current_mid_tick - half_min_width
        raw_upper = aligned_current_mid_tick + half_min_width
        tick_lower = math.floor(raw_lower / self.tick_spacing) * self.tick_spacing
        tick_upper = math.ceil(raw_upper / self.tick_spacing) * self.tick_spacing
        if tick_lower >= tick_upper: 
            self.logger.warning(f"Fallback ticks still invalid (L:{tick_lower} U:{tick_upper}). Ensuring min separation.")
            tick_upper = tick_lower + self.tick_spacing
        self.logger.info(f"Fallback target ticks: Lower={int(tick_lower)}, Upper={int(tick_upper)}")
        return int(tick_lower), int(tick_upper)

    def _get_existing_position(self):
        """Refreshes self.current_position_token_id and self.current_position_details."""
        token_ids = dex_utils.get_owner_token_ids(self.nft_manager_contract, self.wallet_address)
        self.current_position_token_id = None 
        self.current_position_details = None
        if not token_ids:
            self.logger.info("No existing NFT positions found for this wallet.")
            return None
        for token_id in token_ids:
            details = dex_utils.get_position_details(self.nft_manager_contract, token_id)
            if details:
                position_matches_pool = (
                    details['token0'].lower() == self.token0_address.lower() and
                    details['token1'].lower() == self.token1_address.lower() and
                    details['tickSpacing'] == self.tick_spacing
                )
                if position_matches_pool:
                    self.logger.info(f"Found existing position for this pool: TokenID={token_id}, Liquidity={details['liquidity']}")
                    self.current_position_token_id = token_id
                    self.current_position_details = details
                    return token_id 
            else:
                self.logger.warning(f"Could not get details for TokenID {token_id}. Skipping.")
        self.logger.info("No existing NFT positions found specifically for the configured pool.")
        return None

    def _create_new_position(self, tick_lower, tick_upper):
        """Creates a new liquidity position."""
        self.logger.info(f"Attempting to create new position: Lower={tick_lower}, Upper={tick_upper}")
        balance0 = self.token0_contract.functions.balanceOf(self.wallet_address).call()
        balance1 = self.token1_contract.functions.balanceOf(self.wallet_address).call()
        self.logger.info(f"Available balances for minting: Token0 ({self.token0_address}) = {balance0}, Token1 ({self.token1_address}) = {balance1}")
        if balance0 == 0 and balance1 == 0:
            self.logger.error("Cannot mint new position: Zero balance for both tokens.")
            return None
        amount0_min = 0
        amount1_min = 0
        mint_call_params = (
            self.token0_address, self.token1_address, self.tick_spacing, 
            tick_lower, tick_upper, balance0, balance1,
            amount0_min, amount1_min, self.wallet_address,
            int(time.time()) + 600, 0 
        )
        self.logger.info(f"Calling dex_utils.mint_new_position with params: {mint_call_params}")
        receipt = dex_utils.mint_new_position(
            self.w3, self.nft_manager_contract, self.pool_contract,
            mint_call_params, self.account, self.wallet_address
        )
        if receipt and receipt.status == 1:
            self.logger.info(f"Successfully minted new position. TxHash: {receipt.transactionHash.hex()}")
            time.sleep(3) 
            self._get_existing_position() 
            return self.current_position_token_id
        else:
            self.logger.error(f"Failed to mint new position. Receipt: {receipt}")
            return None


    def _remove_position(self, token_id):
        """Removes an existing liquidity position."""
        self.logger.info(f"Attempting to remove position: TokenID={token_id}")
        position_details = dex_utils.get_position_details(self.nft_manager_contract, token_id)
        if not position_details:
            self.logger.error(f"Could not get details for position {token_id} to remove it.")
            return False
        liquidity_to_remove = position_details['liquidity']
        if liquidity_to_remove == 0:
            self.logger.info(f"Position {token_id} already has zero liquidity.")
        else:
            self.logger.info(f"Decreasing liquidity for position {token_id} by {liquidity_to_remove}")
            decrease_success = dex_utils.decrease_liquidity_and_collect_position(
                self.w3, self.nft_manager_contract, token_id,
                liquidity_to_remove, 0, 0, self.wallet_address,       
                self.account, self.wallet_address
            )
            if not decrease_success:
                self.logger.error(f"Failed to decrease liquidity and collect for position {token_id}.")
                return False
            self.logger.info(f"Successfully decreased liquidity and collected for position {token_id}.")
            time.sleep(2) 
            updated_details = dex_utils.get_position_details(self.nft_manager_contract, token_id)
            if updated_details and updated_details['liquidity'] != 0:
                self.logger.error(f"Liquidity for position {token_id} is still {updated_details['liquidity']} after decrease. Cannot burn safely.")
                return False
            elif not updated_details and liquidity_to_remove > 0 : 
                 self.logger.warning(f"Could not re-verify details for position {token_id} after decrease.")
        self.logger.info(f"Burning NFT for position {token_id}")
        burn_success = dex_utils.burn_nft_position(
            self.w3, self.nft_manager_contract, token_id,
            self.account, self.wallet_address
        )
        if burn_success:
            self.logger.info(f"Successfully burned NFT {token_id}.")
            if self.current_position_token_id == token_id:
                self.current_position_token_id = None
                self.current_position_details = None
            return True
        else:
            self.logger.error(f"Failed to burn NFT {token_id}.")
            return False

    def _check_and_rebalance(self):
        """Core logic: Fetches current state and rebalances the position if needed."""
        self.logger.info("--- Running Rebalance Check ---")
        current_tick, _, p_current_for_sd = self._get_current_tick_and_price_state()
        if current_tick is None or p_current_for_sd is None:
            self.logger.error("Cannot proceed with rebalance check: failed to get current tick/price state.")
            self.price_provider.fetch_new_prices() # Attempt to update price history
            return

        self._get_existing_position() 

        if not self.current_position_token_id:
            self.logger.info("No existing managed position found. Creating a new one.")
            target_tick_lower, target_tick_upper = self._calculate_target_ticks(current_tick, p_current_for_sd)
            self._create_new_position(target_tick_lower, target_tick_upper)
        else: 
            pos_details = self.current_position_details
            pos_tick_lower = pos_details['tickLower']
            pos_tick_upper = pos_details['tickUpper']
            self.logger.info(f"Existing position TokenID {self.current_position_token_id}: Range=[{pos_tick_lower}, {pos_tick_upper}], Liquidity={pos_details['liquidity']}")

            if not (pos_tick_lower <= current_tick < pos_tick_upper):
                self.logger.info(f"REBALANCE TRIGGERED: Current tick {current_tick} is outside position range [{pos_tick_lower}, {pos_tick_upper}].")
                self.logger.info("Step 1: Removing old position...")
                remove_success = self._remove_position(self.current_position_token_id)
                if not remove_success:
                    self.logger.error("Failed to remove old position during rebalance. Aborting rebalance cycle.")
                    self.price_provider.fetch_new_prices()
                    return 
                self.logger.info("Old position successfully removed.")
                self.logger.info("Step 2: Creating new position at new target ticks...")
                current_tick_after_removal, _, p_current_for_sd_after_removal = self._get_current_tick_and_price_state()
                if current_tick_after_removal is None or p_current_for_sd_after_removal is None:
                     self.logger.error("Failed to get current price after removal. Cannot create new position.")
                     self.price_provider.fetch_new_prices()
                     return
                target_tick_lower, target_tick_upper = self._calculate_target_ticks(current_tick_after_removal, p_current_for_sd_after_removal)
                self._create_new_position(target_tick_lower, target_tick_upper)
            else: 
                self.logger.info(f"No rebalance needed. Current tick {current_tick} is within position range [{pos_tick_lower}, {pos_tick_upper}].")
                self.price_provider.fetch_new_prices()


    def run(self):
        """Main operational loop for the strategy bot."""
        self.logger.info(f"🚀 Starting Strategy Bot. Poll interval: {config.POLL_INTERVAL_SECONDS} seconds.")
        self.logger.info("Strategy: Initial position check and price history fetch were done during __init__.")
        self.logger.info(f"Strategy: Initial full price history size from PriceProvider: {len(self.price_provider.all_prices_chronological)}.")
        
        while True:
            try:
                self._check_and_rebalance()
            except Exception as e:
                self.logger.error(f"An error occurred in the main strategy loop: {e}", exc_info=True)
                try:
                    self.price_provider.fetch_new_prices()
                except Exception as fetch_e:
                    self.logger.error(f"Failed to update price history after main loop error: {fetch_e}")
            self.logger.info(f"Next check in {config.POLL_INTERVAL_SECONDS} seconds... 😴")
            time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == '__main__':
    logger.info("Strategy.py executed directly. Initializing and running strategy...")
    try:
        # Default start block from data.py if not overridden by config or constructor arg
        start_block_for_strategy = getattr(config, 'PRICE_PROVIDER_START_BLOCK', 28028682) # for default value
        
        strategy_bot = Strategy(
            sd_min_width_ticks=200, # Example: 20 * 10 (tick_spacing)
            price_provider_start_block=start_block_for_strategy 
        ) 
        strategy_bot.run()
    except ConnectionError as ce:
        logger.critical(f"Failed to initialize strategy due to connection error: {ce}")
    except ValueError as ve:
        logger.critical(f"Failed to initialize strategy due to value error: {ve}")
    except Exception as e:
        logger.critical(f"Unhandled exception during strategy initialization or run: {e}", exc_info=True)