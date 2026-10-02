package com.schwabengine.edge.dashboard

import android.os.Bundle
import android.widget.Toast
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.*
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.lifecycleScope
import androidx.navigation.compose.*
import com.schwabengine.edge.dashboard.data.local.DashboardDatabase
import com.schwabengine.edge.dashboard.data.network.RemoteDirectiveClient
import com.schwabengine.edge.dashboard.data.repository.DashboardSnapshot
import com.schwabengine.edge.dashboard.data.repository.TradingDashboardRepository
import com.schwabengine.edge.dashboard.security.BiometricDirectiveSigner
import com.schwabengine.edge.dashboard.ui.*
import com.schwabengine.edge.dashboard.ui.screens.*
import com.schwabengine.edge.dashboard.ui.theme.SchwabEdgeTheme
import kotlinx.coroutines.launch

class MainActivity : FragmentActivity() {
    private lateinit var repository: TradingDashboardRepository
    private lateinit var directiveClient: RemoteDirectiveClient
    private lateinit var biometricSigner: BiometricDirectiveSigner

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        
        val dao = DashboardDatabase.getDatabase(this).dashboardDao()
        repository = TradingDashboardRepository(dao)
        directiveClient = RemoteDirectiveClient()
        biometricSigner = BiometricDirectiveSigner(this)

        setContent {
            SchwabEdgeTheme {
                val navController = rememberNavController()
                val snapshot by repository.dashboardState.collectAsState(
                    initial = DashboardSnapshot(null, emptyList(), emptyList(), emptyList(), null, null, emptyList())
                )

                Scaffold(
                    bottomBar = {
                        NavigationBar {
                            NavigationBarItem(
                                selected = false,
                                onClick = { navController.navigate(OverviewRoute) },
                                icon = { Text("OVR") },
                                label = { Text("Overview") }
                            )
                            NavigationBarItem(
                                selected = false,
                                onClick = { navController.navigate(PositionsRoute) },
                                icon = { Text("POS") },
                                label = { Text("Positions") }
                            )
                            NavigationBarItem(
                                selected = false,
                                onClick = { navController.navigate(LedgerRoute) },
                                icon = { Text("LDG") },
                                label = { Text("Ledger") }
                            )
                            NavigationBarItem(
                                selected = false,
                                onClick = { navController.navigate(MacroRoute) },
                                icon = { Text("MAC") },
                                label = { Text("Macro") }
                            )
                        }
                    }
                ) { innerPadding ->
                    NavHost(
                        navController = navController,
                        startDestination = OverviewRoute,
                        modifier = Modifier.padding(innerPadding)
                    ) {
                        composable<OverviewRoute> {
                            OverviewScreen(
                                snapshot = snapshot,
                                onToggleExecution = { 
                                    dispatchDirective(mutableMapOf("command" to "TOGGLE_EXECUTION"), "/api/v1/operator/action")
                                },
                                onEmergencyFlatten = { 
                                    dispatchDirective(mutableMapOf("command" to "KILL_SWITCH_SWEEP", "force_market_liquidation" to "true"), "/api/v1/operator/kill-switch")
                                }
                            )
                        }
                        composable<PositionsRoute> { PositionsScreen(snapshot) }
                        composable<LedgerRoute> { LedgerScreen(snapshot) }
                        composable<MacroRoute> { MacroScreen(snapshot) }
                    }
                }
            }
        }
    }

    private fun dispatchDirective(payload: MutableMap<String, String>, endpoint: String) {
        biometricSigner.signDirective(
            directive = payload,
            onSuccess = { json, signature, nonce ->
                lifecycleScope.launch {
                    val result = directiveClient.sendDirective(endpoint, json, signature, nonce)
                    result.onSuccess {
                        Toast.makeText(this@MainActivity, "Directive Success!", Toast.LENGTH_SHORT).show()
                    }.onFailure {
                        Toast.makeText(this@MainActivity, "Network Error", Toast.LENGTH_SHORT).show()
                    }
                }
            },
            onError = { error ->
                Toast.makeText(this, error, Toast.LENGTH_LONG).show()
            }
        )
    }
}
