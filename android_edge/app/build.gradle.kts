import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.serialization")
    id("com.google.devtools.ksp")
    // FCM Plugin (commented out until google-services.json is available)
    // id("com.google.gms.google-services")
}

// Load properties from local.properties for the Base URL
val localProperties = Properties()
val localPropertiesFile = rootProject.file("local.properties")
if (localPropertiesFile.exists()) {
    localProperties.load(localPropertiesFile.inputStream())
}
val engineBaseUrl: String = localProperties.getProperty("ENGINE_BASE_URL") ?: "\"http://127.0.0.1:8000\""

android {
    namespace = "com.schwabengine.edge.dashboard"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.schwabengine.edge.dashboard"
        minSdk = 28
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"

        // Inject Base URL into BuildConfig
        buildConfigField("String", "ENGINE_BASE_URL", engineBaseUrl)
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.14"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.0")
    implementation("androidx.activity:activity-compose:1.9.0")
    
    // Compose
    implementation(platform("androidx.compose:compose-bom:2024.05.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    
    // Navigation & Serialization
    implementation("androidx.navigation:navigation-compose:2.8.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.3")
    
    // Room Database
    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")
    
    // Firebase Cloud Messaging
    implementation(platform("com.google.firebase:firebase-bom:33.0.0"))
    implementation("com.google.firebase:firebase-messaging-ktx")
    
    // Security & Network
    implementation("androidx.biometric:biometric:1.2.0-alpha05")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}

// Custom task to copy APK to G: Drive
tasks.register<Copy>("copyApkToDrive") {
    dependsOn("assembleDebug")
    from("build/outputs/apk/debug/app-debug.apk")
    into("G:/My Drive/Financial/SchwabEngine/")
}
