# Recipe Agents (Java)

> [!IMPORTANT]
> **This folder is retired.** `java/agents/` no longer accepts new
> recipes, or changes to the recipes already here. Recipes now live in
> **`contrib/java/`**.
>
> - **Contributing a new recipe?** Start with the
>   [recipe checklist](../docs/recipe-checklist.md).
> - **Already have a recipe here?** Move it to `contrib/java/<name>` and
>   follow the same checklist. See the
>   [contributor guide](../docs/README.md).
>
> Pull requests that add or modify files under `java/agents/` fail CI.

This folder contains Java recipes for the [Agent Development Kit](https://github.com/google/adk-java) (ADK).


Each folder in this directory contains a different recipe.


## Getting Started


1.  **Prerequisites:**
   *   Ensure you have installed and configured the Agent Development Kit (ADK). See the [ADK Quickstart Guide](https://google.github.io/adk-docs/get-started/quickstart/) for general ADK setup. Specific Java ADK setup might be required.
   *   Java Development Kit (JDK) 17+ installed.
   *   [Apache Maven](https://maven.apache.org/install.html) or [Gradle](https://gradle.org/install/) build tool installed.
   *   Access to Google Cloud (Vertex AI, BigQuery, etc.) and/or a Gemini API Key (depending on the agent - see individual agent READMEs).


2.  **Running a Recipe Agent:**
   *   Navigate to the specific agent's directory (e.g., `cd agents/forecasting-agent`).
   *   Copy any example configuration files (e.g., `config.example.properties` to `config.properties`) and fill in the required environment variables or properties (API keys, project IDs, etc.). See the agent's specific README for details on required variables.
   *   Install dependencies and build the project using Maven or Gradle:
       *   For Maven: `mvn clean install`
       *   For Gradle: `gradle build`
   *   Follow the instructions in the agent's `README.md` to run it (e.g., using `java -jar target/<agent-name>.jar`, `mvn exec:java`, `gradle run`, or via the ADK Dev UI).


## Agent Categories


Check out the Java recipes below, organized by category:


| Agent Name                                                      | Use Case                                                                                                                                         | Tags                                                                                | Interaction Type        | Complexity   | Agent Type   | Vertical                           |
| :-------------------------------------------------------------- | :----------------------------------------------------------------------------------------------------------------------------------------------- | :---------------------------------------------------------------------------------- | :---------------------- | :----------- | :----------- | :--------------------------------- |
| [Software Bug Assistant](agents/software-bug-assistant)         | Assists in software bug resolution by querying internal ticketing systems and external knowledge sources to find similar issues and diagnostics. | Java, RAG, MCP, Bug Tracking, Google Search, IT Support, Database Integration, API  | Workflow/Conversational | Intermediate | Single Agent | Horizontal / IT Support            |
| [Time Series Forecasting Agent](agents/time-series-forecasting) | Automates time-series forecasting using BigQuery ML, providing natural language explanations of predictions for business decisions.              | Java, Forecasting, BigQuery, BQML, Time Series Analysis, Business Intelligence, LLM | Conversational          | Intermediate | Single Agent | Horizontal / Business Intelligence |


## Using the Agents in this Repository


This section provides general guidance on how to run, test, evaluate, and potentially deploy the Java recipes found in this repository. While the core steps are similar, **each agent has its own specific requirements and detailed instructions within its dedicated `README.md` file.**


**Always consult the `README.md` inside the specific agent's directory (e.g., `java/agents/software-bug-assistant/README.md`) for the most accurate and detailed steps.**


Here's a general workflow you can expect:


1.  **Choose an Agent:** Select an agent from the table above that aligns with your interests or use case.
2.  **Navigate to the Agent Directory:** Open your terminal and change into the agent's main directory:
   ```bash
   cd java/agents/<agent-name>
   # Example: cd java/agents/software-bug-assistant
   ```
3.  **Review the Agent's README:** **This is the most crucial step.** Open the `README.md` file within this directory. It will contain:
   *   A detailed overview of the agent's purpose and architecture.
   *   Specific prerequisites (e.g., API keys, cloud services, database setup, Java version).
   *   Step-by-step setup and installation instructions (Maven/Gradle commands).
   *   Commands for running the agent locally.
   *   Instructions for running tests (e.g., using JUnit).
   *   Steps for deployment.


4.  **Setup and Configuration:**
   *   **Prerequisites:** Ensure you've met the general prerequisites listed in the main "Getting Started" section *and* any specific prerequisites mentioned in the agent's README.
   *   **Dependencies:** Build the project and download dependencies using Maven or Gradle (this command is usually run from the agent's main directory):
       *   Maven: `mvn clean install`
       *   Gradle: `gradle build`
   *   **Environment Variables/Configuration Files:** Most agents require configuration. This might be via environment variables (loaded into your shell) or configuration files (e.g., `.properties`, `.yaml`). Copy any example configuration files (e.g., `config.example.properties` to `config.properties`) within the agent's directory and populate it with your specific values (API keys, project IDs, etc.). Consult the agent's README for details on required variables and configuration methods.


5.  **Running the Agent Locally:**
   *   Agents can typically be run locally for testing and interaction. The specific command will vary based on how the Java application is packaged and structured (e.g., executable JAR, Maven/Gradle plugin execution). Check the agent's README.
   *   **Executable JAR:** `java -jar target/<agent-artifact-name>.jar` (after `mvn package` or `gradle bootJar`)
   *   **Maven:** `mvn exec:java`
   *   **Gradle:** `gradle run`
   *   **ADK Dev UI:** Often involves running the following command from the agent's main directory (e.g., `agents/software-bug-assistant`).


       ```
       mvn compile exec:java -Dexec.args="\
           --server.port=8080 \
           --adk.agents.source-dir=src/main/java/com/google/adk/samples/software-bug-assistant \
           --logging.level.com.google.adk.dev=DEBUG \
           --logging.level.com.google.adk.demo.agents=DEBUG"
       ```


6.  **Testing the Agent Components:**
   *   The `src/test/java` directory will contain unit or integration tests (e.g., for custom tools, services, or core logic).
   *   These ensure the individual code components function correctly.
   *   The agent's README may provide instructions on how to run these tests, typically using Maven (`mvn test`) or Gradle (`gradle test`).


8.  **Deploying the Agent:**
   *   Some agents may be designed for deployment, potentially to platforms like [Google Cloud Run](https://cloud.google.com/run/docs/quickstarts/build-and-deploy/deploy-java-service) or other cloud services.
   *   A `deployment/` directory might contain necessary scripts (e.g., `Dockerfile`, shell scripts) and configuration files.
   *   Deployment usually requires specific Google Cloud setup (Project ID, enabled APIs, permissions) or other platform-specific configurations. The agent's README and the scripts within the `deployment/` folder would provide detailed instructions.


By following the specific instructions in each agent's `README.md`, you can effectively set up, run, evaluate, test, and potentially deploy these diverse Java examples.


## Directory Structure of Java Agents


Each Java agent displayed here is generally organized as follows. This structure is typical for a Maven or Gradle project.


```bash
java/
└── agents/
   └── <agent-name-folder>/                # e.g., forecasting-agent
       ├── README.md                       # Provides an overview of the agent
       ├── architecture-diagram.png        # Diagram of the agent pattern
       ├── deployment/                     # Deployment scripts and configurations
       │   ├── Dockerfile                  # (Example)
       │   └── deploy.sh                   # (Example)
       └── <agent-module-name>/            # Core agent module (often same as agent-name-folder)
           ├── pom.xml                     # (Maven) POM: dependencies, build config
           ├── .env.example                # (Optional) Example environment variables
           ├── config.example.properties   # (Optional) Example config
           └── src/
               ├── main/
               │   ├── java/
               │   │   └── com/google/adk/samples/agent/  # Java package structure
               │   │       ├── Agent.java      # Core agent logic
               │   │       ├── tools/          # Custom tools used by the agent
               │   │       ├── services/       # Business logic, integrations
               │   │       └── Main.java       # Main class for standalone execution
               │   └── resources/
               │       ├── application.properties # (Optional) Spring Boot or other framework config
               │       └── prompts/            # (Optional) Prompt templates
               └── test/
                   ├── java/
                   │   └── com/google/adk/samples/agent/  # Test package structure
                   │       ├── AgentTest.java  # Unit/Integration tests for the agent
                   │       └── tools/          # (Optional) Tests for custom tools
                   └── resources/
                       └── test-data.json      # (Optional) Test data
```


### General Structure


The root of each agent resides in its own directory under `java/agents/`. For example, the `forecasting-agent` is located in `java/agents/forecasting-agent/`.


#### Directory Breakdown


1.  **`<agent-name-folder>/` (e.g., `forecasting-agent`)**:
   *   This is the top-level directory for the agent.
   *   **`README.md`**: Provides detailed documentation specific to the agent, including its purpose, setup instructions, usage examples, and customization options.
   *   **`architecture-diagram.png`**: A visual diagram illustrating the agent's architecture.
   *   **`deployment/`**: (Optional) Contains scripts and files necessary for deploying the agent (e.g., `Dockerfile`, deployment scripts for cloud platforms).


2.  **`<agent-module-name>/` (Core Agent Code, e.g., `forecasting-agent` or a specific module name):**
   *   This directory contains the source code and build configuration for the Java agent.
   *   **`pom.xml` (Maven) or `build.gradle` (Gradle)**: The build script defining project dependencies, plugins, and build lifecycle.
   *   **`.env.example` or `config.example.properties`**: Example configuration files. Users copy these and fill in their specific values.
   *   **`src/main/java/`**: Contains the main Java source code.
       *   The code is organized into packages (e.g., `com.google.adk.samples.agent`).
       *   **`Agent.java` (or similar)**: Often contains the core logic for defining the agent, its tools, prompts, and registering it with the ADK.
       *   **`tools/` (package)**: Contains custom tools the agent uses.
       *   **`services/` (package)**: Contains business logic, external service integrations, etc.
       *   **`Main.java` (or similar)**: If the agent can be run as a standalone application, this would be the entry point.
   *   **`src/main/resources/`**: Contains non-Java resources.
       *   **`application.properties` / `.yaml`**: Configuration files, especially if using frameworks like Spring Boot.
       *   **`prompts/`**: Directory for storing prompt templates if they are externalized from code.
   *   **`src/test/java/`**: Contains Java test code (e.g., JUnit, Mockito tests).
       *   Mirrors the package structure of `src/main/java/`.
       *   **`AgentTest.java` (or similar)**: Tests for the agent's core functionality.
       *   **`tools/` (package)**: Tests for any custom tools.
   *   **`src/test/resources/`**: Contains resources needed for tests (e.g., test data files).



