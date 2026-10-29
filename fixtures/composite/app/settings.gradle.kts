rootProject.name = "app"

includeBuild("../library") {
    dependencySubstitution {
        substitute(module("com.example:library")).using(project(":"))
    }
}
