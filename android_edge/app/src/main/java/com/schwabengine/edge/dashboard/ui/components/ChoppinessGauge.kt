package com.schwabengine.edge.dashboard.ui.components

import androidx.compose.animation.core.LinearOutSlowInEasing
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.size
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlin.math.cos
import kotlin.math.sin

@Composable
fun ChoppinessGauge(
    ciValue: Float,
    vwapSlope: Double,
    rvol: Double,
    modifier: Modifier = Modifier
) {
    val animatedCi by animateFloatAsState(
        targetValue = ciValue,
        animationSpec = tween(durationMillis = 1000, easing = LinearOutSlowInEasing),
        label = "ci_needle_animation"
    )

    Box(
        modifier = modifier.size(220.dp, 160.dp),
        contentAlignment = Alignment.BottomCenter
    ) {
        Canvas(modifier = Modifier.size(220.dp, 220.dp)) {
            val strokeWidth = 24.dp.toPx()
            val startAngle = 150f
            val sweepAngle = 240f

            // Emerald Green (#00E676) -> Trend: 0 to 38.2
            drawArc(
                color = Color(0xFF00E676),
                startAngle = startAngle,
                sweepAngle = sweepAngle * 0.382f,
                useCenter = false,
                style = Stroke(width = strokeWidth, cap = StrokeCap.Round),
                size = Size(size.width, size.height)
            )

            // Amber/Yellow (#FFD600) -> Mean-Reversion: 38.2 to 61.8
            drawArc(
                color = Color(0xFFFFD600),
                startAngle = startAngle + (sweepAngle * 0.382f),
                sweepAngle = sweepAngle * (0.618f - 0.382f),
                useCenter = false,
                style = Stroke(width = strokeWidth),
                size = Size(size.width, size.height)
            )

            // Crimson Red (#FF1744) -> Chop: 61.8 to 100
            drawArc(
                color = Color(0xFFFF1744),
                startAngle = startAngle + (sweepAngle * 0.618f),
                sweepAngle = sweepAngle * (1f - 0.618f),
                useCenter = false,
                style = Stroke(width = strokeWidth, cap = StrokeCap.Round),
                size = Size(size.width, size.height)
            )

            // Needle
            val needleAngleDeg = startAngle + (animatedCi / 100f) * sweepAngle
            val needleAngleRad = Math.toRadians(needleAngleDeg.toDouble())
            
            val radius = size.width / 2
            val needleLength = radius - strokeWidth - 10.dp.toPx()
            
            val center = Offset(radius, radius)
            val needleEnd = Offset(
                x = center.x + (needleLength * cos(needleAngleRad)).toFloat(),
                y = center.y + (needleLength * sin(needleAngleRad)).toFloat()
            )

            drawLine(
                color = Color.White,
                start = center,
                end = needleEnd,
                strokeWidth = 6.dp.toPx(),
                cap = StrokeCap.Round
            )
            drawCircle(
                color = Color.White,
                radius = 12.dp.toPx(),
                center = center
            )
        }

        val regime = when {
            ciValue < 38.2f -> "TREND"
            ciValue <= 61.8f -> "COMPRESSION"
            else -> "CHOP"
        }

        val regimeColor = when {
            ciValue < 38.2f -> Color(0xFF00E676)
            ciValue <= 61.8f -> Color(0xFFFFD600)
            else -> Color(0xFFFF1744)
        }

        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(
                text = regime,
                color = regimeColor,
                fontWeight = FontWeight.Bold,
                style = MaterialTheme.typography.titleLarge
            )
            Text(
                text = "CI: ${String.format("%.1f", ciValue)} | VWAP: ${String.format("%.3f", vwapSlope)} | RVOL: ${String.format("%.1f", rvol)}",
                color = Color.LightGray,
                style = MaterialTheme.typography.bodyMedium
            )
        }
    }
}
