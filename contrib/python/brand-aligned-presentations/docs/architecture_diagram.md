# Presentation Expert Agent - Architecture Diagram

This document illustrates the high-level multi-agent architecture of the Presentation Expert Agent. It highlights the unified data model, programmatic grounding for research, and enterprise-grade state persistence.

```mermaid
graph TD
    %% User Interaction & Security Layer
    User((User)) <-->|Prompt, Approval, & Edits| ModelArmor{Google Cloud\nModel Armor\nSecurity Interceptor}
    ModelArmor <-->|Sanitized I/O| Orchestrator[Orchestrator Agent\nGemini 3.6 Flash\nSelf-Correction Logic]
    
    %% Persistence Layer (The "Memory" of the Stateless Agent)
    Orchestrator <-->|Save/Load State| ArtifactStore[[ADK Artifact Store\n& Session State\nResearch Anchor]]
    ArtifactStore -.->|Turn-to-Turn Continuity| ArtifactStore

    %% Phase 1: Research & Strategy
    subgraph Phase 1: Context & Research
        Orchestrator -->|Internal IP| RAG[Internal RAG Agent]
        RAG -.->|Retrieval| VertexSearch[(Vertex AI Search)]
        
        Orchestrator -->|Fast Fact Lookup| FastSearch[Google Search Tool\nProgrammatic Grounding]
        Orchestrator -->|Complex Analysis| DeepResearch[Deep Research Agent\nURL Extraction Wrapper]
    end
    
    %% Phase 2: Dual Workflow Execution
    subgraph Phase 2: Dual Workflow Execution
        %% Create Workflow
        Orchestrator -->|Workflow 1: Create| OutlineAgent[Outline Specialist Agent]
        OutlineAgent -->|Research Continuity| ArtifactStore
        
        Orchestrator -->|Generate Approved Plan| BatchWriter[Batch Slide Writer Tool\nasyncio Parallel Execution]
        BatchWriter -->|Citation Preservation| SlideWriter[Slide Writer Agents]
        SlideWriter -->|Professional Bullets| ArtifactStore
        
        %% Edit Workflow
        Orchestrator -->|Workflow 2: Edit| PPTXEditor[Surgical Editor Tools\nadd, delete, replace, layout]
        PPTXEditor --> ArtifactStore
    end
    
    %% Phase 3: Assembly & Rendering
    subgraph Phase 3: Rendering & Delivery
        PPTXEngine[Python-PPTX Engine]
        
        ArtifactStore -->|Final DeckSpec| PPTXEngine
        PPTXEngine -.->|Speaker Note Citations| LayoutSafe{Anti-Squeeze\nLogic Mapping}
        LayoutSafe -->|Forced Image Box| PPTXEngine
        
        PPTXEngine -.->|Fetch Template| Template[(User / Default GCS)]
        PPTXEngine -.->|Generate Visuals| Imagen[Imagen 3 Model]
        Imagen -->|Hybrid Path| GCSBackup[(GCS Backup)]
    end
    
    %% Delivery
    PPTXEngine -->|application/octet-stream| User
    ArtifactStore -->|Reference| User

    %% Styling
    classDef agent fill:#f9f0ff,stroke:#6a1b9a,stroke-width:2px;
    classDef storage fill:#e1f5fe,stroke:#0277bd,stroke-width:2px;
    classDef system fill:#fff3e0,stroke:#e65100,stroke-width:2px;
    classDef security fill:#e8f5e9,stroke:#2e7d32,stroke-width:3px;
    
    class Orchestrator,RAG,FastSearch,DeepResearch,OutlineAgent,SlideWriter agent;
    class VertexSearch,Template,GCSBackup storage;
    class ArtifactStore,PPTXEditor,PPTXEngine,Imagen,BatchWriter system;
    class ModelArmor security;
```

### Architectural Highlights

1. **Research Continuity & Integrity:** To ensure 100% citation consistency, the system implements a **Research Anchor** logic. Once research is conducted in Phase 1, the resulting facts and raw URLs are locked into the session state. Subsequent outline revisions or slide generations strictly reuse this "Ground Truth" to prevent source links from being lost or summarized away.
2. **Programmatic Grounding Extraction:** Rather than relying on the LLM to manually copy-paste URLs, the `google_research_tool` and `deep_research_tool` use custom `FunctionTool` wrappers. These wrappers programmatically intercept the model's response and extract verified source URIs directly from the tool's grounding metadata, ensuring data provenance is never lost.
3. **Stateless State Persistence:** The agent uses a combination of the **ADK Artifact Store** (for physical JSON plans) and **Session State** (for transient research summaries). This hybrid persistence ensures that complex presentations survive cloud worker node rotations and long-running research tasks.
4. **Unified Data Model (`slides`):** To prevent model confusion and "Malformed Function Call" errors, the entire system uses a single consistent data structure. The same `SlideSpec` model is used for planning (focus instruction), interactive revisions, and final output (professional bullets).
5. **Anti-Squeeze Layout Safety:** The rendering engine features a **Smart Layout Guard**. It automatically overrides "squeezed" layout requests (like "Title and Chart") and remaps them to professional alternatives (like "Title and Image") while automatically appending all citations to the slide's speaker notes.
6. **Parallel Content Generation:** Latency is minimized by offloading synthesis to the `batch_slide_writer_tool`, which utilizes Python's `asyncio.gather` to generate detailed content for an entire deck concurrently.
