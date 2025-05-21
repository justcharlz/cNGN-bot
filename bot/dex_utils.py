import time
import logging
from web3 import Web3
import config
from blockchain_utils import load_contract, get_web3_provider, get_wallet_credentials

logger = logging.getLogger(__name__)

# --- Read Functions ---
def get_pool_info(pool_contract):
    if not pool_contract:
        logger.error("Pool contract not loaded.")
        return None
    try:
        token0 = pool_contract.functions.token0().call()
        token1 = pool_contract.functions.token1().call()
        tick_spacing = pool_contract.functions.tickSpacing().call()
        fee = pool_contract.functions.fee().call() 
        logger.info(f"Pool Info: Token0={token0}, Token1={token1}, TickSpacing={tick_spacing}, Fee={fee}")
        return {
            "token0": token0, "token1": token1,
            "tickSpacing": tick_spacing, "fee": fee
        }
    except Exception as e:
        logger.error(f"Error fetching pool info for {pool_contract.address}: {e}")
        return None

def get_current_pool_state(pool_contract):
    if not pool_contract:
        logger.error("Pool contract not loaded for get_current_pool_state.")
        return None
    try:
        slot0_data = pool_contract.functions.slot0().call()
        logger.debug(f"Raw Slot0 data: {slot0_data}")
        # Corrected parsing for 6 fields based on user's note
        return {
            "sqrtPriceX96": slot0_data[0],
            "tick": slot0_data[1],
            "observationIndex": slot0_data[2],
            "observationCardinality": slot0_data[3],
            "observationCardinalityNext": slot0_data[4],
            "unlocked": slot0_data[5],
            "feeProtocol": 0 # Placeholder, as it's not in this pool's slot0
        }
    except Exception as e:
        logger.error(f"Error fetching slot0 from pool {pool_contract.address}: {e}", exc_info=True)
        return None

def get_position_details(nft_manager_contract, token_id):
    if not nft_manager_contract:
        logger.error("NFT manager contract not loaded for get_position_details.")
        return None
    try:
        pos_details = nft_manager_contract.functions.positions(token_id).call()
        logger.info(f"Position details for tokenId {token_id}: {pos_details}")
        return {
            "nonce": pos_details[0], "operator": pos_details[1],
            "token0": pos_details[2], "token1": pos_details[3],
            "tickSpacing": pos_details[4], "tickLower": pos_details[5],
            "tickUpper": pos_details[6], "liquidity": pos_details[7],
            "feeGrowthInside0LastX128": pos_details[8],
            "feeGrowthInside1LastX128": pos_details[9],
            "tokensOwed0": pos_details[10], "tokensOwed1": pos_details[11]
        }
    except Exception as e:
        logger.error(f"Error fetching position details for tokenId {token_id}: {e}")
        return None

def get_owner_token_ids(nft_manager_contract, owner_address):
    if not nft_manager_contract:
        logger.error("NFT manager contract not loaded for get_owner_token_ids.")
        return []
    token_ids = []
    try:
        balance = nft_manager_contract.functions.balanceOf(owner_address).call()
        logger.info(f"Address {owner_address} owns {balance} position NFTs.")
        for i in range(balance):
            try:
                token_id = nft_manager_contract.functions.tokenOfOwnerByIndex(owner_address, i).call()
                token_ids.append(token_id)
            except Exception as e_idx:
                logger.error(f"Error fetching tokenId at index {i} for owner {owner_address}: {e_idx}")
        logger.info(f"Token IDs for {owner_address}: {token_ids}")
        return token_ids
    except Exception as e:
        logger.error(f"Error fetching token balance for owner {owner_address}: {e}")
        return []

