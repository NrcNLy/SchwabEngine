# Schwab Day Trading & Margin Rules Reference Engine
**Effective Date:** July 2026 
**Source:** Schwab Updates Day Trading and Margin Rules

## 1. Core Definitions & Status Changes

### 1.1 Elimination of Pattern Day Trader (PDT) Status
* **Old Rule:** Accounts making 4 or more day trades within 5 business days were flagged as PDT and required a minimum $25,000 equity balance.
* **New Rule (Effective June 8, 2026):** The PDT designation is completely eliminated.
* **Engine Logic Update:** 
    * Disable any rolling 5-day trade counters.
    * Remove the $25,000 minimum equity lock/restriction for accounts flagged as PDT.
    * Do NOT flag or restrict accounts based on the frequency of day trades.

### 1.2 Minimum Account Equity
* **New Rule:** To utilize margin and Intraday Margin Buying Power, an account must maintain a minimum of **$2,000** in cash or eligible securities.

## 2. Margin Buying Power Calculations

The engine must now distinguish between two types of buying power: **Regulation T (Reg T) Buying Power** and **Intraday Margin Buying Power**.

### 2.1 Intraday Margin Buying Power (New as of July 13, 2026)
* **Definition:** The dollar amount a client can trade *during the day* in eligible securities.
* **Calculation/Leverage:** Based on a default **25% maintenance margin requirement** for most stocks.
    * *Max Intraday Leverage:* Up to 4x the account's eligible equity.
* **Dynamic Adjustment:** This value must be calculated in **real-time**. It fluctuates throughout the trading session based on current open positions and real-time asset valuations.
* **Condition:** Trades using this power *must* be opened and closed on the same trading day.

### 2.2 Regulation T (Reg T) Overnight Buying Power
* **Definition:** The traditional buying power used for securities held overnight.
* **Calculation/Leverage:** Generally limited to borrowing up to **50%** of the value of marginable securities.
    * *Max Overnight Leverage:* 2x the account's eligible equity.

## 3. Position and Time Constraints

### 3.1 End-of-Day Liquidation Rules
* **Rule:** If a trader uses Intraday Margin Buying Power (25% requirement) to open positions that exceed their standard Reg T overnight buying power (50% requirement), the excess positions **must be exited by the end of the trading day**.
* **Cutoff Time:** 8:00 p.m. ET.
* **Engine Enforcement:** 
    * The engine should calculate the delta between the total open position value and the Reg T overnight limit.
    * Automated alerts or forced liquidation protocols should be triggered prior to the 8:00 p.m. ET cutoff if the account exceeds overnight limits.
    * Failure to close these positions will result in a margin call.

## 4. Risk and Compliance Monitoring

### 4.1 Real-Time Margin Monitoring
* **Schwab Implementation:** Schwab has opted for real-time monitoring rather than a single end-of-day check.
* **Engine Requirement:** The engine must constantly poll or calculate the real-time margin deficit.
* **Trade Blocking:** The system must actively **block** any new trade requests that would create or increase an intraday margin deficit.

## 5. Summary Checklist for Engine Architecture
- [ ] Deprecate 5-day/4-trade PDT counters.
- [ ] Deprecate $25k PDT minimum balance locks.
- [ ] Implement a $2,000 minimum equity check for margin eligibility.
- [ ] Implement dual buying-power tracking: Intraday (up to 4x / 25% req) and Overnight/Reg T (up to 2x / 50% req).
- [ ] Build a real-time calculator to constantly update Intraday Buying Power based on tick data of open positions.
- [ ] Implement pre-trade validation to block orders that exceed real-time Intraday Buying Power.
- [ ] Implement an end-of-day sequence (prior to 8:00 p.m. ET) to identify, alert on, or auto-liquidate positions exceeding standard Reg T overnight limits.