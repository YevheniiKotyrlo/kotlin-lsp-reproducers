pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "app"

include(":local")

includeBuild("../library") {
    dependencySubstitution {
        substitute(module("com.example:library")).using(project(":"))
    }
}
