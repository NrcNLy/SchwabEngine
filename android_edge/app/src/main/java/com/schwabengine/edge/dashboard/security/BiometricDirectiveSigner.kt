package com.schwabengine.edge.dashboard.security

import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.util.concurrent.Executor

class BiometricDirectiveSigner(
    private val activity: FragmentActivity,
    private val hardwareKeyManager: HardwareKeyManager = HardwareKeyManager()
) {

    private val executor: Executor = ContextCompat.getMainExecutor(activity)

    /**
     * Serializes the directive, requests biometric authentication, and returns the signed payload.
     */
    fun signDirective(
        directive: MutableMap<String, String>,
        onSuccess: (String, String, String) -> Unit,
        onError: (String) -> Unit
    ) {
        val nonce = System.currentTimeMillis().toString()
        directive["nonce"] = nonce

        val jsonPayload = Json.encodeToString(
            JsonObject(directive.mapValues { JsonPrimitive(it.value) })
        )

        try {
            val mac = hardwareKeyManager.getMacInstance()
            val cryptoObject = BiometricPrompt.CryptoObject(mac)

            val biometricPrompt = BiometricPrompt(
                activity,
                executor,
                object : BiometricPrompt.AuthenticationCallback() {
                    override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                        super.onAuthenticationError(errorCode, errString)
                        onError("Authentication error: $errString")
                    }

                    override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                        super.onAuthenticationSucceeded(result)
                        result.cryptoObject?.mac?.let { unlockedMac ->
                            val signatureBytes = unlockedMac.doFinal(jsonPayload.toByteArray(Charsets.UTF_8))
                            val signatureHex = signatureBytes.joinToString("") { "%02x".format(it) }
                            onSuccess(jsonPayload, signatureHex, nonce)
                        } ?: onError("Failed to unlock cryptographic primitive.")
                    }

                    override fun onAuthenticationFailed() {
                        super.onAuthenticationFailed()
                        onError("Biometric authentication failed.")
                    }
                }
            )

            val promptInfo = BiometricPrompt.PromptInfo.Builder()
                .setTitle("Authorize Trade Mutation")
                .setSubtitle("Biometric authentication required to sign remote directive")
                .setNegativeButtonText("Cancel")
                .build()

            biometricPrompt.authenticate(promptInfo, cryptoObject)

        } catch (e: Exception) {
            onError("Keystore initialization failed: ${e.message}")
        }
    }
}
