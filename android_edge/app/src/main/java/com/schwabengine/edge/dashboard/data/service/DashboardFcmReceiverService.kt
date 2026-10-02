package com.schwabengine.edge.dashboard.data.service

import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import com.schwabengine.edge.dashboard.data.local.DashboardDatabase
import com.schwabengine.edge.dashboard.data.local.entity.*
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class DashboardFcmReceiverService : FirebaseMessagingService() {

    private val job = SupervisorJob()
    private val scope = CoroutineScope(Dispatchers.IO + job)

    override fun onMessageReceived(message: RemoteMessage) {
        super.onMessageReceived(message)

        val data = message.data
        val eventType = data["event_type"] ?: return
        val timestamp = System.currentTimeMillis()

        val dao = DashboardDatabase.getDatabase(applicationContext).dashboardDao()

        scope.launch {
            when (eventType) {
                "PRE_MARKET_DIAGNOSTIC" -> {
                    dao.insertDiagnostic(
                        DiagnosticEntity(
                            settledCashBucket1 = data["settled_cash_bucket_1"]?.toDoubleOrNull() ?: 0.0,
                            unsettledProceedsBucket2 = data["unsettled_proceeds_bucket_2"]?.toDoubleOrNull() ?: 0.0,
                            pendingAchBucket3 = data["pending_ach_bucket_3"]?.toDoubleOrNull() ?: 0.0,
                            brokerReportedCash = data["broker_reported_cash"]?.toDoubleOrNull() ?: 0.0,
                            refreshTokenTtlHours = data["refresh_token_ttl_hours"]?.toDoubleOrNull() ?: 0.0,
                            roboRoutingIsolated = data["robo_routing_isolated"]?.toBoolean() ?: false,
                            timestamp = timestamp
                        )
                    )
                }
                "SIGNAL_GENERATED" -> {
                    val signalId = data["signal_id"] ?: return@launch
                    dao.insertTradeSignal(
                        TradeSignalEntity(
                            signalId = signalId,
                            strategy = data["strategy"] ?: "UNKNOWN",
                            symbol = data["symbol"] ?: "",
                            direction = data["direction"] ?: "",
                            entryLimitPrice = data["entry_limit_price"]?.toDoubleOrNull() ?: 0.0,
                            calculatedStopLoss = data["calculated_stop_loss"]?.toDoubleOrNull() ?: 0.0,
                            calculatedTakeProfit = data["calculated_take_profit"]?.toDoubleOrNull() ?: 0.0,
                            targetShareQuantity = data["target_share_quantity"]?.toIntOrNull() ?: 0,
                            grossOrderValue = data["gross_order_value"]?.toDoubleOrNull() ?: 0.0,
                            timestamp = timestamp
                        )
                    )
                }
                "ORDER_LIFECYCLE" -> {
                    val brokerOrderId = data["broker_order_id"] ?: return@launch
                    dao.insertOrder(
                        OrderEntity(
                            brokerOrderId = brokerOrderId,
                            status = data["order_status"] ?: "UNKNOWN",
                            executedQuantity = data["executed_quantity"]?.toIntOrNull() ?: 0,
                            leavesQuantity = data["leaves_quantity"]?.toIntOrNull() ?: 0,
                            filledPrice = data["filled_price"]?.toDoubleOrNull() ?: 0.0,
                            slippage = data["slippage"]?.toDoubleOrNull() ?: 0.0,
                            timestamp = timestamp
                        )
                    )
                }
                "RISK_BREACH" -> {
                    dao.insertRiskBreach(
                        RiskBreachEntity(
                            breachType = data["breach_type"] ?: "UNKNOWN",
                            severity = data["severity"] ?: "WARNING",
                            realizedDailyPnl = data["realized_daily_pnl"]?.toDoubleOrNull() ?: 0.0,
                            consecutiveStoppedTrades = data["consecutive_stopped_trades"]?.toIntOrNull() ?: 0,
                            mitigationActionsTaken = data["mitigation_actions_taken"] ?: "",
                            timestamp = timestamp
                        )
                    )
                }
                "PORTFOLIO_SWEEP" -> {
                    dao.insertPortfolioSweep(
                        PortfolioSweepEntity(
                            sweepType = data["sweep_type"] ?: "EOD",
                            positionsLiquidated = data["positions_liquidated"]?.toIntOrNull() ?: 0,
                            totalSandboxEquity = data["total_sandbox_equity"]?.toDoubleOrNull() ?: 0.0,
                            netDailyRoiPct = data["net_daily_roi_pct"]?.toDoubleOrNull() ?: 0.0,
                            eodReconciliation = data["eod_reconciliation"]?.toBoolean() ?: false,
                            timestamp = timestamp
                        )
                    )
                }
                "LLM_INSIGHT" -> {
                    dao.insertLlmThought(
                        LlmThoughtEntity(
                            note = data["note"] ?: "",
                            category = data["category"] ?: "LOG",
                            timestamp = timestamp
                        )
                    )
                }
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        job.cancel()
    }
}
