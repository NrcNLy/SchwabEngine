---
trigger: always_on
description: Architectural guidelines for the Schwab Edge Android Command Dashboard
---

# Android Architecture Rules
1. Pure Jetpack Compose with Material 3 styling (dark theme only, no legacy XML layouts).
2. Unidirectional Data Flow (UDF) using Kotlin StateFlow and Room persistence.
3. Type-safe navigation using Navigation Compose 2.8+ with @Serializable routes.
4. No stubbed code or placeholder comments; every file must be fully implemented.
5. All background I/O operations must be dispatched to `Dispatchers.IO`.
