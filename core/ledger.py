"""
core/ledger.py
==============
Event-Driven State Machine Ledger for T+1 Cash Settlement Compliance.

Refactored to allow algorithmic capital velocity utilizing Bucket 2 (Unsettled)
funds without triggering FINRA Good Faith Violations via temporal lock tagging.
"""

from typing import Dict, Optional
import datetime
import logging

logger = logging.getLogger("ledger")

class TradeTranche:
    def __init__(self, ticker: str, capital_utilized: float, source_bucket: str, timestamp_purchased: datetime.datetime):
        self.ticker = ticker
        self.capital_utilized = capital_utilized
        self.source_bucket = source_bucket
        self.timestamp_purchased = timestamp_purchased

class SettlementLedger:
    def __init__(self):
        self.bucket1_settled = 720.0
        self.bucket2_unsettled = 240.0
        self.bucket3_pending = 40.0
        
        self.active_tranches: Dict[str, TradeTranche] = {}

    def allocate_capital(self, ticker: str, amount: float) -> bool:
        """
        Dynamically allocates capital across Bucket 1 and Bucket 2.
        Tags the tranche with the source bucket for temporal tracking.
        """
        now = datetime.datetime.now(datetime.UTC)
        
        if self.bucket1_settled >= amount:
            self.bucket1_settled -= amount
            self.active_tranches[ticker] = TradeTranche(ticker, amount, "BUCKET_1", now)
            logger.info(f"Allocated ${amount} from BUCKET_1 for {ticker}.")
            return True
        elif (self.bucket1_settled + self.bucket2_unsettled) >= amount:
            # Requires dipping into unsettled funds
            deficit = amount - self.bucket1_settled
            self.bucket1_settled = 0.0
            self.bucket2_unsettled -= deficit
            self.active_tranches[ticker] = TradeTranche(ticker, amount, "BUCKET_2", now)
            logger.warning(f"Allocated ${amount} leveraging BUCKET_2 for {ticker}. Temporal lock applied.")
            return True
            
        logger.error(f"Insufficient total capital to allocate ${amount} for {ticker}.")
        return False

    def evaluate_gfv_compliance(self, ticker: str) -> bool:
        """
        Queries the ledger's state to check the specific settlement timestamp.
        If funds have not crossed the T+1 clearing threshold (09:00 EDT),
        the exit order is algorithmically suppressed to prevent a GFV.
        """
        tranche = self.active_tranches.get(ticker)
        if not tranche:
            return True # Not managed by us or already cleared
            
        if tranche.source_bucket == "BUCKET_1":
            return True # Settled funds can be day-traded freely
            
        # Tranche was funded via BUCKET_2 (Unsettled). We must check T+1 clearance.
        now = datetime.datetime.now(datetime.UTC)
        purchase_day = tranche.timestamp_purchased.date()
        
        # Simplistic T+1 check for scaffolding: If it's the next calendar day, assume settled.
        # A true implementation would calculate 09:00 EDT business days.
        if now.date() > purchase_day:
            return True
            
        logger.error(f"GFV PREVENTION: {ticker} exit suppressed. Funds from BUCKET_2 have not cleared T+1.")
        return False
        
    def release_capital(self, ticker: str, amount: float):
        """Returns capital back to Bucket 2 (Unsettled) upon closing a position."""
        if ticker in self.active_tranches:
            del self.active_tranches[ticker]
        self.bucket2_unsettled += amount
        logger.info(f"Released ${amount} to BUCKET_2 from {ticker}.")
        
    def snapshot(self):
        class Snapshot:
            def __init__(self, b1, b2, b3):
                self.bucket1_settled = b1
                self.bucket2_unsettled = b2
                self.bucket3_pending_ach = b3
                self.max_order_value = b1 + b2
        return Snapshot(self.bucket1_settled, self.bucket2_unsettled, self.bucket3_pending)
