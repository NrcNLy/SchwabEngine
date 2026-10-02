package com.schwabengine.edge.dashboard.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp

@Composable
fun MacroBlackoutCard(
    bias: String,
    riskMultiplier: Double,
    activeBlackout: String?,
    modifier: Modifier = Modifier
) {
    val biasColor = when (bias.uppercase()) {
        "BULLISH" -> Color(0xFF00E676)
        "BEARISH" -> Color(0xFFFF1744)
        else -> Color(0xFFFFD600)
    }

    Card(
        modifier = modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = Color(0xFF121820))
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text("Gemini Macro Sentinel", style = MaterialTheme.typography.titleMedium, color = Color.White)
            Spacer(modifier = Modifier.height(12.dp))

            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Column {
                    Text("Market Bias", color = Color.Gray, style = MaterialTheme.typography.labelMedium)
                    Text(bias.uppercase(), color = biasColor, fontWeight = FontWeight.Bold)
                }
                Column {
                    Text("Risk Multiplier", color = Color.Gray, style = MaterialTheme.typography.labelMedium)
                    Text("${String.format("%.2f", riskMultiplier)}x", color = Color.White, fontWeight = FontWeight.Bold)
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            if (activeBlackout != null && activeBlackout.isNotBlank()) {
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(Color(0xFFFF1744).copy(alpha = 0.2f))
                        .padding(8.dp)
                ) {
                    Text(
                        "TEMPORAL LOCKOUT ACTIVE: $activeBlackout",
                        color = Color(0xFFFF1744),
                        fontWeight = FontWeight.Bold,
                        style = MaterialTheme.typography.labelSmall
                    )
                }
            } else {
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(Color(0xFF00E676).copy(alpha = 0.1f))
                        .padding(8.dp)
                ) {
                    Text(
                        "CLEAR: No scheduled macroeconomic events",
                        color = Color(0xFF00E676),
                        style = MaterialTheme.typography.labelSmall
                    )
                }
            }
        }
    }
}
