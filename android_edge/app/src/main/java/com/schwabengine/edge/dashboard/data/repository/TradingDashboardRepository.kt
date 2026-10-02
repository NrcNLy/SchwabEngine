package com.schwabengine.edge.dashboard.data.repository

import com.schwabengine.edge.dashboard.data.local.dao.DashboardDao
import com.schwabengine.edge.dashboard.data.local.entity.*
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.combine

data class DashboardSnapshot(
    val diagnostic: DiagnosticEntity?,
    val recentSignals: List<TradeSignalEntity>,
    val recentOrders: List<OrderEntity>,
    val recentBreaches: List<RiskBreachEntity>,
    val macroState: MacroStateEntity?,
    val latestSweep: PortfolioSweepEntity?,
    val llmThoughts: List<LlmThoughtEntity>
)

class TradingDashboardRepository(private val dao: DashboardDao) {

    private val flow1 = combine(
        dao.getDiagnosticState(),
        dao.getRecentTradeSignals(),
        dao.getRecentOrders()
    ) { a, b, c -> Triple(a, b, c) }

    private val flow2 = combine(
        dao.getRecentRiskBreaches(),
        dao.getMacroState(),
        dao.getLatestPortfolioSweep()
    ) { a, b, c -> Triple(a, b, c) }

    val dashboardState: Flow<DashboardSnapshot> = combine(
        flow1,
        flow2,
        dao.getLlmThoughts()
    ) { f1, f2, thoughts ->
        DashboardSnapshot(
            diagnostic = f1.first,
            recentSignals = f1.second,
            recentOrders = f1.third,
            recentBreaches = f2.first,
            macroState = f2.second,
            latestSweep = f2.third,
            llmThoughts = thoughts
        )
    }
}
