plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.hcode91.pocketpear"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.hcode91.pocketpear"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1-prototype"
        ndk { abiFilters += listOf("arm64-v8a") }
    }

    signingConfigs {
        // A fixed key checked into the (private) repo, so each CI build installs over the last.
        create("shared") {
            storeFile = file("pocketpear-debug.keystore")
            storePassword = "pocketpear"
            keyAlias = "pocketpear"
            keyPassword = "pocketpear"
        }
    }
    buildTypes {
        getByName("debug") { signingConfig = signingConfigs.getByName("shared") }
        getByName("release") {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("shared")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    packaging { jniLibs { useLegacyPackaging = true } }
}

chaquopy {
    defaultConfig {
        version = "3.13"
        pip {
            install("numpy")
            install("pillow")
        }
        // The .pyc step needs a host Python of the same version; skip it so any build machine works.
        pyc { src = false }
    }
    sourceSets {
        getByName("main") {
            // The emulator itself is the prototype, shared with the desktop runs.
            srcDir("../../prototype")
            // Unicorn's Python binding, copied in from the Unicorn source by the build (see README).
            srcDir("build/unicorn-python")
        }
    }
}