# --- Transaction Helper Functions ---
def build_eip1559_transaction_params(w3, from_address, to_address=None, value_wei=0, data=None):
    try:
        latest_block = w3.eth.get_block('latest')
        base_fee_per_gas = latest_block.get('baseFeePerGas')

        if base_fee_per_gas is None:
            logger.warning("baseFeePerGas not found. Falling back to legacy gasPrice.")
            return {'from': from_address, 'nonce': w3.eth.get_transaction_count(from_address),
                    'gasPrice': w3.eth.gas_price, 'value': value_wei, 'chainId': config.CHAIN_ID, 'data': data}

        suggested_tip = w3.eth.max_priority_fee
        min_priority_fee_gwei = getattr(config, 'EIP1559_MIN_PRIORITY_FEE_GWEI', 1.0)
        min_priority_fee_wei = Web3.to_wei(min_priority_fee_gwei, 'gwei')
        max_priority_fee_per_gas = max(suggested_tip, min_priority_fee_wei)
        
        base_fee_multiplier = getattr(config, 'EIP1559_MAX_FEE_BASE_MULTIPLIER', 1.5)
        max_fee_per_gas = int(base_fee_multiplier * base_fee_per_gas) + max_priority_fee_per_gas
        
        if max_fee_per_gas < (base_fee_per_gas + max_priority_fee_per_gas): # Safety check
             max_fee_per_gas = base_fee_per_gas + max_priority_fee_per_gas
        
        logger.debug(f"EIP-1559: baseFee={base_fee_per_gas}, priorityFee={max_priority_fee_per_gas}, maxFee={max_fee_per_gas}")
        tx_params = {'from': from_address, 'nonce': w3.eth.get_transaction_count(from_address),
                     'maxPriorityFeePerGas': max_priority_fee_per_gas, 'maxFeePerGas': max_fee_per_gas,
                     'value': value_wei, 'chainId': config.CHAIN_ID}
        if to_address: tx_params['to'] = to_address 
        if data: tx_params['data'] = data 
        return tx_params
    except Exception as e:
        logger.error(f"Error building EIP-1559 params: {e}. Falling back to legacy.", exc_info=True)
        return {'from': from_address, 'nonce': w3.eth.get_transaction_count(from_address),
                'gasPrice': w3.eth.gas_price, 'value': value_wei, 'chainId': config.CHAIN_ID, 'data': data}

def sign_and_send_transaction(w3, transaction, private_key):
    try:
        if 'gas' not in transaction or transaction['gas'] is None or transaction['gas'] == 0 :
            logger.error("Transaction 'gas' limit not set or invalid before signing.")
            return None
        signed_txn = w3.eth.account.sign_transaction(transaction, private_key)
        txn_hash = w3.eth.send_raw_transaction(signed_txn.rawTransaction)
        logger.info(f"Transaction sent: {txn_hash.hex()}")
        txn_receipt = w3.eth.wait_for_transaction_receipt(txn_hash, timeout=300) 
        status_msg = 'Success' if txn_receipt.status == 1 else 'Failed'
        logger.info(f"Tx confirmed. Block: {txn_receipt.blockNumber}, Status: {status_msg}, GasUsed: {txn_receipt.gasUsed}")
        if txn_receipt.status == 0:
            logger.error(f"Transaction REVERTED: {txn_receipt}")
            return None
        return txn_receipt
    except Exception as e:
        logger.error(f"Error signing/sending transaction: {e}", exc_info=True)
        return None

# --- Write Functions (Refactored Gas Estimation) ---

def _estimate_and_build_tx(w3, contract_function_prepared_with_args, from_address, to_contract_address, value_wei=0, encoded_data_override=None):
    """
    Internal helper to estimate gas and build full tx params.
    Expects contract_function_prepared_with_args to be the result of contract.functions.MyFunction(args)
    OR for encoded_data_override to be provided.
    """
    encoded_data = encoded_data_override
    fn_name_for_log = "unknown_function (data_override)" # Default if only override is used

    if not encoded_data:
        if not contract_function_prepared_with_args:
            logger.error("Either contract_function_prepared_with_args or encoded_data_override must be provided to _estimate_and_build_tx.")
            raise ValueError("No function or data to encode for transaction.")
        try:
            # Attempt to get function name for logging even if encodeABI might fail or be overridden
            if hasattr(contract_function_prepared_with_args, 'fn_name'):
                fn_name_for_log = contract_function_prepared_with_args.fn_name
            else:
                fn_name_for_log = 'unknown_function_obj'
            
            logger.debug(f"Attempting to call encodeABI() on object of type: {type(contract_function_prepared_with_args)} for function: {fn_name_for_log}")
            encoded_data = contract_function_prepared_with_args.encodeABI()
            logger.debug(f"Successfully called encodeABI() for {fn_name_for_log}")

        except AttributeError as ae: #
            logger.error(f"AttributeError on encodeABI for {fn_name_for_log}: {ae}. Object type: {type(contract_function_prepared_with_args)}", exc_info=True) #
            raise 
        except Exception as e:
            logger.error(f"Error encoding ABI for {fn_name_for_log} using .encodeABI(): {e}", exc_info=True)
            raise
    elif contract_function_prepared_with_args and hasattr(contract_function_prepared_with_args, 'fn_name'):
        # If data is overridden but we still have the function object, use its name for logging
        fn_name_for_log = contract_function_prepared_with_args.fn_name
    
    if not encoded_data:
        logger.error(f"Failed to obtain encoded_data for {fn_name_for_log}.")
        return None # Indicate failure

    gas_estimation_dict = {'from': from_address, 'to': to_contract_address, 'value': value_wei, 'data': encoded_data}
    
    try:
        estimated_gas = w3.eth.estimate_gas(gas_estimation_dict)
        logger.info(f"Raw estimated gas for {fn_name_for_log}: {estimated_gas}")
    except Exception as e:
        logger.error(f"Gas estimation failed for {fn_name_for_log}: {e}", exc_info=True)
        if hasattr(e, 'message') and e.message: logger.error(f"GasEst Revert Msg: {e.message}")
        if hasattr(e, 'data') and e.data: logger.error(f"GasEst Revert Data: {e.data}")
        return None # Indicate failure

    tx_params = build_eip1559_transaction_params(w3, from_address, 
                                                 to_address=to_contract_address, 
                                                 value_wei=value_wei) 
    
    tx_params['gas'] = int(estimated_gas * getattr(config, 'GAS_LIMIT_MULTIPLIER', 1.2)) #
    tx_params['data'] = encoded_data 
    return tx_params


