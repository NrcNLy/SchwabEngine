package com.schwabengine.edge.dashboard.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.schwabengine.edge.dashboard.data.local.entity.LlmThoughtEntity
import com.schwabengine.edge.dashboard.data.repository.DashboardSnapshot
import com.schwabengine.edge.dashboard.ui.components.LlmThoughtTerminal
import com.schwabengine.edge.dashboard.ui.components.MacroBlackoutCard
import com.schwabengine.edge.dashboard.ui.components.VolatilityRadarChart

@Composable
fun MacroScreen(
    snapshot: DashboardSnapshot,
    modifier: Modifier = Modifier
) {
    Column(modifier = modifier.fillMaxSize().padding(16.dp)) {
        Text("Macroeconomic Sentinel", style = MaterialTheme.typography.headlineMedium, color = Color.White)
        Spacer(modifier = Modifier.height(16.dp))

        val macro = snapshot.macroState

        MacroBlackoutCard(
            bias = macro?.bias ?: "NEUTRAL",
            riskMultiplier = macro?.riskMultiplier ?: 1.0,
            activeBlackout = macro?.blackoutFlags ?: ""
        )

        Spacer(modifier = Modifier.height(16.dp))
        
        Text("AI Volatility Radar", style = MaterialTheme.typography.titleMedium, color = Color.White)
        VolatilityRadarChart(
            vix = 0.7f,
            natr = 0.5f,
            rvol = 0.8f,
            momentum = 0.4f,
            sentiment = 0.9f
        )

        Spacer(modifier = Modifier.height(16.dp))
        Text("Strategic Thought Stream", style = MaterialTheme.typography.titleMedium, color = Color.White)
        Spacer(modifier = Modifier.height(8.dp))

        // If no thoughts, provide mock data for the visual
        val thoughts = if (snapshot.llmThoughts.isEmpty()) {
            listOf(
                LlmThoughtEntity(note = "Booting macro assessment layer...", category = "SYS", timestamp = System.currentTimeMillis() - 10000),
                LlmThoughtEntity(note = "Scanning breaking news. Sentiment highly bullish.", category = "NLP", timestamp = System.currentTimeMillis() - 5000),
                LlmThoughtEntity(note = "VIX stabilizing. Risk multiplier increased to 1.5x.", category = "VOLATILITY", timestamp = System.currentTimeMillis())
            )
        } else {
            snapshot.llmThoughts
        }

        LlmThoughtTerminal(thoughts = thoughts)
    }
}
