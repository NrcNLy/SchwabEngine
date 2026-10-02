package com.schwabengine.edge.dashboard.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.schwabengine.edge.dashboard.data.repository.DashboardSnapshot
import com.schwabengine.edge.dashboard.ui.components.DualTierStopBar

@Composable
fun PositionsScreen(
    snapshot: DashboardSnapshot,
    modifier: Modifier = Modifier
) {
    Column(modifier = modifier.fillMaxSize().padding(16.dp)) {
        Text("Active Positions & Orders", style = MaterialTheme.typography.headlineMedium, color = Color.White)
        Spacer(modifier = Modifier.height(16.dp))

        if (snapshot.recentOrders.isEmpty()) {
            Text("No recent orders.", color = Color.Gray)
        } else {
            LazyColumn(verticalArrangement = Arrangement.spacedBy(16.dp)) {
                items(snapshot.recentOrders) { order ->
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        colors = CardDefaults.cardColors(containerColor = Color(0xFF121820))
                    ) {
                        Column(modifier = Modifier.padding(16.dp)) {
                            Text("Order: ${order.brokerOrderId}", color = Color.White)
                            Text("Status: ${order.status}", color = if (order.status == "FILLED") Color(0xFF00E676) else Color(0xFFFFD600))
                            Text("Filled: ${order.executedQuantity} | Leaves: ${order.leavesQuantity}", color = Color.LightGray)
                            
                            Spacer(modifier = Modifier.height(16.dp))
                            
                            // Mocking matrix data for demonstration
                            DualTierStopBar(
                                entryPrice = order.filledPrice,
                                currentBid = order.filledPrice * 1.01,
                                dynamicStop = order.filledPrice * 0.98,
                                catastropheStop = order.filledPrice * 0.96,
                                targetPrice = order.filledPrice * 1.05
                            )
                        }
                    }
                }
            }
        }
    }
}
