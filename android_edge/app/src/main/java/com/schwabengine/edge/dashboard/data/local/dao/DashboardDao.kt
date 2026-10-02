package com.schwabengine.edge.dashboard.data.local.dao

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import com.schwabengine.edge.dashboard.data.local.entity.*
import kotlinx.coroutines.flow.Flow

@Dao
interface DashboardDao {

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertDiagnostic(diagnostic: DiagnosticEntity)

    @Query("SELECT * FROM diagnostic_state WHERE id = 1")
    fun getDiagnosticState(): Flow<DiagnosticEntity?>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertTradeSignal(signal: TradeSignalEntity)

    @Query("SELECT * FROM trade_signals ORDER BY timestamp DESC LIMIT 10")
    fun getRecentTradeSignals(): Flow<List<TradeSignalEntity>>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertOrder(order: OrderEntity)

    @Query("SELECT * FROM order_lifecycle ORDER BY timestamp DESC LIMIT 20")
    fun getRecentOrders(): Flow<List<OrderEntity>>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertRiskBreach(breach: RiskBreachEntity)

    @Query("SELECT * FROM risk_breaches ORDER BY timestamp DESC LIMIT 5")
    fun getRecentRiskBreaches(): Flow<List<RiskBreachEntity>>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertMacroState(macroState: MacroStateEntity)

    @Query("SELECT * FROM macro_state WHERE id = 1")
    fun getMacroState(): Flow<MacroStateEntity?>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertPortfolioSweep(sweep: PortfolioSweepEntity)

    @Query("SELECT * FROM portfolio_sweep ORDER BY timestamp DESC LIMIT 1")
    fun getLatestPortfolioSweep(): Flow<PortfolioSweepEntity?>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertLlmThought(thought: LlmThoughtEntity)

    @Query("SELECT * FROM llm_thoughts ORDER BY timestamp ASC LIMIT 50")
    fun getLlmThoughts(): Flow<List<LlmThoughtEntity>>
}