def approve_token_if_needed(w3, token_address, owner_address, spender_address, amount_to_approve, account_private_key):
    token_contract = load_contract(w3, token_address, config.MINIMAL_ERC20_ABI) #
    if not token_contract: return False
    try:
        current_allowance = token_contract.functions.allowance(owner_address, spender_address).call() #
        logger.info(f"Allowance for {spender_address} by {owner_address} on {token_address}: {current_allowance}")
        if current_allowance < amount_to_approve:
            logger.info(f"Approving {amount_to_approve} for {token_address}...")
            
            encoded_data_for_approve = None
            # This object is primarily for logging purposes if fn_name can be extracted.
            # The actual encoding will use contract.encodeABI.
            approve_function_object_for_logging = None
            try:
                approve_function_object_for_logging = token_contract.functions.approve(spender_address, amount_to_approve)
            except Exception as e_obj:
                logger.warning(f"Could not create approve function object for logging: {e_obj}")


            try:
                # Use contract.encodeABI directly to get the calldata
                encoded_data_for_approve = token_contract.encodeABI(fn_name="approve", args=[spender_address, amount_to_approve])
                logger.info(f"Successfully generated encoded_data for 'approve' via contract.encodeABI().")
            except Exception as e:
                logger.error(f"Critical error: Failed to generate encoded_data for 'approve' using contract.encodeABI(): {e}", exc_info=True)
                # Log details about the problematic object if it was created
                if approve_function_object_for_logging:
                    logger.error(f"Type of 'token_contract.functions.approve(args)' object was: {type(approve_function_object_for_logging)}")
                    try:
                        logger.error(f"Attributes of this object: {dir(approve_function_object_for_logging)}")
                    except:
                        pass # dir() might fail on some strange objects
                return False

            final_tx_params = _estimate_and_build_tx(
                w3, 
                approve_function_object_for_logging, # Pass for logging fn_name if available
                owner_address, 
                token_contract.address,
                encoded_data_override=encoded_data_for_approve # Provide the reliably encoded data
            )
            if not final_tx_params: 
                logger.error(f"Failed to build transaction parameters for approve call to {token_address}.")
                return False 

            receipt = sign_and_send_transaction(w3, final_tx_params, account_private_key) #
            return receipt is not None and receipt.status == 1
        else:
            logger.info("Sufficient allowance present.") #
            return True
    except Exception as e:
        logger.error(f"Error in approve_token_if_needed for {token_address}: {e}", exc_info=True)
        return False

