rootProject.name = "app-renamed"

includeBuild("../library") {
    dependencySubstitution {
        substitute(module("com.example:published-library")).using(project(":"))
    }
}
