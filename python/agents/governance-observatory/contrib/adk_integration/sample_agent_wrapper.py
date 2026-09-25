"""
Sample wrapper for ADK agents – demonstrates plugin usage.
"""

class SampleAgent:
    name = "sample_agent"
    tools = ["sample_tool"]

    def run(self, input_data):
        return {"status": "success", "data": input_data}