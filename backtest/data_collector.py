import logging
import pandas as pd
import os
import time
from decimal import Decimal
from typing import List, Dict, Optional, Union

from bot.price_provider import PriceProvider
import bot.config as config

class SwapDataCollector(PriceProvider):
    """
    Enhanced version of PriceProvider that collects complete swap data for backtesting.
    
    This collector maintains backward compatibility with PriceProvider while adding
    comprehensive swap data collection capabilities for historical analysis and backtesting.
    """
    
    def __init__(self, w3, pool_contract, cngn_reference_address, swap_event_topic0, 
                 initial_start_block, cache_file_path=None, collect_full_swap_data=True):
        """
        Initialize SwapDataCollector.
        
        Args:
            w3: Web3 instance
            pool_contract: Pool contract instance
            cngn_reference_address: Reference address for price inversion determination
            swap_event_topic0: Swap event topic hash
            initial_start_block: Starting block for data collection
            cache_file_path: Base path for cache files (will create variants)
            collect_full_swap_data: Whether to collect full swap data or just prices
        """
        # Initialize parent PriceProvider
        super().__init__(w3, pool_contract, cngn_reference_address, swap_event_topic0, 
                        initial_start_block, cache_file_path)
        
        self.collect_full_swap_data = collect_full_swap_data
        self.all_swaps_chronological = []  # Store complete swap data
        
        # Set up enhanced cache files
        if cache_file_path and collect_full_swap_data:
            self._setup_cache_paths(cache_file_path)
        else:
            self.swap_cache_file = None
            
        self.logger.info(f"SwapDataCollector initialized. Full data collection: {collect_full_swap_data}")
        
    def _setup_cache_paths(self, base_cache_path: str):
        """
        Sets up cache file paths for different data types.
        """
        # Remove .csv extension if present
        base_path = base_cache_path.replace('.csv', '')
        
        # Create specific cache files
        self.swap_cache_file = f"{base_path}_full_swaps.csv"
        
        # Update parent's cache file path to be specific to prices
        self.cache_file_path = f"{base_path}_prices.csv"
        
        self.logger.info(f"Cache files configured:")
        self.logger.info(f"  Prices: {self.cache_file_path}")
        self.logger.info(f"  Full swaps: {self.swap_cache_file}")
        
    def _process_log_entry_to_swap_data(self, log_entry) -> Optional[Dict]:
        """
        Processes a swap log entry into complete swap data structure.
        
        Args:
            log_entry: Raw log entry from blockchain
            
        Returns:
            Dictionary with complete swap data or None if processing failed
        """
        try:
            event_data = self.pool_contract.events.Swap().process_log(log_entry)
            
            # Extract basic swap data
            sqrt_price_x96 = Decimal(event_data['args']['sqrtPriceX96'])
            amount0 = int(event_data['args']['amount0'])
            amount1 = int(event_data['args']['amount1'])
            liquidity = int(event_data['args']['liquidity'])
            tick = int(event_data['args']['tick'])
            
            # Calculate price from sqrtPriceX96
            raw_price = (sqrt_price_x96 / config.Q96_DEC) ** 2
            
            # Apply price inversion if needed (consistent with parent class)
            if self.invert_price and raw_price != Decimal(0):
                oriented_price = Decimal(1) / raw_price
            else:
                oriented_price = raw_price
                
            # Determine swap direction and volume
            is_zero_for_one = amount0 > 0  # Positive amount0 means selling token0
            swap_volume_token0 = abs(amount0)
            swap_volume_token1 = abs(amount1)
            
            return {
                'block': int(log_entry['blockNumber']),
                'logIndex': int(log_entry['logIndex']),
                'transactionHash': log_entry['transactionHash'].hex(),
                'sender': event_data['args']['sender'],
                'recipient': event_data['args']['recipient'],
                'amount0': amount0,
                'amount1': amount1,
                'sqrtPriceX96': int(sqrt_price_x96),
                'liquidity': liquidity,
                'tick': tick,
                'price': float(oriented_price),
                'raw_price': float(raw_price),
                'is_zero_for_one': is_zero_for_one,
                'volume_token0': swap_volume_token0,
                'volume_token1': swap_volume_token1,
                'gas_used': None,  # Could be filled later if needed
                'gas_price': None,  # Could be filled later if needed
            }
            
        except Exception as e:
            self.logger.warning(f"Error processing swap log entry at block {log_entry.get('blockNumber', 'unknown')}: {e}")
            return None
            
    def _load_swaps_from_cache(self) -> Optional[List[Dict]]:
        """
        Loads swap data from cache file.
        
        Returns:
            List of swap dictionaries or None if loading failed
        """
        if not self.swap_cache_file or not os.path.exists(self.swap_cache_file):
            self.logger.info("No swap cache file found or path not set")
            return None
            
        try:
            self.logger.info(f"Loading swap data from cache: {self.swap_cache_file}")
            df = pd.read_csv(self.swap_cache_file)
            
            if df.empty:
                self.logger.info("Swap cache file is empty")
                return []
                
            # Ensure proper data types
            df['block'] = df['block'].astype(int)
            df['logIndex'] = df['logIndex'].astype(int)
            df['amount0'] = df['amount0'].astype(int)
            df['amount1'] = df['amount1'].astype(int)
            df['sqrtPriceX96'] = df['sqrtPriceX96'].astype(int)
            df['liquidity'] = df['liquidity'].astype(int)
            df['tick'] = df['tick'].astype(int)
            df['price'] = df['price'].astype(float)
            
            # Handle optional columns with defaults
            if 'raw_price' not in df.columns:
                df['raw_price'] = df['price']  # Fallback for older cache files
            if 'is_zero_for_one' not in df.columns:
                df['is_zero_for_one'] = df['amount0'] > 0
            if 'volume_token0' not in df.columns:
                df['volume_token0'] = df['amount0'].abs()
            if 'volume_token1' not in df.columns:
                df['volume_token1'] = df['amount1'].abs()
                
            # Convert to list of dicts
            swaps = df.to_dict('records')
            
            # Sort chronologically
            swaps.sort(key=lambda x: (x['block'], x['logIndex']))
            
            self.logger.info(f"Loaded {len(swaps)} swaps from cache (latest block: {swaps[-1]['block'] if swaps else 'N/A'})")
            return swaps
            
        except Exception as e:
            self.logger.warning(f"Failed to load swaps from cache: {e}")
            # Try to remove corrupted cache
            try:
                os.remove(self.swap_cache_file)
                self.logger.info("Removed corrupted swap cache file")
            except OSError:
                pass
            return None
            
    def _save_swaps_to_cache(self):
        """
        Saves swap data to cache file.
        """
        if not self.swap_cache_file or not self.all_swaps_chronological:
            return
            
        try:
            self.logger.info(f"Saving {len(self.all_swaps_chronological)} swaps to cache: {self.swap_cache_file}")
            
            # Ensure directory exists
            cache_dir = os.path.dirname(self.swap_cache_file)
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
                
            # Convert to DataFrame
            df = pd.DataFrame(self.all_swaps_chronological)
            
            # Ensure consistent column order
            column_order = [
                'block', 'logIndex', 'transactionHash', 'sender', 'recipient',
                'amount0', 'amount1', 'sqrtPriceX96', 'liquidity', 'tick',
                'price', 'raw_price', 'is_zero_for_one', 'volume_token0', 'volume_token1',
                'gas_used', 'gas_price'
            ]
            
            # Reorder columns, keeping any extras at the end
            existing_cols = [col for col in column_order if col in df.columns]
            extra_cols = [col for col in df.columns if col not in column_order]
            df = df[existing_cols + extra_cols]
            
            # Save to CSV
            df.to_csv(self.swap_cache_file, index=False)
            
            self.logger.info(f"Swap data saved successfully")
            
        except Exception as e:
            self.logger.error(f"Failed to save swaps to cache: {e}")
            
    def fetch_initial_swap_data(self):
        """
        Fetches initial swap data, using cache if available.
        
        This method extends the parent's fetch_initial_prices() to also collect
        full swap data when enabled.
        """
        if not self.collect_full_swap_data:
            # Fall back to original price-only behavior
            self.logger.info("Full swap data collection disabled, using price-only mode")
            return super().fetch_initial_prices()
            
        self.logger.info("Starting initial swap data collection...")
        
        # Load cached swap data
        cached_swaps = self._load_swaps_from_cache()
        
        # Also handle price cache for backward compatibility
        cached_prices = self._load_prices_from_cache()
        
        latest_block = self.w3.eth.block_number
        
        # Determine effective start block for fetching
        effective_start_block_swaps = self.start_block
        effective_start_block_prices = self.start_block
        
        # Initialize data containers
        self.all_swaps_chronological = []
        self.all_prices_chronological = []
        
        # Process cached swap data
        if cached_swaps is not None:
            if cached_swaps:  # Has cached swap data
                last_cached_swap_block = cached_swaps[-1]['block']
                
                if last_cached_swap_block > latest_block:
                    self.logger.warning("Swap cache is ahead of chain head, re-fetching all")
                else:
                    self.all_swaps_chronological = cached_swaps
                    effective_start_block_swaps = last_cached_swap_block + 1
                    self.logger.info(f"Using {len(cached_swaps)} cached swaps")
                    
        # Process cached price data
        if cached_prices is not None:
            if cached_prices:  # Has cached price data
                last_cached_price_block = cached_prices[-1]['block']
                
                if last_cached_price_block > latest_block:
                    self.logger.warning("Price cache is ahead of chain head, re-fetching all")
                else:
                    self.all_prices_chronological = cached_prices
                    effective_start_block_prices = last_cached_price_block + 1
                    self.logger.info(f"Using {len(cached_prices)} cached prices")
                    
        # Use the most restrictive start block
        effective_start_block = max(effective_start_block_swaps, effective_start_block_prices)
        
        # Fetch new data if needed
        if effective_start_block <= latest_block:
            self.logger.info(f"Fetching new swap data from block {effective_start_block} to {latest_block}")
            logs = self._fetch_logs_in_batches(effective_start_block, latest_block)
            
            new_swaps = []
            new_prices = []
            
            for log_entry in logs:
                # Process price data (for backward compatibility)
                price = self._process_log_entry_to_price(log_entry)
                if price is not None:
                    new_prices.append({
                        'price': price,
                        'block': log_entry['blockNumber'],
                        'logIndex': log_entry['logIndex']
                    })
                
                # Process full swap data
                swap_data = self._process_log_entry_to_swap_data(log_entry)
                if swap_data is not None:
                    new_swaps.append(swap_data)
                    
            # Add new data and sort
            if new_swaps:
                new_swaps.sort(key=lambda x: (x['block'], x['logIndex']))
                self.all_swaps_chronological.extend(new_swaps)
                self.all_swaps_chronological.sort(key=lambda x: (x['block'], x['logIndex']))
                self.logger.info(f"Added {len(new_swaps)} new swaps")
                
            if new_prices:
                new_prices.sort(key=lambda x: (x['block'], x['logIndex']))
                self.all_prices_chronological.extend(new_prices)
                self.all_prices_chronological.sort(key=lambda x: (x['block'], x['logIndex']))
                self.logger.info(f"Added {len(new_prices)} new price points")
                
        # Update tracking
        if self.all_swaps_chronological:
            self.last_processed_block = self.all_swaps_chronological[-1]['block']
        elif self.all_prices_chronological:
            self.last_processed_block = self.all_prices_chronological[-1]['block']
        else:
            self.last_processed_block = latest_block
            
        # Save to caches
        self._save_swaps_to_cache()
        if self.cache_file_path:  # Save price cache too
            self._save_prices_to_cache()
            
        # Ensure price data exists (derive from swaps if needed)
        if not self.all_prices_chronological and self.all_swaps_chronological:
            self.logger.info("Deriving price data from swap data")
            self.all_prices_chronological = [
                {
                    'price': swap['price'],
                    'block': swap['block'],
                    'logIndex': swap['logIndex']
                }
                for swap in self.all_swaps_chronological
            ]
            
        self.logger.info(
            f"Data collection complete. "
            f"Swaps: {len(self.all_swaps_chronological)}, "
            f"Prices: {len(self.all_prices_chronological)}, "
            f"Last processed block: {self.last_processed_block}"
        )
        
    def fetch_new_swap_data(self):
        """
        Fetches new swap data since last update.
        
        This extends the parent's fetch_new_prices() functionality.
        """
        if not self.collect_full_swap_data:
            return super().fetch_new_prices()
            
        if self.last_processed_block is None:
            self.logger.warning("last_processed_block is None, triggering full initial fetch")
            return self.fetch_initial_swap_data()
            
        current_block = self.w3.eth.block_number
        from_block = self.last_processed_block + 1
        
        if from_block > current_block:
            self.logger.debug(f"No new blocks to process (last: {self.last_processed_block}, current: {current_block})")
            return
            
        self.logger.info(f"Fetching new swap data from block {from_block} to {current_block}")
        logs = self._fetch_logs_in_batches(from_block, current_block)
        
        new_swaps = []
        new_prices = []
        
        for log_entry in logs:
            # Process price data
            price = self._process_log_entry_to_price(log_entry)
            if price is not None:
                new_prices.append({
                    'price': price,
                    'block': log_entry['blockNumber'],
                    'logIndex': log_entry['logIndex']
                })
            
            # Process swap data
            swap_data = self._process_log_entry_to_swap_data(log_entry)
            if swap_data is not None:
                new_swaps.append(swap_data)
                
        # Add new data
        if new_swaps:
            new_swaps.sort(key=lambda x: (x['block'], x['logIndex']))
            self.all_swaps_chronological.extend(new_swaps)
            self.logger.info(f"Added {len(new_swaps)} new swaps")
            
        if new_prices:
            new_prices.sort(key=lambda x: (x['block'], x['logIndex']))
            self.all_prices_chronological.extend(new_prices)
            self.logger.info(f"Added {len(new_prices)} new price points")
            
        # Update tracking
        self.last_processed_block = current_block
        
        # Save to caches
        if new_swaps or new_prices:
            self._save_swaps_to_cache()
            if self.cache_file_path:
                self._save_prices_to_cache()
                
    def get_swaps_for_backtest(self, start_block: int = None, end_block: int = None) -> pd.DataFrame:
        """
        Returns swap data as DataFrame for backtesting.
        
        Args:
            start_block: Optional start block filter
            end_block: Optional end block filter
            
        Returns:
            DataFrame with swap data sorted chronologically
        """
        if not self.all_swaps_chronological:
            self.logger.warning("No swap data available for backtest")
            return pd.DataFrame()
            
        df = pd.DataFrame(self.all_swaps_chronological)
        
        # Apply block filters
        if start_block is not None:
            df = df[df['block'] >= start_block]
        if end_block is not None:
            df = df[df['block'] <= end_block]
            
        return df.sort_values(['block', 'logIndex']).reset_index(drop=True)
        
    def get_swap_summary_stats(self, start_block: int = None, end_block: int = None) -> Dict:
        """
        Returns summary statistics for the collected swap data.
        
        Args:
            start_block: Optional start block filter
            end_block: Optional end block filter
            
        Returns:
            Dictionary with summary statistics
        """
        df = self.get_swaps_for_backtest(start_block, end_block)
        
        if df.empty:
            return {}
            
        return {
            'total_swaps': len(df),
            'block_range': {
                'start': int(df['block'].min()),
                'end': int(df['block'].max()),
                'span': int(df['block'].max() - df['block'].min())
            },
            'price_range': {
                'min': float(df['price'].min()),
                'max': float(df['price'].max()),
                'mean': float(df['price'].mean()),
                'std': float(df['price'].std())
            },
            'volume_stats': {
                'total_volume_token0': int(df['volume_token0'].sum()),
                'total_volume_token1': int(df['volume_token1'].sum()),
                'avg_volume_token0': float(df['volume_token0'].mean()),
                'avg_volume_token1': float(df['volume_token1'].mean())
            },
            'swap_direction': {
                'zero_for_one_count': int(df['is_zero_for_one'].sum()),
                'one_for_zero_count': int((~df['is_zero_for_one']).sum()),
                'zero_for_one_pct': float(df['is_zero_for_one'].mean() * 100)
            },
            'liquidity_stats': {
                'min_liquidity': int(df['liquidity'].min()),
                'max_liquidity': int(df['liquidity'].max()),
                'avg_liquidity': float(df['liquidity'].mean())
            }
        }
        
    def export_swap_data_csv(self, output_file: str, start_block: int = None, end_block: int = None, 
                           include_summary: bool = True):
        """
        Exports swap data to CSV for external analysis.
        
        Args:
            output_file: Path to output CSV file
            start_block: Optional start block filter
            end_block: Optional end block filter
            include_summary: Whether to also export a summary file
        """
        df = self.get_swaps_for_backtest(start_block, end_block)
        
        if df.empty:
            self.logger.warning("No data to export")
            return
            
        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        
        # Export main data
        df.to_csv(output_file, index=False)
        self.logger.info(f"Exported {len(df)} swap records to {output_file}")
        
        # Export summary if requested
        if include_summary:
            summary_file = output_file.replace('.csv', '_summary.txt')
            stats = self.get_swap_summary_stats(start_block, end_block)
            
            with open(summary_file, 'w') as f:
                f.write("Swap Data Summary\n")
                f.write("=" * 50 + "\n\n")
                
                for category, data in stats.items():
                    f.write(f"{category.replace('_', ' ').title()}:\n")
                    if isinstance(data, dict):
                        for key, value in data.items():
                            f.write(f"  {key}: {value}\n")
                    else:
                        f.write(f"  {data}\n")
                    f.write("\n")
                    
            self.logger.info(f"Exported summary to {summary_file}")
            
    # Override parent methods to maintain compatibility
    def fetch_initial_prices(self):
        """Backward compatibility wrapper."""
        return self.fetch_initial_swap_data()
        
    def fetch_new_prices(self):
        """Backward compatibility wrapper.""" 
        return self.fetch_new_swap_data()



