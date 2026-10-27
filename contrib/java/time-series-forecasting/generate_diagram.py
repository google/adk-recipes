# To generate the diagram from this script, ensure you have Python and the 'diagrams' library installed.
# For more information on the 'diagrams' library, visit: https://diagrams.mingrammer.com/
# Then, navigate to the directory containing this file in your terminal and run:
# python time-series-forecasting-diagram.py
# The output image (e.g., time-series-forecasting-diagram.png) will be saved in the same directory.

from diagrams import Cluster, Diagram, Edge
from diagrams.gcp.analytics import BigQuery
from diagrams.gcp.compute import Run as CloudRunService
from diagrams.gcp.devtools import Code as DevToolCode
from diagrams.gcp.ml import AIPlatform
from diagrams.onprem.client import User

with Diagram(
    "Time Series Forecasting Architecture",
    filename="diagram",
    show=False,
    direction="LR",
    graph_attr={
        "splines": "ortho",
        "fontsize": "18",
        "labelloc": "t",
        "nodesep": "1.0",
        "ranksep": "1.5",
    },
):
    cli_user = User("User")

    with Cluster("Google Cloud"):
        with Cluster("Forecasting App"):
            agent_cloud_run_app = CloudRunService("Java ADK Agent")

        with Cluster("MCP Toolbox for Databases"):
            mcp_toolbox_cloud_run_service = CloudRunService(
                "MCP Toolbox\nServer"
            )
            forecasting_tools_on_mcp = DevToolCode("MCP Forecasting\nTools")
            (
                mcp_toolbox_cloud_run_service
                >> Edge(label="Hosts Tools")
                >> forecasting_tools_on_mcp
            )

        with Cluster("Vertex AI"):
            llm_gemini = AIPlatform("Gemini LLM")

        with Cluster("BigQuery"):
            bq_engine = BigQuery("BQ Engine")
            bq_dataset = BigQuery("BQ Dataset")
            timesfm_model_in_bq = BigQuery("TimesFM Model")

        agent_cloud_run_app >> Edge(label="LLM for Reasoning") >> llm_gemini
        (
            agent_cloud_run_app
            >> Edge(label="Invokes Tools")
            >> mcp_toolbox_cloud_run_service
        )

        (
            forecasting_tools_on_mcp
            >> Edge(label="Executes AI.FORECAST")
            >> bq_engine
        )

        bq_engine >> Edge(label="Reads Data") >> bq_dataset
        bq_engine >> Edge(label="Forecasts With") >> timesfm_model_in_bq

    cli_user >> Edge(label="Requests Forecast") >> agent_cloud_run_app
    (
        agent_cloud_run_app
        >> Edge(label="Returns Forecast", style="dashed", color="#2E8B57")
        >> cli_user
    )
