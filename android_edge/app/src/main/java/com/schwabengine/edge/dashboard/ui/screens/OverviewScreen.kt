package com.schwabengine.edge.dashboard.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.schwabengine.edge.dashboard.data.repository.DashboardSnapshot
import com.schwabengine.edge.dashboard.ui.components.ChoppinessGauge
import com.schwabengine.edge.dashboard.ui.components.ProportionalCapitalBar
import com.schwabengine.edge.dashboard.ui.components.SwipeToConfirm

@Composable
fun OverviewScreen(
    snapshot: DashboardSnapshot,
    onToggleExecution: () -> Unit,
    onEmergencyFlatten: () -> Unit,
    modifier: Modifier = Modifier
) {
    val scrollState = rememberScrollState()

    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(16.dp)
            .verticalScroll(scrollState),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text("Schwab Engine Overview", style = MaterialTheme.typography.headlineMedium, color = Color.White)
        Spacer(modifier = Modifier.height(24.dp))

        ChoppinessGauge(
            ciValue = 42.5f,
            vwapSlope = 0.015,
            rvol = 1.2
        )

        Spacer(modifier = Modifier.height(32.dp))

        val diagnostic = snapshot.diagnostic
        ProportionalCapitalBar(
            bucket1Settled = diagnostic?.settledCashBucket1 ?: 1000.0,
            bucket2Unsettled = diagnostic?.unsettledProceedsBucket2 ?: 0.0,
            bucket3PendingAch = diagnostic?.pendingAchBucket3 ?: 0.0
        )

        Spacer(modifier = Modifier.height(48.dp))

        // High Friction Action Gating
        SwipeToConfirm(
            onConfirm = onToggleExecution,
            text = "SWIPE TO TOGGLE EXECUTION >>>",
            thumbColor = Color(0xFF00E5FF)
        )
        
        Spacer(modifier = Modifier.height(16.dp))

        SwipeToConfirm(
            onConfirm = onEmergencyFlatten,
            text = "SWIPE TO EMERGENCY FLATTEN >>>",
            thumbColor = Color(0xFFFF1744)
        )
    }
}
