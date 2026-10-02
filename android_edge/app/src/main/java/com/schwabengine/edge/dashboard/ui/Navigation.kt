package com.schwabengine.edge.dashboard.ui

import kotlinx.serialization.Serializable

@Serializable
object OverviewRoute

@Serializable
object PositionsRoute

@Serializable
object LedgerRoute

@Serializable
object MacroRoute

@Serializable
data class StagedApprovalRoute(val signalId: String)
