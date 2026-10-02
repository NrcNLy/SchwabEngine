package com.schwabengine.edge.dashboard.data.local.entity

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "diagnostic_state")
data class DiagnosticEntity(
    @PrimaryKey val id: Int = 1,
    val settledCashBucket1: Double,
    val unsettledProceedsBucket2: Double,
    val pendingAchBucket3: Double,
    val brokerReportedCash: Double,
    val refreshTokenTtlHours: Double,
    val roboRoutingIsolated: Boolean,
    val timestamp: Long
)

@Entity(tableName = "trade_signals")
data class TradeSignalEntity(
    @PrimaryKey val signalId: String,
    val strategy: String,
    val symbol: String,
    val direction: String,
    val entryLimitPrice: Double,
    val calculatedStopLoss: Double,
    val calculatedTakeProfit: Double,
    val targetShareQuantity: Int,
    val grossOrderValue: Double,
    val timestamp: Long
)

@Entity(tableName = "order_lifecycle")
data class OrderEntity(
    @PrimaryKey val brokerOrderId: String,
    val status: String, // WORKING, PARTIALLY_FILLED, FILLED, CANCELED
    val executedQuantity: Int,
    val leavesQuantity: Int,
    val filledPrice: Double,
    val slippage: Double,
    val timestamp: Long
)

@Entity(tableName = "risk_breaches")
data class RiskBreachEntity(
    @PrimaryKey(autoGenerate = true) val id: Int = 0,
    val breachType: String,
    val severity: String,
    val realizedDailyPnl: Double,
    val consecutiveStoppedTrades: Int,
    val mitigationActionsTaken: String,
    val timestamp: Long
)

@Entity(tableName = "macro_state")
data class MacroStateEntity(
    @PrimaryKey val id: Int = 1,
    val regime: String,
    val bias: String,
    val confidence: Double,
    val riskMultiplier: Double,
    val blackoutFlags: String,
    val timestamp: Long
)

@Entity(tableName = "portfolio_sweep")
data class PortfolioSweepEntity(
    @PrimaryKey(autoGenerate = true) val id: Int = 0,
    val sweepType: String,
    val positionsLiquidated: Int,
    val totalSandboxEquity: Double,
    val netDailyRoiPct: Double,
    val eodReconciliation: Boolean,
    val timestamp: Long
)

@Entity(tableName = "llm_thoughts")
data class LlmThoughtEntity(
    @PrimaryKey(autoGenerate = true) val id: Int = 0,
    val note: String,
    val category: String,
    val timestamp: Long
)
