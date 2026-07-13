// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * HoneypotSimulator — measures EXACT buy/sell tax and sellability in one call.
 *
 * This contract is never deployed. Its *runtime* bytecode is injected at a
 * scratch address via `eth_call` `stateOverride.code`, and the caller (a burner
 * funded via `stateOverride.balance`) invokes `simulate` with `value`. Because
 * it runs inside a single eth_call it can buy then sell and compare the actual
 * amounts against tax-free reserve quotes (`getAmountsOut`) — which is the only
 * way to get true token transfer taxes (reserve quotes alone never reveal them).
 *
 * Compile the runtime bytecode and put it in config as
 * `chain.honeypot_simulator_bytecode`:
 *
 *     solc --optimize --bin-runtime --evm-version paris HoneypotSimulator.sol
 *
 * Returns tax in basis points (100 bps = 1%). `ok` is false if the buy yielded
 * nothing or the sell returned no native token (i.e. a honeypot).
 *
 * Router is assumed UniswapV2-compatible (the dominant memecoin DEX shape).
 */

interface IUniV2Router {
    function getAmountsOut(uint256 amountIn, address[] calldata path)
        external view returns (uint256[] memory amounts);

    function swapExactETHForTokensSupportingFeeOnTransferTokens(
        uint256 amountOutMin, address[] calldata path, address to, uint256 deadline
    ) external payable;

    function swapExactTokensForETHSupportingFeeOnTransferTokens(
        uint256 amountIn, uint256 amountOutMin, address[] calldata path, address to, uint256 deadline
    ) external;
}

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
}

contract HoneypotSimulator {
    receive() external payable {}

    function simulate(address token, address router, address weth, uint256 amountInEth)
        external
        payable
        returns (uint256 buyTaxBps, uint256 sellTaxBps, uint256 bought, uint256 soldEth, bool ok)
    {
        // ---- BUY: native -> token ----
        address[] memory buyPath = new address[](2);
        buyPath[0] = weth;
        buyPath[1] = token;

        uint256 expectedTokens = IUniV2Router(router).getAmountsOut(amountInEth, buyPath)[1];

        uint256 balBefore = IERC20(token).balanceOf(address(this));
        IUniV2Router(router).swapExactETHForTokensSupportingFeeOnTransferTokens{value: amountInEth}(
            0, buyPath, address(this), block.timestamp + 1
        );
        bought = IERC20(token).balanceOf(address(this)) - balBefore;

        if (expectedTokens > 0 && bought < expectedTokens) {
            buyTaxBps = ((expectedTokens - bought) * 10000) / expectedTokens;
        }

        // ---- SELL: token -> native ----
        IERC20(token).approve(router, bought);

        address[] memory sellPath = new address[](2);
        sellPath[0] = token;
        sellPath[1] = weth;

        uint256 expectedEth = bought > 0 ? IUniV2Router(router).getAmountsOut(bought, sellPath)[1] : 0;

        uint256 ethBefore = address(this).balance;
        IUniV2Router(router).swapExactTokensForETHSupportingFeeOnTransferTokens(
            bought, 0, sellPath, address(this), block.timestamp + 1
        );
        soldEth = address(this).balance - ethBefore;

        if (expectedEth > 0 && soldEth < expectedEth) {
            sellTaxBps = ((expectedEth - soldEth) * 10000) / expectedEth;
        }

        ok = bought > 0 && soldEth > 0;
    }
}