if __name__ == "__main__":
    import sys
    import argparse
    from bot.utils.blockchain_utils import get_web3_provider, load_contract
    
    # Set up logging
    logging.basicConfig(
        level=logging.INFO, 
        format="%(asctime)s [%(levelname)s][%(name)s:%(lineno)s] %(message)s"
    )
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Collect swap data for backtesting')
    parser.add_argument('--start-block', type=int, default=28028682, 
                       help='Starting block number (default: pool creation)')
    parser.add_argument('--end-block', type=int, default=None,
                       help='Ending block number (default: latest)')
    parser.add_argument('--output-dir', type=str, default='./backtest_data',
                       help='Output directory for data files')
    parser.add_argument('--cache-dir', type=str, default='./cache',
                       help='Cache directory for intermediate files')
    parser.add_argument('--export-csv', action='store_true',
                       help='Export data to CSV after collection')
    
    args = parser.parse_args()
    
    try:
        print("Initializing SwapDataCollector...")
        
        # Setup Web3 and contracts
        w3 = get_web3_provider()
        pool_contract = load_contract(w3, config.POOL_ADDRESS, config.MINIMAL_POOL_ABI)
        
        if not pool_contract:
            print("ERROR: Failed to load pool contract")
            sys.exit(1)
            
        # Event topic for Swap events
        swap_event_sig = "Swap(address,address,int256,int256,uint160,uint128,int24)"
        swap_topic0 = w3.keccak(text=swap_event_sig).hex()
        
        # Set up cache path
        cache_file = os.path.join(args.cache_dir, f"pool_{config.POOL_ADDRESS[-8:]}_data.csv")
        
        # Create collector
        collector = SwapDataCollector(
            w3=w3,
            pool_contract=pool_contract,
            cngn_reference_address=config.TOKEN0_ADDRESS,
            swap_event_topic0=swap_topic0,
            initial_start_block=args.start_block,
            cache_file_path=cache_file,
            collect_full_swap_data=True
        )
        
        print(f"Collecting swap data from block {args.start_block} to {args.end_block or 'latest'}...")
        start_time = time.time()
        
        # Collect data
        collector.fetch_initial_swap_data()
        
        # Get summary stats
        stats = collector.get_swap_summary_stats()
        
        print(f"\nCollection completed in {time.time() - start_time:.2f} seconds")
        print(f"Total swaps collected: {stats.get('total_swaps', 0)}")
        print(f"Block range: {stats.get('block_range', {})}")
        print(f"Price range: {stats.get('price_range', {})}")
        
        # Export to CSV if requested
        if args.export_csv:
            output_file = os.path.join(args.output_dir, 
                                     f"cngn_usdc_swaps_{args.start_block}_{args.end_block or 'latest'}.csv")
            collector.export_swap_data_csv(output_file, args.start_block, args.end_block)
            print(f"Data exported to {output_file}")
            
        print("SwapDataCollector execution completed successfully!")
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)