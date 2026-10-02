package com.schwabengine.edge.dashboard.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.Orientation
import androidx.compose.foundation.gestures.draggable
import androidx.compose.foundation.gestures.rememberDraggableState
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt

@Composable
fun SwipeToConfirm(
    onConfirm: () -> Unit,
    text: String = "SWIPE TO CONFIRM >>>",
    trackColor: Color = Color(0xFF121820),
    thumbColor: Color = Color(0xFF00E5FF),
    modifier: Modifier = Modifier
) {
    var widthPx by remember { mutableFloatStateOf(0f) }
    var thumbOffset by remember { mutableFloatStateOf(0f) }
    val density = LocalDensity.current
    val thumbSize = 56.dp
    val thumbSizePx = with(density) { thumbSize.toPx() }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(64.dp)
            .clip(RoundedCornerShape(32.dp))
            .background(trackColor)
            .onGloballyPositioned { widthPx = it.size.width.toFloat() },
        contentAlignment = Alignment.CenterStart
    ) {
        // Track Text
        Text(
            text = text,
            color = Color.Gray,
            style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold),
            modifier = Modifier.align(Alignment.Center)
        )

        // Thumb
        Box(
            modifier = Modifier
                .offset { IntOffset(thumbOffset.roundToInt(), 0) }
                .padding(4.dp)
                .size(thumbSize)
                .clip(CircleShape)
                .background(thumbColor)
                .draggable(
                    orientation = Orientation.Horizontal,
                    state = rememberDraggableState { delta ->
                        val newOffset = thumbOffset + delta
                        thumbOffset = newOffset.coerceIn(0f, widthPx - thumbSizePx - 8.dp.value * density.density)
                    },
                    onDragStopped = {
                        if (thumbOffset >= widthPx - thumbSizePx - 20.dp.value * density.density) {
                            onConfirm()
                        }
                        thumbOffset = 0f
                    }
                )
        ) {
            Text(">>", color = Color.Black, fontWeight = FontWeight.Bold, modifier = Modifier.align(Alignment.Center))
        }
    }
}
