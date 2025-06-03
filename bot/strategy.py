import time
import logging
from decimal import Decimal
import math
import pandas as pd
import os

import bot.config as config
from bot.utils.blockchain_utils import get_web3_provider, get_wallet_credentials, load_contract
import bot.utils.dex_utils as dex_utils
from bot.price_provider import PriceProvider

# Configure logger for the strategy module
logger = logging.getLogger(__name__)
# Ensure basicConfig is called, e.g. if __main__ or by the importing module like test suite
if not logger.hasHandlers(): # Avoid adding multiple handlers if imported
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s][%(filename)s:%(lineno)s] %(message)s")

class Strategy:
    def __init__(self, sd_calculation_points=0, sd_multiple=1.0, min_tick_width=100, 
                 max_tick_width=None, price_provider_start_block=None, price_provider_cache_path=None): # Added cache path
        self.logger = logging.getLogger(__name__ + ".Strategy") 
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
        self.tick_spacing = self.pool_info['tickSpacing'] 
        self.pool_fee = self.pool_info['fee'] 

        self.token0_contract = load_contract(self.w3, self.token0_address, config.MINIMAL_ERC20_ABI)
        self.token1_contract = load_contract(self.w3, self.token1_address, config.MINIMAL_ERC20_ABI)
        if not self.token0_contract or not self.token1_contract:
            self.logger.error("Failed to load token contracts. Exiting.")
            raise ConnectionError("Strategy: Token contract loading failed.")

        swap_event_sig_text = "Swap(address,address,int256,int256,uint160,uint128,int24)" 
        self.swap_event_topic0 = self.w3.keccak(text=swap_event_sig_text).hex() 

        default_start_block = 28028682 
        resolved_start_block = price_provider_start_block if price_provider_start_block is not None else default_start_block

        self.price_provider = PriceProvider(self.w3,
                                            self.pool_contract,
                                            config.TOKEN0_ADDRESS, #cNGN
                                            self.swap_event_topic0,
                                            initial_start_block=resolved_start_block,
                                            cache_file_path=price_provider_cache_path) # Pass cache path

        self.logger.info(f"Performing initial/cached price history fetch via PriceProvider from block {resolved_start_block} (cache: {price_provider_cache_path})...")
        self.price_provider.fetch_initial_prices() 
        if not self.price_provider.all_prices_chronological:
            self.logger.warning("Initial full price history from PriceProvider is empty. SD calculation might be unavailable initially.")

        self.sd_calculation_points = sd_calculation_points
        self.sd_multiple = Decimal(str(sd_multiple))
        self.min_tick_width = min_tick_width
        self.max_tick_width = max_tick_width
        self.current_position_token_id =  None
        self.current_position_details =  None

        self.logger.info(f"Strategy initialized for wallet: {self.wallet_address}")
        self.logger.info(f"Monitoring Pool: {config.POOL_ADDRESS}")
        self.logger.info(f"Token0: {self.token0_address}, Token1: {self.token1_address}")
        self.logger.info(f"TickSpacing: {self.tick_spacing}, Fee: {self.pool_fee}")
        self.logger.info(f"SD calculation using last {self.sd_calculation_points if self.sd_calculation_points > 0 else 'all'} price points.")
        self.logger.info(f"SD multiple for range calculation: {self.sd_multiple}")
        self.logger.info(f"Min tick width constraint: {self.min_tick_width}")
        self.logger.info(f"Max tick width constraint: {self.max_tick_width if self.max_tick_width is not None else 'None'}")

    def _calculate_price_sd(self, num_recent_points=0):
        """
        Calculates standard deviation from PriceProvider's historical data.
        Uses the last `num_recent_points` if specified and available, otherwise all history.
        """
        min_points_for_sd = 3
        price_history = self.price_provider.get_all_prices_for_sd() #

        if not price_history:
            self.logger.warning("Price history is empty. Cannot calculate SD.")
            return None

        if num_recent_points > 0 and len(price_history) > num_recent_points:
            relevant_prices = price_history[-num_recent_points:]
            history_desc = f"last {len(relevant_prices)}"
        else:
            relevant_prices = price_history
            history_desc = f"all {len(relevant_prices)}"

        if len(relevant_prices) < min_points_for_sd:
            self.logger.warning(f"Not enough price data points ({len(relevant_prices)} from {history_desc} history) to calculate SD. Need at least {min_points_for_sd}.")
            return None

        prices_series = pd.Series(relevant_prices, dtype=float)
        sd = prices_series.std(ddof=1)
        if pd.isna(sd) or sd == 0: # Also check for sd == 0 if it causes issues
            self.logger.warning(f"Standard deviation over {history_desc} prices resulted in NaN or zero (history size: {len(prices_series)}, SD: {sd}). This can happen with too few unique values or constant prices.")
            return None # Treat zero SD as potentially problematic for range setting
        self.logger.info(f"Calculated SD over {history_desc} prices: {sd:.8f}")
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
        p_current_raw = (current_sqrt_price_x96 / config.Q96_DEC) ** 2
        if self.price_provider.invert_price: #
            if p_current_raw == Decimal(0):
                self.logger.error("Current raw price (token0/token1) is 0, cannot invert for SD-oriented price.")
                return current_tick, current_sqrt_price_x96, None
            p_current_for_sd_orientation = Decimal(1) / p_current_raw
        else:
            p_current_for_sd_orientation = p_current_raw
        self.logger.info(f"Current tick: {current_tick}, Price (oriented for SD, inv={self.price_provider.invert_price}): {p_current_for_sd_orientation:.8f}")
        return current_tick, current_sqrt_price_x96, p_current_for_sd_orientation


    def _calculate_target_ticks(self, current_tick, p_current_for_sd_orientation):
        """Calculates target ticks based on current price, its SD, SD multiple, and min/max width constraints."""
        if p_current_for_sd_orientation is None:
            self.logger.error("p_current_for_sd_orientation is None, cannot calculate SD-based ticks. Using fallback.")
            return self._fallback_ticks(current_tick)

        self.price_provider.fetch_new_prices() #
        price_sd_float = self._calculate_price_sd(self.sd_calculation_points)

        if price_sd_float is None or price_sd_float <= 0: # Ensure SD is positive
            self.logger.warning(f"Invalid price_sd ({price_sd_float}). Falling back to fixed min_width_ticks around current tick.")
            return self._fallback_ticks(current_tick)

        price_sd_decimal = Decimal(str(price_sd_float))
        price_offset = self.sd_multiple * price_sd_decimal

        target_price_low_oriented = p_current_for_sd_orientation - price_offset
        target_price_high_oriented = p_current_for_sd_orientation + price_offset
        self.logger.info(f"Target price range (oriented as per SD calc, multiplier {self.sd_multiple}): Low={target_price_low_oriented:.8f}, High={target_price_high_oriented:.8f}")

        if target_price_low_oriented <= Decimal(0):
            self.logger.warning(f"Calculated target_price_low_oriented ({target_price_low_oriented}) is <= 0. Adjusting. Using fallback.")
            # Using fallback is safer than arbitrary adjustments here if the SD multiple pushes price too low.
            return self._fallback_ticks(current_tick)


        if self.price_provider.invert_price: #
            # When inverted, high oriented price corresponds to low raw price (for lower tick)
            if target_price_high_oriented == Decimal(0):
                 self.logger.error("Cannot derive raw_price_for_tick_lower due to zero target_price_high_oriented (inverted). Using fallback.")
                 return self._fallback_ticks(current_tick)
            raw_price_for_tick_lower = Decimal(1) / target_price_high_oriented
            # low oriented price corresponds to high raw price (for upper tick)
            if target_price_low_oriented == Decimal(0):
                self.logger.error("Cannot derive raw_price_for_tick_upper due to zero target_price_low_oriented (inverted). Using fallback.")
                return self._fallback_ticks(current_tick)
            raw_price_for_tick_upper = Decimal(1) / target_price_low_oriented
        else:
            raw_price_for_tick_lower = target_price_low_oriented
            raw_price_for_tick_upper = target_price_high_oriented
            self.logger.info(f"Price for Lower and Upper Target Ticks: {raw_price_for_tick_lower} and {raw_price_for_tick_upper}")

        try:
            if raw_price_for_tick_lower <= Decimal(0) or raw_price_for_tick_upper <= Decimal(0):
                 self.logger.warning(f"Raw prices for tick conversion would be non-positive (L:{raw_price_for_tick_lower}, U:{raw_price_for_tick_upper}). Using fallback.")
                 return self._fallback_ticks(current_tick)
            if raw_price_for_tick_lower >= raw_price_for_tick_upper:
                self.logger.warning(f"Raw prices for tick conversion are invalid (Lower {raw_price_for_tick_lower} >= Upper {raw_price_for_tick_upper}). Likely due to very small SD or large multiple. Using fallback.")
                return self._fallback_ticks(current_tick)
            tick_lower_raw = self._price_to_tick(raw_price_for_tick_lower)
            tick_upper_raw = self._price_to_tick(raw_price_for_tick_upper)
        except ValueError as e:
            self.logger.error(f"Error converting calculated price to tick: {e}. Using fallback.")
            return self._fallback_ticks(current_tick)

        # Initial alignment
        target_tick_lower = math.floor(tick_lower_raw / self.tick_spacing) * self.tick_spacing
        target_tick_upper = math.ceil(tick_upper_raw / self.tick_spacing) * self.tick_spacing

        if target_tick_lower >= target_tick_upper:
            self.logger.warning(f"Initial aligned SD-based ticks overlap or inverted (Lower={target_tick_lower}, Upper={target_tick_upper}). Using fallback.")
            return self._fallback_ticks(current_tick)

        current_width = target_tick_upper - target_tick_lower

        # Min Width Adjustment
        if current_width < self.min_tick_width:
            self.logger.info(f"SD-based tick range width ({current_width}) is narrower than min_tick_width ({self.min_tick_width}). Expanding range.")
            center_tick = (target_tick_lower + target_tick_upper) / 2.0
            half_min_width = self.min_tick_width / 2.0
            target_tick_lower = math.floor((center_tick - half_min_width) / self.tick_spacing) * self.tick_spacing
            target_tick_upper = math.ceil((center_tick + half_min_width) / self.tick_spacing) * self.tick_spacing
            if target_tick_lower >= target_tick_upper: # Ensure valid range after expansion
                target_tick_upper = target_tick_lower + self.tick_spacing
            current_width = target_tick_upper - target_tick_lower # Update current_width

        # Max Width Adjustment
        if self.max_tick_width is not None and current_width > self.max_tick_width:
            self.logger.info(f"Calculated tick range width ({current_width}) is wider than max_tick_width ({self.max_tick_width}). Capping range.")
            center_tick = (target_tick_lower + target_tick_upper) / 2.0 # Center of the (potentially min-width-adjusted) range
            half_max_width = self.max_tick_width / 2.0
            target_tick_lower = math.floor((center_tick - half_max_width) / self.tick_spacing) * self.tick_spacing
            target_tick_upper = math.ceil((center_tick + half_max_width) / self.tick_spacing) * self.tick_spacing
            if target_tick_lower >= target_tick_upper: # Ensure valid range after capping
                target_tick_upper = target_tick_lower + self.tick_spacing # Should ideally not happen if max_width is reasonable


        self.logger.info(f"Final target ticks after constraints: AlignedLower={int(target_tick_lower)}, AlignedUpper={int(target_tick_upper)}, Width={int(target_tick_upper - target_tick_lower)}")
        return int(target_tick_lower), int(target_tick_upper)

    def _fallback_ticks(self, current_tick):
        """Provides fallback tick calculation using min_tick_width centered around current_tick."""
        self.logger.info(f"Using fallback tick calculation: {self.min_tick_width} wide around current_tick {current_tick}.")
        # Align current_tick to the nearest tick that is a multiple of tickSpacing
        # This helps in centering the fallback range more predictably.
        aligned_current_mid_tick = round(current_tick / self.tick_spacing) * self.tick_spacing

        half_width = self.min_tick_width / 2.0
        
        # Calculate raw lower and upper ticks based on the aligned current mid tick and half_width
        raw_lower = aligned_current_mid_tick - half_width
        raw_upper = aligned_current_mid_tick + half_width
        
        # Align these raw ticks to the tick_spacing
        tick_lower = math.floor(raw_lower / self.tick_spacing) * self.tick_spacing
        tick_upper = math.ceil(raw_upper / self.tick_spacing) * self.tick_spacing

        if tick_lower >= tick_upper:
            self.logger.warning(f"Fallback ticks initially invalid (L:{tick_lower} U:{tick_upper}). Ensuring min separation based on tick_spacing.")
            # Ensure a minimum separation of at least one tick_spacing
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
                # Check if the position matches the pool this strategy is configured for
                # Note: Pool contract's fee might be different from NFT manager's position fee if it's a V3 fee.
                # Here, we use tickSpacing as a proxy along with token addresses.
                position_matches_pool = (
                    details['token0'].lower() == self.token0_address.lower() and
                    details['token1'].lower() == self.token1_address.lower() and
                    details['tickSpacing'] == self.tick_spacing # Compare with pool's tickSpacing
                )
                if position_matches_pool:
                    self.logger.info(f"Found existing position for this pool: TokenID={token_id}, Liquidity={details['liquidity']}")
                    self.current_position_token_id = token_id
                    self.current_position_details = details
                    return token_id # Return the first matching position
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
        # Aeordrome requires both token0 AND token1 if price is strictly inside [tick_lower, tick_upper].
        # So if *either* side is zero, we cannot mint inside that range; bail out immediately.
        if balance0 == 0 or balance1 == 0:
            self.logger.error(
                "Cannot mint new position: Both token0 and token1 are required to be nonzero "
                f"when price is inside the chosen ticks. (balance0={balance0}, balance1={balance1})"
            )
            return None
        
        if self.current_position_token_id or self.current_position_details:
            logger.error(f"Cannot call _create_new_position when already have LP postion open with id and details: {self.current_position_token_id} and {self.current_position_details}")
    
        # The actual amounts used will depend on the price range and current pool price.
        # We pass the full balances as desired amounts and let the contract figure it out.
        # Min amounts are set to 0 for simplicity, meaning we accept any amount of the other token.
        amount0_desired = balance0
        amount1_desired = balance1
        amount0_min = 0 # Slippage control: min amount of token0 to provide
        amount1_min = 0 # Slippage control: min amount of token1 to provide
        
        # Ensure ticks are integers
        tick_lower_int = int(tick_lower)
        tick_upper_int = int(tick_upper)

        # Deadline for the transaction
        deadline = int(time.time()) + 600  # 10 minutes from now

        # Params for mint function:
        mint_call_params = (
            self.token0_address, self.token1_address, self.tick_spacing, 
            tick_lower_int, tick_upper_int, amount0_desired, amount1_desired,
            amount0_min, amount1_min, self.wallet_address,
            deadline, 0
        )

        self.logger.info(f"Calling dex_utils.mint_new_position with params: {mint_call_params}")
        receipt = dex_utils.mint_new_position(
            self.w3, self.nft_manager_contract, self.pool_contract, # pool_contract might be used internally by mint_new_position for some checks or calls
            mint_call_params, self.account, self.wallet_address
        )
        if receipt and receipt.status == 1:
            self.logger.info(f"Successfully minted new position. TxHash: {receipt.transactionHash.hex()}")
            time.sleep(3) # Brief pause for blockchain state to settle before re-querying
            self.current_position_token_id = self._get_existing_position() # Refresh current position state
            details = dex_utils.get_position_details(self.nft_manager_contract, self.current_position_token_id)
            self.current_position_details = details
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
            self.logger.info(f"Position {token_id} already has zero liquidity. Proceeding to burn if necessary.")
            # If liquidity is zero, we might just need to burn the NFT.
            # However, a "collect" might still be needed for accrued fees even with zero liquidity.
            # The decrease_liquidity_and_collect_position handles both.
        
        self.logger.info(f"Attempting to decrease liquidity (by {liquidity_to_remove}) and collect for position {token_id}")
        # Set amount0Min and amount1Min to 0 to collect whatever is available.
        decrease_collect_success = dex_utils.decrease_liquidity_and_collect_position(
            self.w3, self.nft_manager_contract, token_id,
            liquidity_to_remove, 0, 0, # liquidity, amount0Min, amount1Min
            self.wallet_address, # recipient
            self.account, self.wallet_address
        )

        if not decrease_collect_success:
            self.logger.error(f"Failed to decrease liquidity and/or collect for position {token_id}.")
            # Don't try to burn if decrease/collect failed, as there might be an on-chain reason.
            return False
        
        self.logger.info(f"Successfully decreased liquidity (if any) and collected for position {token_id}.")
        time.sleep(2)

        # Verify liquidity is zero before burning (optional, as burn might succeed anyway but good practice)
        updated_details = dex_utils.get_position_details(self.nft_manager_contract, token_id)
        if updated_details and updated_details['liquidity'] != 0:
            self.logger.error(f"Liquidity for position {token_id} is still {updated_details['liquidity']} after decrease/collect. Cannot burn safely.")
            return False
        elif not updated_details and liquidity_to_remove > 0 : # If details became unavailable and we expected liquidity
             self.logger.warning(f"Could not re-verify details for position {token_id} after decrease/collect. Proceeding with burn cautiously.")
        
        self.logger.info(f"Burning NFT for position {token_id}")
        burn_success = dex_utils.burn_nft_position(
            self.w3, self.nft_manager_contract, token_id,
            self.account, self.wallet_address
        )
        
        if burn_success:
            self.logger.info(f"Successfully burned NFT {token_id}.")
            # Clear local state
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
            try:
                self.price_provider.fetch_new_prices() #
            except ConnectionError as ce:
                 self.logger.error(f"Failed to update price history during rebalance check due to connection error: {ce}")
            except Exception as e_fetch:
                 self.logger.error(f"Generic error updating price history during rebalance check: {e_fetch}")
            return

        self._get_existing_position()

        if not self.current_position_token_id:
            self.logger.info("No existing managed position found. Creating a new one.")
            target_tick_lower, target_tick_upper = self._calculate_target_ticks(current_tick, p_current_for_sd)
            self.logger.info(f"Calculated target ticks after calling _check_and_rebalance and no current position is active: {target_tick_lower} and {target_tick_upper}. About to _create_new_position")
            self.current_position_token_id = self._create_new_position(target_tick_lower, target_tick_upper)
            if self.current_position_token_id is None:
                self.logger.error("Create new LP position failed, no NFT ID returned")
            self.current_position_details = dex_utils.get_position_details(self.nft_manager_contract, self.current_position_token_id)
        else:
            pos_details = self.current_position_details
            pos_tick_lower = pos_details['tickLower']
            pos_tick_upper = pos_details['tickUpper']
            self.logger.info(f"Existing position TokenID {self.current_position_token_id}: Range=[{pos_tick_lower}, {pos_tick_upper}], Liquidity={pos_details['liquidity']}")

            # Rebalance if current tick is outside the position's range
            if not (pos_tick_lower <= current_tick < pos_tick_upper):
                self.logger.info(f"REBALANCE TRIGGERED: Current tick {current_tick} is outside position range [{pos_tick_lower}, {pos_tick_upper}].")
                self.logger.info("Step 1: Removing old position...")
                remove_success = self._remove_position(self.current_position_token_id)
                if not remove_success:
                    self.logger.error("Failed to remove old position during rebalance. Aborting rebalance cycle.")
                    self.price_provider.fetch_new_prices() #
                    return
                
                self.logger.info("Old position successfully removed.")
                self.current_position_token_id = None # Double check to ensure it's cleared
                self.current_position_details = None

                self.logger.info("Step 2: Creating new position at new target ticks...")
                # Re-fetch current state as market might have moved during removal
                current_tick_after_removal, _, p_current_for_sd_after_removal = self._get_current_tick_and_price_state()
                if current_tick_after_removal is None or p_current_for_sd_after_removal is None:
                     self.logger.error("Failed to get current price state after removal. Cannot create new position.")
                     self.price_provider.fetch_new_prices() #
                     return
                
                target_tick_lower, target_tick_upper = self._calculate_target_ticks(current_tick_after_removal, p_current_for_sd_after_removal)
                self.current_position_token_id = self._create_new_position(target_tick_lower, target_tick_upper)
                if self.current_position_token_id is None:
                    self.logger.error("Create new LP position failed, no NFT ID returned")
                self.current_position_details = dex_utils.get_position_details(self.nft_manager_contract, self.current_position_token_id)
            else:
                self.logger.info(f"No rebalance needed. Current tick {current_tick} is within position range [{pos_tick_lower}, {pos_tick_upper}].")
                # Still fetch new prices to keep history updated
                self.price_provider.fetch_new_prices() #


    def run(self):
        """Main operational loop for the strategy bot."""
        self.logger.info(f"Starting Strategy Bot. Poll interval: {config.POLL_INTERVAL_SECONDS} seconds.")
        self.logger.info("Strategy: Initial position check and price history fetch were done during __init__.")
        self.logger.info(f"Strategy: Initial full price history size from PriceProvider: {len(self.price_provider.all_prices_chronological)}.")

        while True:
            try:
                self._check_and_rebalance()
            except ConnectionError as ce:
                self.logger.error(f"A connection error occurred in the main strategy loop (likely during log fetching or RPC calls): {ce}", exc_info=True)
            except Exception as e:
                self.logger.error(f"An unexpected error occurred in the main strategy loop: {e}", exc_info=True)
                # Attempt to update price history if a generic error occurred, to keep provider state fresh
                try:
                    self.price_provider.fetch_new_prices() #
                except ConnectionError as fetch_ce:
                    self.logger.error(f"Failed to update price history after main loop ConnectionError: {fetch_ce}")
                except Exception as fetch_e:
                    self.logger.error(f"Failed to update price history after main loop error: {fetch_e}")
            
            self.logger.info(f"Next check in {config.POLL_INTERVAL_SECONDS} seconds... 😴")
            time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == '__main__':
    logger.info("Strategy.py executed directly. Initializing and running strategy...")
    try:
        start_block_for_strategy = getattr(config, 'PRICE_PROVIDER_START_BLOCK', 28028682)

        # Example parameters for the new strategy constructor
        # These could also come from config.py or environment variables
        sd_points = getattr(config, 'SD_CALCULATION_POINTS', 0)  # 0 for all history, or e.g., 1000 for last 1000 points
        sd_multi = getattr(config, 'SD_MULTIPLE', 1.0)         # e.g., 1.0 for 1x SD, 1.5 for 1.5x SD
        
        min_width = getattr(config, 'MIN_TICK_WIDTH', 200)     # Min ticks, e.g., 20 * 10 (tick_spacing)
        max_width = getattr(config, 'MAX_TICK_WIDTH', None)  # Max ticks, e.g., 2000, or None for no cap
        
        current_dir = os.path.dirname(os.path.abspath(__file__))
        cache_dir = os.path.join(current_dir, ".true_cache") 
        os.makedirs(cache_dir, exist_ok=True)
        price_cache_file = os.path.join(cache_dir, f"price_cache_for_cNGN_USDC.csv")
        
        strategy_bot = Strategy(
            sd_calculation_points=sd_points,
            sd_multiple=float(sd_multi), # Ensure float if coming from string config
            min_tick_width=int(min_width),
            max_tick_width=int(max_width) if max_width is not None else None,
            price_provider_start_block=start_block_for_strategy,
            price_provider_cache_path=price_cache_file
        )
        strategy_bot.run()
    except ConnectionError as ce:
        logger.critical(f"Failed to initialize or run strategy due to connection error: {ce}", exc_info=True)
    except ValueError as ve:
        logger.critical(f"Failed to initialize or run strategy due to value error: {ve}", exc_info=True)
    except Exception as e:
        logger.critical(f"Unhandled exception during strategy initialization or run: {e}", exc_info=True)