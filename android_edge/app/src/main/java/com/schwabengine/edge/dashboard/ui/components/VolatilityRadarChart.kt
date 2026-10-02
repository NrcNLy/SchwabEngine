package com.schwabengine.edge.dashboard.ui.components

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.Fill
import androidx.compose.ui.unit.dp
import kotlin.math.cos
import kotlin.math.sin

@Composable
fun VolatilityRadarChart(
    vix: Float,
    natr: Float,
    rvol: Float,
    momentum: Float,
    sentiment: Float,
    modifier: Modifier = Modifier
) {
    Box(
        modifier = modifier.fillMaxWidth().height(200.dp),
        contentAlignment = Alignment.Center
    ) {
        Canvas(modifier = Modifier.fillMaxWidth().height(200.dp)) {
            val radius = size.height / 2f - 20.dp.toPx()
            val center = Offset(size.width / 2f, size.height / 2f)
            val sides = 5
            val angleStep = (2 * Math.PI) / sides

            // Draw Web Grid
            for (level in 1..4) {
                val r = radius * (level / 4f)
                val gridPath = Path()
                for (i in 0 until sides) {
                    val angle = i * angleStep - Math.PI / 2
                    val x = center.x + r * cos(angle).toFloat()
                    val y = center.y + r * sin(angle).toFloat()
                    if (i == 0) gridPath.moveTo(x, y) else gridPath.lineTo(x, y)
                }
                gridPath.close()
                drawPath(gridPath, color = Color(0xFF1A212D), style = Stroke(width = 2.dp.toPx()))
            }

            // Draw Axes
            for (i in 0 until sides) {
                val angle = i * angleStep - Math.PI / 2
                val x = center.x + radius * cos(angle).toFloat()
                val y = center.y + radius * sin(angle).toFloat()
                drawLine(color = Color(0xFF1A212D), start = center, end = Offset(x, y), strokeWidth = 2.dp.toPx())
            }

            // Draw Data Polygon
            val data = listOf(
                vix.coerceIn(0f, 1f),
                natr.coerceIn(0f, 1f),
                rvol.coerceIn(0f, 1f),
                momentum.coerceIn(0f, 1f),
                sentiment.coerceIn(0f, 1f)
            )

            val dataPath = Path()
            for (i in 0 until sides) {
                val angle = i * angleStep - Math.PI / 2
                val r = radius * data[i]
                val x = center.x + r * cos(angle).toFloat()
                val y = center.y + r * sin(angle).toFloat()
                if (i == 0) dataPath.moveTo(x, y) else dataPath.lineTo(x, y)
            }
            dataPath.close()

            drawPath(dataPath, color = Color(0xFF00E5FF).copy(alpha = 0.3f), style = Fill)
            drawPath(dataPath, color = Color(0xFF00E5FF), style = Stroke(width = 3.dp.toPx()))
        }
    }
}
