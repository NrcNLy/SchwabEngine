package com.schwabengine.edge.dashboard.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.schwabengine.edge.dashboard.data.repository.DashboardSnapshot

@Composable
fun LedgerScreen(
    snapshot: DashboardSnapshot,
    modifier: Modifier = Modifier
) {
    Column(modifier = modifier.fillMaxSize().padding(16.dp)) {
        Text("Compliance & Ledger", style = MaterialTheme.typography.headlineMedium, color = Color.White)
        Spacer(modifier = Modifier.height(16.dp))

        val diagnostic = snapshot.diagnostic

        Text("Broker Reported Cash: \$${String.format("%.2f", diagnostic?.brokerReportedCash ?: 0.0)}", color = Color.White)
        Text("Robo Routing Isolated: ${diagnostic?.roboRoutingIsolated ?: false}", color = Color.White)

        Spacer(modifier = Modifier.height(24.dp))
        Text("IRC §1091 Wash Sale Protection", style = MaterialTheme.typography.titleMedium, color = Color.White)
        Spacer(modifier = Modifier.height(8.dp))

        // Static Mock for Wash Sale Table
        Column(modifier = Modifier.fillMaxWidth().background(Color(0xFF121820)).padding(16.dp)) {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text("SOXL", color = Color(0xFFFF1744))
                Text("SWING_LOCKED", color = Color(0xFFFF1744))
                Text("-> FNGU", color = Color(0xFF00E676))
            }
            Spacer(modifier = Modifier.height(8.dp))
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text("TQQQ", color = Color(0xFFFF1744))
                Text("SWING_LOCKED", color = Color(0xFFFF1744))
                Text("-> CONL", color = Color(0xFF00E676))
            }
            Spacer(modifier = Modifier.height(8.dp))
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text("TNA", color = Color(0xFFFF1744))
                Text("SWING_LOCKED", color = Color(0xFFFF1744))
                Text("-> DPST", color = Color(0xFF00E676))
            }
        }
    }
}
