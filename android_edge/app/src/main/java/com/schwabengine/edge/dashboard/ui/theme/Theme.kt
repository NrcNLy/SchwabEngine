package com.schwabengine.edge.dashboard.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable

private val CyberColorScheme = darkColorScheme(
    background = CyberBlack,
    surface = PanelDark,
    primary = NeonCyan,
    secondary = EmeraldGreen,
    error = CrimsonRed,
    onBackground = TerminalGray,
    onSurface = TerminalGray
)

@Composable
fun SchwabEdgeTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = CyberColorScheme,
        typography = TerminalTypography,
        content = content
    )
}
