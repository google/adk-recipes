import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    kotlin("jvm") version "2.3.20"
    application
}

repositories {
    mavenCentral()
}

// Both adk-kotlin artifacts must resolve to the same version, so they are
// pinned together.
val adkVersion = "1.2.0"

dependencies {
    implementation("com.google.adk:google-adk-kotlin-core:$adkVersion")
    implementation("com.google.adk:google-adk-kotlin-webserver:$adkVersion")

    testImplementation(kotlin("test"))
}

// Target Java 17 bytecode without pinning a toolchain, so the build runs on
// any JDK 17 or later.
java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    jvmToolchain(17)
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

application {
    mainClass.set(
        project.findProperty("mainClass") as? String
            ?: "com.google.adk.samples.agents.llmauditor.MainKt",
    )
}

tasks.test {
    useJUnitPlatform()
    // The test never calls the model; it only needs MODEL_NAME to build the
    // agent graph.
    environment("MODEL_NAME", "gemini-flash-latest")
}

tasks.named<JavaExec>("run") {
    standardInput = System.`in`
}
