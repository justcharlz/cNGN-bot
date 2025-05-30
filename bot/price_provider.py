import logging
from decimal import Decimal
import bot.config as config
import time
import os
import pandas as pd

class PriceProvider:
    """
    Handles fetching and maintaining a complete history of pool prices
    from a specified start block, with CSV caching capabilities.
    """
    MAX_FETCH_RETRIES = 5
    INITIAL_BACKOFF_SECONDS = 3
    BACKOFF_FACTOR = 2
    DEFAULT_POST_REQUEST_DELAY_SECONDS = 0.25

    def __init__(self, w3, pool_contract, cngn_reference_address, swap_event_topic0, initial_start_block, cache_file_path=None):
        self.logger = logging.getLogger(__name__ + ".PriceProvider")
        self.w3 = w3
        self.pool_contract = pool_contract
        self.all_prices_chronological = []
        self.cngn_reference_address = cngn_reference_address.lower()
        self.swap_event_topic0 = swap_event_topic0
        self.start_block = initial_start_block
        self.last_processed_block = None
        self.cache_file_path = cache_file_path
        self._determine_price_inversion()

    # Assuming that USDC will always be the stronger stable (i.e, active ticks stay < 0)
    def _determine_price_inversion(self):
        try:
            pool_token0_addr = self.pool_contract.functions.token0().call().lower()
            if pool_token0_addr == self.cngn_reference_address:
                self.invert_price = True
            else:
                self.invert_price = False
            self.logger.info(f"Price inversion for price calculations set to {self.invert_price} (based on token0: {pool_token0_addr}, ref cNGN: {self.cngn_reference_address})")
        except Exception as e:
            self.logger.error(f"Error determining price inversion: {e}. Defaulting to False.")
            self.invert_price = False

    def _process_log_entry_to_price(self, log_entry):
        try:
            event_data = self.pool_contract.events.Swap().process_log(log_entry)
            sqrt_price_x96 = Decimal(event_data['args']['sqrtPriceX96'])
            price = (sqrt_price_x96 / config.Q96_DEC) ** 2
            if self.invert_price:
                if price != Decimal(0):
                    price = Decimal(1) / price
                else:
                    self.logger.warning("Price is zero before inversion, cannot invert. Skipping log.")
                    return None
            return float(price)
        except Exception as e:
            self.logger.warning(f"Error processing log entry to price: {e}. Log: {log_entry}")
            return None

    def _fetch_logs_in_batches(self, from_block, to_block, batch_size=10000):
        all_logs = []
        self.logger.info(f"Batch fetching Swap logs from block {from_block} to {to_block} (batch size: {batch_size})")
        current_batch_start_block = from_block
        while current_batch_start_block <= to_block:
            current_batch_end_block = min(current_batch_start_block + batch_size - 1, to_block)
            self.logger.debug(f"Preparing to fetch log batch: {current_batch_start_block} -> {current_batch_end_block}")
            attempt = 0
            current_backoff_seconds = self.INITIAL_BACKOFF_SECONDS
            while attempt < self.MAX_FETCH_RETRIES:
                try:
                    self.logger.debug(f"Attempt {attempt + 1}/{self.MAX_FETCH_RETRIES} for batch {current_batch_start_block}-{current_batch_end_block}")
                    logs_this_batch = self.w3.eth.get_logs({
                        "address": self.pool_contract.address,
                        "fromBlock": current_batch_start_block,
                        "toBlock": current_batch_end_block,
                        "topics": [self.swap_event_topic0]
                    })
                    all_logs.extend(logs_this_batch)
                    self.logger.debug(f"Successfully fetched {len(logs_this_batch)} logs for batch {current_batch_start_block}-{current_batch_end_block}.")
                    time.sleep(self.DEFAULT_POST_REQUEST_DELAY_SECONDS)
                    break
                except ValueError as ve:
                    error_message = str(ve).lower()
                    is_rate_limit_error = ("429" in error_message or "too many requests" in error_message or
                                           "-32005" in error_message or "rate limit" in error_message or
                                           "exceeded cus" in error_message)
                    if is_rate_limit_error:
                        if attempt < self.MAX_FETCH_RETRIES - 1:
                            self.logger.warning(
                                f"Rate limit encountered on attempt {attempt + 1} for batch {current_batch_start_block}-{current_batch_end_block}. "
                                f"Waiting {current_backoff_seconds}s. Error: {ve}")
                            time.sleep(current_backoff_seconds)
                            current_backoff_seconds *= self.BACKOFF_FACTOR
                            attempt += 1
                        else:
                            self.logger.error(f"Max retries ({self.MAX_FETCH_RETRIES}) reached for rate-limited batch {current_batch_start_block}-{current_batch_end_block}. Error: {ve}")
                            raise ConnectionError(f"Failed to fetch logs for batch {current_batch_start_block}-{current_batch_end_block} after {self.MAX_FETCH_RETRIES} rate-limited attempts: {ve}") from ve
                    else:
                        self.logger.error(f"Non-rate-limit ValueError on attempt {attempt + 1} for batch {current_batch_start_block}-{current_batch_end_block}: {ve}", exc_info=True)
                        raise
                except Exception as e:
                    self.logger.warning(f"General network/RPC error on attempt {attempt + 1} for batch {current_batch_start_block}-{current_batch_end_block}. Waiting {current_backoff_seconds}s. Error: {e}")
                    if attempt < self.MAX_FETCH_RETRIES - 1:
                        time.sleep(current_backoff_seconds)
                        current_backoff_seconds *= self.BACKOFF_FACTOR
                        attempt += 1
                    else:
                        self.logger.error(f"Max retries ({self.MAX_FETCH_RETRIES}) reached for batch {current_batch_start_block}-{current_batch_end_block} due to general errors. Last error: {e}")
                        raise ConnectionError(f"Failed to fetch logs for batch {current_batch_start_block}-{current_batch_end_block} after {self.MAX_FETCH_RETRIES} general error attempts: {e}") from e
            current_batch_start_block = current_batch_end_block + 1
        self.logger.info(f"Finished batch fetching. Total logs collected: {len(all_logs)} from {from_block} to {to_block}.")
        return all_logs

    def _load_prices_from_cache(self):
        if self.cache_file_path and os.path.exists(self.cache_file_path):
            try:
                self.logger.info(f"Loading price history from cache: {self.cache_file_path}")
                df = pd.read_csv(self.cache_file_path)
                if df.empty:
                    self.logger.info(f"Cache file '{self.cache_file_path}' is empty.")
                    return [] # Return empty list, not None, if file is empty but valid
                df['block'] = df['block'].astype(int)
                df['logIndex'] = df['logIndex'].astype(int)
                df['price'] = df['price'].astype(float)
                loaded_prices = df.to_dict('records')
                loaded_prices.sort(key=lambda x: (x['block'], x['logIndex']))
                return loaded_prices
            except pd.errors.EmptyDataError:
                self.logger.info(f"Cache file '{self.cache_file_path}' is empty or invalid (pandas EmptyDataError).")
                return []
            except Exception as e:
                self.logger.warning(f"Failed to load prices from cache '{self.cache_file_path}': {e}. A full refetch will occur.")
                if os.path.exists(self.cache_file_path): # Attempt to remove corrupted cache
                    try:
                        os.remove(self.cache_file_path)
                        self.logger.info(f"Removed potentially corrupted cache file: {self.cache_file_path}")
                    except OSError as oe:
                        self.logger.error(f"Error removing corrupted cache file '{self.cache_file_path}': {oe}")
                return None # Indicates error in loading
        return None # Cache does not exist

    def _save_prices_to_cache(self):
        if self.cache_file_path and self.all_prices_chronological is not None: # Allow saving empty list
            try:
                self.logger.info(f"Saving {len(self.all_prices_chronological)} prices to cache: {self.cache_file_path}")
                cache_dir = os.path.dirname(self.cache_file_path)
                if cache_dir: # Ensure directory exists if path includes one
                    os.makedirs(cache_dir, exist_ok=True)
                df = pd.DataFrame(self.all_prices_chronological)
                df.to_csv(self.cache_file_path, index=False)
            except Exception as e:
                self.logger.error(f"Failed to save prices to cache '{self.cache_file_path}': {e}")

    def fetch_initial_prices(self):
        self.logger.info(f"Fetching initial prices. Configured start block: {self.start_block}.")
        
        cached_prices = self._load_prices_from_cache()
        latest_block_on_chain = self.w3.eth.block_number
        
        effective_start_block_for_fetch = self.start_block
        self.all_prices_chronological = [] # Default to empty, will be populated by cache or fetch

        if cached_prices is not None: # Cache existed (might be empty list or contain prices)
            if not cached_prices: # Cache was successfully read as an empty list
                self.logger.info("Price cache was empty. Will fetch from configured start_block.")
            else: # Cache has prices
                last_cached_block = cached_prices[-1]['block']
                if last_cached_block > latest_block_on_chain:
                    self.logger.warning(f"Cache's last block ({last_cached_block}) is ahead of current chain head ({latest_block_on_chain}). Cache is stale. Re-fetching all from configured start_block.")
                    # Stale cache, so effective_start_block_for_fetch remains self.start_block and all_prices_chronological is empty
                else:
                    self.all_prices_chronological = cached_prices
                    self.logger.info(f"Loaded {len(self.all_prices_chronological)} prices from cache. Last cached block: {last_cached_block}.")
                    effective_start_block_for_fetch = last_cached_block + 1
        else: # No cache file or error loading it.
             self.logger.info("No cache found or error during load. Will fetch from configured start_block.")
             # effective_start_block_for_fetch remains self.start_block and all_prices_chronological is empty


        logs_to_process = []
        if effective_start_block_for_fetch <= latest_block_on_chain:
            self.logger.info(f"Fetching logs from block {effective_start_block_for_fetch} to {latest_block_on_chain}.")
            logs_to_process = self._fetch_logs_in_batches(effective_start_block_for_fetch, latest_block_on_chain)
        else:
            self.logger.info(f"No new blocks to fetch. Effective start for fetch {effective_start_block_for_fetch}, chain head {latest_block_on_chain}.")

        newly_processed_prices = []
        for log_entry in logs_to_process:
            price = self._process_log_entry_to_price(log_entry)
            if price is not None:
                newly_processed_prices.append({
                    'price': price,
                    'block': log_entry['blockNumber'],
                    'logIndex': log_entry['logIndex']
                })
        
        if newly_processed_prices:
            newly_processed_prices.sort(key=lambda x: (x['block'], x['logIndex']))
            self.all_prices_chronological.extend(newly_processed_prices)
            # Re-sort to merge cached and newly fetched prices correctly
            self.all_prices_chronological.sort(key=lambda x: (x['block'], x['logIndex']))
            self.logger.info(f"Processed and added {len(newly_processed_prices)} new prices.")

        if self.all_prices_chronological:
            # Use the latest block from fetched data or chain head if no new logs were processed but cache existed
            last_data_block = self.all_prices_chronological[-1]['block']
            self.last_processed_block = max(last_data_block, latest_block_on_chain if not logs_to_process else last_data_block)
        else: 
            self.last_processed_block = latest_block_on_chain
            
        self._save_prices_to_cache()
        self.logger.info(f"Price history initialized/updated. Total prices: {len(self.all_prices_chronological)}. Last processed block for scan: {self.last_processed_block}")


    def fetch_new_prices(self):
        if self.last_processed_block is None:
            self.logger.warning("last_processed_block is None. Triggering initial price fetch.")
            self.fetch_initial_prices() # This will also handle caching
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
            newly_added_prices_metadata.sort(key=lambda x: (x['block'], x['logIndex']))
            self.all_prices_chronological.extend(newly_added_prices_metadata)
            self.logger.info(f"Added {len(newly_added_prices_metadata)} new prices. Total price history size: {len(self.all_prices_chronological)}.")
            self._save_prices_to_cache() # Save updates to cache

        self.last_processed_block = current_block_on_chain

    def get_all_prices_for_sd(self):
        return [item['price'] for item in self.all_prices_chronological]