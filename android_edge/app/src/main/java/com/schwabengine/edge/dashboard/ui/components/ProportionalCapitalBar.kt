package com.schwabengine.edge.dashboard.ui.components

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.*
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

@Composable
fun ProportionalCapitalBar(
    bucket1Settled: Double,
    bucket2Unsettled: Double,
    bucket3PendingAch: Double,
    modifier: Modifier = Modifier
) {
    val total = bucket1Settled + bucket2Unsettled + bucket3PendingAch
    val b1Weight = if (total > 0) (bucket1Settled / total).toFloat() else 0f
    val b2Weight = if (total > 0) (bucket2Unsettled / total).toFloat() else 0f

    val colorB1 = Color(0xFF00E676)
    val colorB2 = Color(0xFFFFAB00)
    val colorB3 = Color(0xFF546E7A)

    Column(modifier = modifier.fillMaxWidth()) {
        Text("Capital Ledger Allocation", style = MaterialTheme.typography.titleMedium, color = Color.White)
        Spacer(modifier = Modifier.height(8.dp))
        
        Canvas(modifier = Modifier.fillMaxWidth().height(24.dp)) {
            val w1 = size.width * b1Weight
            val w2 = size.width * b2Weight
            val w3 = size.width - w1 - w2

            if (w1 > 0) {
                drawRoundRect(
                    color = colorB1,
                    topLeft = Offset(0f, 0f),
                    size = Size(w1, size.height),
                    cornerRadius = CornerRadius(8.dp.toPx(), 8.dp.toPx())
                )
            }
            if (w2 > 0) {
                drawRoundRect(
                    color = colorB2,
                    topLeft = Offset(w1, 0f),
                    size = Size(w2, size.height),
                    cornerRadius = if (w1 == 0f && w3 == 0f) CornerRadius(8.dp.toPx(), 8.dp.toPx()) else CornerRadius.Zero
                )
            }
            if (w3 > 0) {
                drawRoundRect(
                    color = colorB3,
                    topLeft = Offset(w1 + w2, 0f),
                    size = Size(w3, size.height),
                    cornerRadius = CornerRadius(8.dp.toPx(), 8.dp.toPx())
                )
            }
        }

        Spacer(modifier = Modifier.height(8.dp))
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Column {
                Text("Settled", color = colorB1, style = MaterialTheme.typography.bodySmall)
                Text("\$${String.format("%.2f", bucket1Settled)}", color = Color.White)
            }
            Column {
                Text("Unsettled (T+1)", color = colorB2, style = MaterialTheme.typography.bodySmall)
                Text("\$${String.format("%.2f", bucket2Unsettled)}", color = Color.White)
            }
            Column {
                Text("Pending ACH", color = colorB3, style = MaterialTheme.typography.bodySmall)
                Text("\$${String.format("%.2f", bucket3PendingAch)}", color = Color.White)
            }
        }
    }
}
