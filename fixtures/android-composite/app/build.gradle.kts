import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application") version "8.12.0"
    id("com.android.library") version "8.12.0" apply false
    kotlin("android") version "2.1.20"
}

android {
    namespace = "com.example.app"
    compileSdk = 36
    defaultConfig {
        applicationId = "com.example.app"
        minSdk = 24
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    implementation("com.example:library:1.0")
    implementation(project(":local"))
}
