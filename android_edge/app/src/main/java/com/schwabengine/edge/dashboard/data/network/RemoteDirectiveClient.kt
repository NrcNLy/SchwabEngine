package com.schwabengine.edge.dashboard.data.network

import com.schwabengine.edge.dashboard.BuildConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException

class RemoteDirectiveClient {

    private val client = OkHttpClient()
    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    suspend fun sendDirective(
        endpoint: String, // e.g. "/api/v1/operator/action"
        jsonPayload: String,
        signature: String,
        nonce: String
    ): Result<String> = withContext(Dispatchers.IO) {
        try {
            // Trim quotes from BuildConfig.ENGINE_BASE_URL if present
            val baseUrl = BuildConfig.ENGINE_BASE_URL.removeSurrounding("\"")
            val url = "$baseUrl$endpoint"

            val requestBody = jsonPayload.toRequestBody(jsonMediaType)

            val request = Request.Builder()
                .url(url)
                .post(requestBody)
                .addHeader("X-Signature", signature)
                .addHeader("X-Timestamp", nonce)
                .build()

            client.newCall(request).execute().use { response ->
                if (response.isSuccessful) {
                    Result.success(response.body?.string() ?: "Success")
                } else {
                    Result.failure(IOException("Unexpected code $response"))
                }
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
}
