# GenMedia for Commerce Agent Quickstart

The **GenMedia for Commerce Agent** is a production-ready solution for generating high-quality retail media, including Virtual Try-On (VTO) videos and 360° product spins (Reference-to-Video / R2V). It is built using the Agent Development Kit (ADK) and demonstrates how to orchestrate complex media generation workflows with Gemini and Veo.

<img src="https://raw.githubusercontent.com/lspataroG/genmedia_for_commerce_assets/main/short_demo.gif" width="80%" alt="Demo">


<table>
  <thead>
    <tr>
      <th colspan="2">Key Features</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>👗</td>
      <td><strong>Virtual Try-On (VTO):</strong> Generates natural catwalk-style animation videos of models wearing specific clothes using Veo 3.1 R2V mode.</td>
    </tr>
    <tr>
      <td>🔄</td>
      <td><strong>360° Product Spinning:</strong> Generates smooth spinning videos of shoes and other products from a few static images.</td>
    </tr>
    <tr>
      <td>🛡️</td>
      <td><strong>Automated Validation:</strong> Includes rotation consistency checks and glitch detection to ensure high-quality outputs.</td>
    </tr>
    <tr>
      <td>🚀</td>
      <td><strong>Production-Ready:</strong> Modular backend, multi-stage Docker build, and deployment support for Cloud Run and Agent Runtime.</td>
    </tr>
  </tbody>
</table>

## 🚀 Getting Started

### Prerequisites
- **Python 3.11+**
- **Node.js 20+**
- **uv** (for dependency management)
- **Terraform** (for infrastructure setup)
- **Google Cloud SDK (gcloud)**

### Step 1: Get the Code

**Option A: Clone directly from adk-recipes**
```bash
git clone https://github.com/google/adk-recipes.git
cd adk-recipes/core/python/genmedia-for-commerce
```

**Option B: Create project from template**

This uses the [Google Agents CLI](https://github.com/google/agents-cli) to create a new directory (`genmedia4commerce`) with all the necessary code.

**Install the CLI** (one-time):

```bash
uvx google-agents-cli setup
```

**Create the project from this recipe**:

```bash
agents-cli create genmedia4commerce -a adk@genmedia-for-commerce
```

You'll be prompted to select a deployment option — choose **None**, as deployment is already handled in the code repo.

### Step 2: Configure Environment
Copy the example config and fill in your project-specific values:
```bash
cp config.env.example config.env
```
Then edit `config.env` and set at least:
```env
PROJECT_ID=your-gcp-project-id
IMAGE_NAME=gcr.io/your-gcp-project-id/genmedia-for-commerce
```

### Step 3: Install Dependencies
```bash
make install
```
This installs Python dependencies via `uv` and frontend packages.

### Step 4: Authenticate with Google Cloud
```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project your-gcp-project-id
```

### Step 5: Infrastructure Setup
Enable necessary APIs and create initial resources via Terraform:
```bash
make setup-infra
```

---

## 💻 Running Locally

To start the backend and frontend development servers:
```bash
make dev
```
*(Note: Refer to specific Makefiles in `src/backend` and `src/frontend` if running parts separately.)*

---

## ☁️ Cloud Deployment Options

After completing the initial setup, you have two deployment options:

### Option A: Deploy Agent to Agent Runtime + Gemini Enterprise
This deployment focuses solely on the agent logic and tools, making it accessible via Gemini Enterprise.

1. **Deploy Agent**:
   ```bash
   make deploy-agent-engine
   ```
2. **Register with Gemini Enterprise**:
   ```bash
   make register-gemini-enterprise
   ```
   Follow the interactive prompts to confirm the Agent Runtime ID and link it to your instance.

### Option B: Deploy Full Application (Cloud Run with MCP & Frontend)
This deploys the entire application stack, including the React frontend, FastAPI backend, and MCP server.

1. **Build and Deploy**:
   ```bash
   make deploy-cloudrun
   ```
   This command builds the frontend, packages the application into a Docker container, pushes it to Artifact Registry, and deploys it to Cloud Run.

## Agent Details

| Attribute | Description |
| :--- | :--- |
| **Interaction Type** | Workflow / API |
| **Complexity** | Advanced |
| **Agent Type** | Orchestrator |
| **Components** | Veo 3.1, Gemini 2.5, Image Processing, Validation Loops |
| **Vertical** | Retail / Commerce |

### 1. Clothes Video VTO Pipeline
This pipeline generates a catwalk animation of a model wearing specific clothes.
- **Input**: A full-body image of a model wearing the clothes.
- **Process**:
  1. **Framing**: Splits the image into three framings: lower body, upper body, and face.
  2. **Canvas Creation**: Fits each framing onto a 16:9 canvas.
  3. **Veo Generation**: Calls Veo 3.1 R2V in parallel with the reference images and a detailed animation prompt.
- **Output**: A set of generated videos showing the model walking forward.

### 2. Shoe Spinning (R2V) Pipeline
This pipeline generates a 360° spinning video of a shoe from a few static images.
- **Input**: Multiple images of a shoe from different angles.
- **Process**:
  1. **Classification**: Identifies the viewing angle of each image (front, back, left, right, etc.).
  2. **Selection**: Picks the best images covering the necessary angles.
  3. **Stacking**: Combines images into up to 3 reference images (as limited by the Veo API).
  4. **Generation**: Generates an 8-second video using Veo 3.1.
  5. **Validation**: Checks for correct rotation direction and detects visual glitches. If validation fails, it retries generation up to a configured limit.
- **Output**: A smooth, continuous spinning video of the product.

### 3. Image VTO Pipeline (Static)
This pipeline generates static images of a model wearing specific clothes or glasses.
- **Input**:
  - Person image (Full body for clothes, front face for glasses).
  - Product images (Garments or glasses).
- **Process**:
  1. **Routing**: Routes to clothes or glasses pipeline based on input flags.
  2. **Generation**: Generates multiple variations using Gemini.
  3. **Evaluation**: Ranks variations and selects the best result.
- **Output**: Static images with the best score based on automated evaluation.

### 4. Catalog Search
This capability allows searching the product catalog using natural language queries.
- **Input**:
  - Search query (e.g., "red casual dress").
  - Optional audience filter (e.g., "women", "men").
- **Process**:
  1. **Vector Search**: Uses vector similarity search to find matching products.
  2. **Filtering**: Applies audience filters if provided.
- **Output**: A list of matched products with descriptions and image.


## 🛠️ Technologies Used

### Backend
- **FastAPI**: For high-performance API endpoints.
- **Google GenAI SDK**: For interacting with Gemini and Veo models.
- **uv**: For fast Python package management.
- **OpenCV & Pillow**: For image and video processing.

### Frontend
- **React** (with Vite): For building the user interface.

---
## Disclaimer
This agent recipe is provided for illustrative purposes only. It serves as a basic example of an agent and a foundational starting point for individuals or teams to develop their own agents.

Users are solely responsible for any further development, testing, security hardening, and deployment of agents based on this recipe. We recommend thorough review, testing, and the implementation of appropriate safeguards before using any derived agent in a live or critical system.