def mint_new_position(w3, nft_manager_contract, pool_contract, params, account, wallet_address): #
    if not nft_manager_contract or not pool_contract: 
        logger.error("NFT manager or Pool contract not loaded for minting.")
        return None
    logger.info(f"Attempting to mint with params: {params}")

    MAX_UINT256 = 2**256 - 1
    token0_addr = Web3.to_checksum_address(params[0])
    token1_addr = Web3.to_checksum_address(params[1])

    # Approvals are handled by the calling test script logic or should be ensured before this
    # For robustness, explicit approval checks/calls could be re-added here if this function
    # is to be used as a standalone utility that must ensure approvals.
    # However, given the prior fix, let's assume approvals are managed externally for now
    # or the approve_token_if_needed calls were intended to be part of this flow.
    # Re-adding them for completeness based on the original structure:
    if not approve_token_if_needed(w3, token0_addr, wallet_address, nft_manager_contract.address, MAX_UINT256, account.key):
        logger.error(f"Approval failed for token0 ({token0_addr}) within mint_new_position") #
        return None
    if not approve_token_if_needed(w3, token1_addr, wallet_address, nft_manager_contract.address, MAX_UINT256, account.key):
        logger.error(f"Approval failed for token1 ({token1_addr}) within mint_new_position") #
        return None
    logger.info("Token approvals successful for mint_new_position.")


    mint_params_tuple = (
        token0_addr,
        token1_addr,
        params[2],
        params[3],
        params[4],
        params[5],
        params[6],
        params[7],
        params[8],
        Web3.to_checksum_address(params[9]),
        params[10],
        params[11] 
    )

    try:
        mint_function_object_for_logging = None
        try:
            mint_function_object_for_logging = nft_manager_contract.functions.mint(mint_params_tuple)
        except Exception as e_obj:
            logger.warning(f"Could not create mint function object for logging: {e_obj}")

        encoded_data_for_mint = None
        try:
            # The 'mint' function in the ABI takes a single 'params' tuple which is `mint_params_tuple`
            encoded_data_for_mint = nft_manager_contract.encodeABI(fn_name="mint", args=[mint_params_tuple]) 
            logger.info(f"Successfully generated encoded_data for 'mint' via contract.encodeABI().")
        except Exception as e:
            logger.error(f"Critical error: Failed to generate encoded_data for 'mint' using contract.encodeABI(): {e}", exc_info=True)
            if mint_function_object_for_logging:
                logger.error(f"Type of 'nft_manager_contract.functions.mint(args)' object was: {type(mint_function_object_for_logging)}")
                try: logger.error(f"Attributes of this object: {dir(mint_function_object_for_logging)}")
                except: pass
            return None
        
        final_tx_params = _estimate_and_build_tx(
            w3, 
            mint_function_object_for_logging,
            wallet_address,
            nft_manager_contract.address,
            encoded_data_override=encoded_data_for_mint
        )
        
        if not final_tx_params:
            logger.error("Failed to build transaction for mint (gas estimation or param build failed).")
            return None

        logger.info(f"Sending mint transaction with calculated gas and EIP-1559 params: {final_tx_params}")
        receipt = sign_and_send_transaction(w3, final_tx_params, account.key)

        if receipt and receipt.status == 1:
            logger.info("Mint transaction successful.")
            return receipt
        else:
            logger.error(f"Mint transaction failed or no receipt. Receipt: {receipt}")
            return None
    except Exception as e:
        logger.error(f"Exception during mint_new_position: {e}", exc_info=True)
        return None

