package com.schwabengine.edge.dashboard.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import com.schwabengine.edge.dashboard.data.local.entity.LlmThoughtEntity
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@Composable
fun LlmThoughtTerminal(
    thoughts: List<LlmThoughtEntity>,
    modifier: Modifier = Modifier
) {
    val listState = rememberLazyListState()

    LaunchedEffect(thoughts.size) {
        if (thoughts.isNotEmpty()) {
            listState.animateScrollToItem(thoughts.size - 1)
        }
    }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(250.dp)
            .clip(RoundedCornerShape(8.dp))
            .background(Color(0xFF05070A))
            .padding(12.dp)
    ) {
        LazyColumn(state = listState) {
            items(thoughts) { thought ->
                val time = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date(thought.timestamp))
                
                val annotatedString = buildAnnotatedString {
                    withStyle(style = SpanStyle(color = Color(0xFF8F9AAB))) {
                        append("[$time] ")
                    }
                    withStyle(style = SpanStyle(color = Color(0xFF00E5FF), fontWeight = FontWeight.Bold)) {
                        append("[${thought.category}] ")
                    }
                    withStyle(style = SpanStyle(color = Color(0xFF00E676))) {
                        append("> ")
                    }
                    withStyle(style = SpanStyle(color = Color.White)) {
                        append(thought.note)
                    }
                }

                Text(
                    text = annotatedString,
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.padding(bottom = 6.dp)
                )
            }
        }
    }
}
