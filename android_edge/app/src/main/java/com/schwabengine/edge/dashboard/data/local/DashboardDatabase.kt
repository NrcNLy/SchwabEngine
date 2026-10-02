package com.schwabengine.edge.dashboard.data.local

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import com.schwabengine.edge.dashboard.data.local.dao.DashboardDao
import com.schwabengine.edge.dashboard.data.local.entity.*

@Database(
    entities = [
        DiagnosticEntity::class,
        TradeSignalEntity::class,
        OrderEntity::class,
        RiskBreachEntity::class,
        MacroStateEntity::class,
        PortfolioSweepEntity::class,
        LlmThoughtEntity::class
    ],
    version = 2,
    exportSchema = false
)
abstract class DashboardDatabase : RoomDatabase() {
    abstract fun dashboardDao(): DashboardDao

    companion object {
        @Volatile
        private var INSTANCE: DashboardDatabase? = null

        fun getDatabase(context: Context): DashboardDatabase {
            return INSTANCE ?: synchronized(this) {
                val instance = Room.databaseBuilder(
                    context.applicationContext,
                    DashboardDatabase::class.java,
                    "dashboard_database"
                )
                .fallbackToDestructiveMigration()
                .setJournalMode(JournalMode.WRITE_AHEAD_LOGGING)
                .build()
                INSTANCE = instance
                instance
            }
        }
    }
}
