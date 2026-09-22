plugins {
    kotlin("jvm") version "2.1.20"
    id("com.google.devtools.ksp") version "2.1.20-2.0.1"
    application
}

repositories {
    mavenCentral()
}

// All three adk-kotlin artifacts must resolve to the same version — the KSP
// processor generates code against core's API — so they are pinned together.
val adkVersion = "0.9.0"

dependencies {
    implementation("com.google.adk:google-adk-kotlin-core:$adkVersion")
    implementation("com.google.adk:google-adk-kotlin-webserver:$adkVersion")
    ksp("com.google.adk:google-adk-kotlin-processor:$adkVersion")
}

kotlin {
    jvmToolchain(17)
}

application {
    mainClass.set(
        project.findProperty("mainClass") as? String
            ?: "com.google.adk.samples.agents.llmauditor.MainKt",
    )
}

tasks.named<JavaExec>("run") {
    standardInput = System.`in`
}
