package com.schwabengine.edge.dashboard.ui.components

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.*
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.unit.dp

@Composable
fun DualTierStopBar(
    entryPrice: Double,
    currentBid: Double,
    dynamicStop: Double,
    catastropheStop: Double,
    targetPrice: Double,
    modifier: Modifier = Modifier
) {
    val totalRange = (targetPrice - catastropheStop).coerceAtLeast(0.01)
    
    // Calculate normalized positions (0f to 1f)
    val entryPct = ((entryPrice - catastropheStop) / totalRange).toFloat()
    val bidPct = ((currentBid - catastropheStop) / totalRange).toFloat().coerceIn(0f, 1f)
    val stopPct = ((dynamicStop - catastropheStop) / totalRange).toFloat().coerceIn(0f, 1f)

    Column(modifier = modifier.fillMaxWidth()) {
        Text("Dual-Tier Stop Matrix", style = MaterialTheme.typography.titleSmall, color = Color.White)
        Spacer(modifier = Modifier.height(16.dp))

        Canvas(modifier = Modifier.fillMaxWidth().height(30.dp)) {
            val w = size.width
            val cy = size.height / 2

            // Base track
            drawLine(
                color = Color.DarkGray,
                start = Offset(0f, cy),
                end = Offset(w, cy),
                strokeWidth = 8.dp.toPx(),
                cap = StrokeCap.Round
            )

            // Dynamic Stop to Current Bid fill (Green if profitable, Red if in drawdown)
            val fillStart = w * stopPct
            val fillEnd = w * bidPct
            val fillColor = if (currentBid >= entryPrice) Color(0xFF00E676) else Color(0xFFFF1744)

            drawLine(
                color = fillColor,
                start = Offset(fillStart, cy),
                end = Offset(fillEnd, cy),
                strokeWidth = 8.dp.toPx()
            )

            // Markers
            // 1. Catastrophe Stop (Left)
            drawLine(Color(0xFFFF1744), Offset(0f, 0f), Offset(0f, size.height), strokeWidth = 3.dp.toPx())
            // 2. Entry Price
            drawLine(Color.White, Offset(w * entryPct, 0f), Offset(w * entryPct, size.height), strokeWidth = 2.dp.toPx())
            // 3. Dynamic Stop
            drawCircle(Color(0xFFFFD600), radius = 6.dp.toPx(), center = Offset(fillStart, cy))
            // 4. Current Bid
            drawCircle(Color.Cyan, radius = 8.dp.toPx(), center = Offset(fillEnd, cy))
            // 5. Target Price (Right)
            drawLine(Color(0xFF00E676), Offset(w, 0f), Offset(w, size.height), strokeWidth = 3.dp.toPx())
        }

        Spacer(modifier = Modifier.height(4.dp))
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text("Catast: \$${String.format("%.2f", catastropheStop)}", color = Color(0xFFFF1744), style = MaterialTheme.typography.labelSmall)
            Text("Stop: \$${String.format("%.2f", dynamicStop)}", color = Color(0xFFFFD600), style = MaterialTheme.typography.labelSmall)
            Text("Bid: \$${String.format("%.2f", currentBid)}", color = Color.Cyan, style = MaterialTheme.typography.labelSmall)
            Text("Target: \$${String.format("%.2f", targetPrice)}", color = Color(0xFF00E676), style = MaterialTheme.typography.labelSmall)
        }
    }
}