def decrease_liquidity_and_collect_position(w3, nft_manager_contract, token_id, liquidity_to_decrease,
                                            amount0_min, amount1_min, recipient_address,
                                            account, wallet_address): #
    if not nft_manager_contract: return None
    deadline = int(time.time()) + 300
    checksum_recipient = Web3.to_checksum_address(recipient_address)

    logger.info(f"Decreasing liquidity for tokenId {token_id} by {liquidity_to_decrease}")
    # DecreaseLiquidityParams: (uint256 tokenId, uint128 liquidity, uint256 amount0Min, uint256 amount1Min, uint256 deadline)
    decrease_params_tuple = (token_id, liquidity_to_decrease, amount0_min, amount1_min, deadline)
    
    try:
        decrease_object_for_logging = None
        try:
            decrease_object_for_logging = nft_manager_contract.functions.decreaseLiquidity(decrease_params_tuple)
        except Exception as e_obj:
            logger.warning(f"Could not create decreaseLiquidity function object for logging: {e_obj}")

        encoded_data_for_decrease = None
        try:
            encoded_data_for_decrease = nft_manager_contract.encodeABI(fn_name="decreaseLiquidity", args=[decrease_params_tuple])
            logger.info(f"Successfully generated encoded_data for 'decreaseLiquidity' via contract.encodeABI().")
        except Exception as e:
            logger.error(f"Critical error: Failed to generate encoded_data for 'decreaseLiquidity': {e}", exc_info=True)
            if decrease_object_for_logging:
                logger.error(f"Type of 'decreaseLiquidity(args)' object was: {type(decrease_object_for_logging)}")
                try: logger.error(f"Attributes: {dir(decrease_object_for_logging)}")
                except: pass
            return False

        final_tx_params_decrease = _estimate_and_build_tx(
            w3, 
            decrease_object_for_logging, 
            wallet_address, 
            nft_manager_contract.address,
            encoded_data_override=encoded_data_for_decrease
        )
        if not final_tx_params_decrease: 
            logger.error(f"Failed to build transaction for decreaseLiquidity on token {token_id}.")
            return False
        
        receipt_decrease = sign_and_send_transaction(w3, final_tx_params_decrease, account.key)
        if not (receipt_decrease and receipt_decrease.status == 1):
            logger.error("Decrease liquidity transaction failed.")
            return False
        logger.info("Decrease liquidity successful.")
    except Exception as e:
        logger.error(f"Error during decreaseLiquidity for tokenId {token_id}: {e}", exc_info=True)
        return False

    MAX_UINT128 = 2**128 - 1 # Max for amount0Max, amount1Max in CollectParams
    logger.info(f"Collecting fees for tokenId {token_id} to {checksum_recipient}")
    # CollectParams: (uint256 tokenId, address recipient, uint128 amount0Max, uint128 amount1Max)
    collect_params_tuple = (token_id, checksum_recipient, MAX_UINT128, MAX_UINT128)
    
    try:
        collect_object_for_logging = None
        try:
            collect_object_for_logging = nft_manager_contract.functions.collect(collect_params_tuple)
        except Exception as e_obj:
            logger.warning(f"Could not create collect function object for logging: {e_obj}")

        encoded_data_for_collect = None
        try:
            encoded_data_for_collect = nft_manager_contract.encodeABI(fn_name="collect", args=[collect_params_tuple])
            logger.info(f"Successfully generated encoded_data for 'collect' via contract.encodeABI().")
        except Exception as e:
            logger.error(f"Critical error: Failed to generate encoded_data for 'collect': {e}", exc_info=True)
            if collect_object_for_logging:
                logger.error(f"Type of 'collect(args)' object was: {type(collect_object_for_logging)}")
                try: logger.error(f"Attributes: {dir(collect_object_for_logging)}")
                except: pass
            return False # Returning False as the overall operation failed at collect step

        final_tx_params_collect = _estimate_and_build_tx(
            w3, 
            collect_object_for_logging, 
            wallet_address, 
            nft_manager_contract.address,
            encoded_data_override=encoded_data_for_collect
        )
        if not final_tx_params_collect: 
            logger.error(f"Failed to build transaction for collect on token {token_id}.")
            return False

        receipt_collect = sign_and_send_transaction(w3, final_tx_params_collect, account.key)
        if not (receipt_collect and receipt_collect.status == 1):
            logger.error("Collect fees transaction failed.")
            return False 
        logger.info("Collect fees successful.")
        return True
    except Exception as e:
        logger.error(f"Error during collect for tokenId {token_id}: {e}", exc_info=True)
        return False

def burn_nft_position(w3, nft_manager_contract, token_id, account, wallet_address): #
    if not nft_manager_contract: return False
    logger.info(f"Attempting to burn NFT tokenId {token_id}")
    
    try:
        burn_object_for_logging = None
        try:
            # burn takes a single uint256 tokenId argument
            burn_object_for_logging = nft_manager_contract.functions.burn(token_id) 
        except Exception as e_obj:
            logger.warning(f"Could not create burn function object for logging: {e_obj}")

        encoded_data_for_burn = None
        try:
            encoded_data_for_burn = nft_manager_contract.encodeABI(fn_name="burn", args=[token_id])
            logger.info(f"Successfully generated encoded_data for 'burn' via contract.encodeABI().")
        except Exception as e:
            logger.error(f"Critical error: Failed to generate encoded_data for 'burn': {e}", exc_info=True)
            if burn_object_for_logging:
                logger.error(f"Type of 'burn(args)' object was: {type(burn_object_for_logging)}")
                try: logger.error(f"Attributes: {dir(burn_object_for_logging)}")
                except: pass
            return False

        final_tx_params = _estimate_and_build_tx(
            w3, 
            burn_object_for_logging, 
            wallet_address, 
            nft_manager_contract.address,
            encoded_data_override=encoded_data_for_burn
        )
        if not final_tx_params: 
            logger.error(f"Failed to build transaction for burn on token {token_id}.")
            return False

        receipt = sign_and_send_transaction(w3, final_tx_params, account.key)
        if receipt and receipt.status == 1:
            logger.info(f"NFT tokenId {token_id} burned successfully.")
            return True
        else:
            logger.error(f"Burn NFT transaction failed for tokenId {token_id}.")
            return False
    except Exception as e:
        logger.error(f"Error during burn_nft_position for tokenId {token_id}: {e}", exc_info=True)
        return False

