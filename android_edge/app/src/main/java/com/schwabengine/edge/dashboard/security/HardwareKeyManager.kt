package com.schwabengine.edge.dashboard.security

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import javax.crypto.KeyGenerator
import javax.crypto.Mac

class HardwareKeyManager {

    companion object {
        private const val KEY_ALIAS = "SchwabEdgeControlKey"
        private const val KEYSTORE_PROVIDER = "AndroidKeyStore"
    }

    init {
        generateKeyIfNotExists()
    }

    private fun generateKeyIfNotExists() {
        val keyStore = KeyStore.getInstance(KEYSTORE_PROVIDER)
        keyStore.load(null)

        if (!keyStore.containsAlias(KEY_ALIAS)) {
            val keyGenerator = KeyGenerator.getInstance(
                KeyProperties.KEY_ALGORITHM_HMAC_SHA256,
                KEYSTORE_PROVIDER
            )
            val keyGenSpec = KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_SIGN
            )
                .setUserAuthenticationRequired(true)
                .setUserAuthenticationParameters(
                    0, // 0 means authentication is required for every use of the key
                    KeyProperties.AUTH_BIOMETRIC_STRONG
                )
                .setInvalidatedByBiometricEnrollment(true)
                .build()

            keyGenerator.init(keyGenSpec)
            keyGenerator.generateKey()
        }
    }

    /**
     * Provides an unauthenticated Mac instance initialized with the hardware key.
     * This Mac must be passed to the BiometricPrompt CryptoObject to be unlocked.
     */
    fun getMacInstance(): Mac {
        val keyStore = KeyStore.getInstance(KEYSTORE_PROVIDER)
        keyStore.load(null)
        val secretKey = keyStore.getKey(KEY_ALIAS, null)

        val mac = Mac.getInstance(KeyProperties.KEY_ALGORITHM_HMAC_SHA256)
        mac.init(secretKey)
        return mac
    }
}