if __name__ == '__main__':
    # Setup basic logging for standalone script execution
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s][%(filename)s:%(lineno)s] %(message)s")
    logger.info("Running dex_utils.py standalone tests/demonstrations...")

    try:
        w3 = get_web3_provider()
        if not w3: 
            raise ConnectionError("Failed to get Web3 provider from blockchain_utils.py")

        # Use DUMMY_PRIVATE_KEY from config for testing, ensure it's set in .env or config.py
        if not config.DUMMY_PRIVATE_KEY or "0xaaa" in config.DUMMY_PRIVATE_KEY : # Basic check for placeholder
            logger.warning("DUMMY_PRIVATE_KEY in config.py appears to be a placeholder. Standalone tests requiring signing will fail.")
            # Depending on tests, you might want to exit or skip certain parts.
            # For now, just warn and proceed.

        bot_account, bot_wallet_address = get_wallet_credentials(config.DUMMY_PRIVATE_KEY)
        if not bot_account: 
            # get_wallet_credentials now raises ValueError on invalid key, so this might not be hit if that's the case.
            raise ValueError("Failed to get wallet credentials. Ensure DUMMY_PRIVATE_KEY is valid.")

        logger.info(f"Bot Wallet Address for standalone tests: {bot_wallet_address}")
        logger.info(f"ETH Balance: {w3.from_wei(w3.eth.get_balance(bot_wallet_address), 'ether')} ETH")


        # Load contracts (ensure addresses and ABIs are correct in config.py)
        pool_contract = load_contract(w3, config.POOL_ADDRESS, config.MINIMAL_POOL_ABI)
        nft_manager_contract = load_contract(w3, config.NONFUNGIBLE_POSITION_MANAGER_ADDRESS, config.MINIMAL_NONFUNGIBLE_POSITION_MANAGER_ABI)

        if not pool_contract:
            logger.error(f"Failed to load Pool contract at {config.POOL_ADDRESS}")
        if not nft_manager_contract:
            logger.error(f"Failed to load NFT Manager contract at {config.NONFUNGIBLE_POSITION_MANAGER_ADDRESS}")
        
        if not pool_contract or not nft_manager_contract:
            raise ConnectionError("Failed to load one or more critical contracts for testing.")

        # --- Test Read Functions ---
        logger.info("\n--- Testing Read Functions ---")
        pool_details = get_pool_info(pool_contract)
        if pool_details:
            logger.info(f"Retrieved Pool Details: {pool_details}")
        else:
            logger.warning("Could not retrieve pool details.")

        current_pool_state = get_current_pool_state(pool_contract)
        if current_pool_state:
            logger.info(f"Retrieved Current Pool State: Tick={current_pool_state['tick']}, SqrtPriceX96={current_pool_state['sqrtPriceX96']}")
        else:
            logger.warning("Could not retrieve current pool state.")

        owned_token_ids = get_owner_token_ids(nft_manager_contract, bot_wallet_address)
        logger.info(f"Initial Owned Token IDs by {bot_wallet_address}: {owned_token_ids}")

        if owned_token_ids:
            test_token_id_for_details = owned_token_ids[0]
            logger.info(f"Fetching details for first owned token ID: {test_token_id_for_details}")
            position_details = get_position_details(nft_manager_contract, test_token_id_for_details)
            if position_details:
                logger.info(f"Details for Token ID {test_token_id_for_details}: Liquidity={position_details['liquidity']}")
            else:
                logger.warning(f"Could not retrieve details for token ID {test_token_id_for_details}")
        else:
            logger.info("No owned token IDs to test get_position_details with.")

        logger.info("\nStandalone dex_utils.py tests completed.")

    except ConnectionError as ce:
        logger.error(f"Connection error in standalone execution: {ce}")
    except ValueError as ve:
        logger.error(f"Value error in standalone execution: {ve}")
    except Exception as e:
        logger.error(f"An unexpected error occurred in dex_utils.py standalone main block: {e}", exc_info=True)