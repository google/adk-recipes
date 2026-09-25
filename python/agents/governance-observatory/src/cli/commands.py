"""
CLI commands for ADK Governance Observatory.
"""

import click
import json
from src.adapters.adk_adapter import ADKGovernanceWrapper
from src.governance.replay import ReplayEngine

@click.group()
def cli():
    """ADK Governance Observatory CLI."""
    pass

@cli.command()
@click.argument('agent_class')
@click.argument('input_file', type=click.Path(exists=True))
def run(agent_class, input_file):
    """Run an agent with governance observability."""
    with open(input_file, 'r') as f:
        input_data = json.load(f)

    # Dynamically import agent class
    module_path, class_name = agent_class.rsplit('.', 1)
    module = __import__(module_path, fromlist=[class_name])
    AgentClass = getattr(module, class_name)

    agent = AgentClass()
    wrapper = ADKGovernanceWrapper(agent)
    result = wrapper.run(input_data)

    click.echo(json.dumps({
        "trace_id": result["trace"].trace_id,
        "final_decision": result["trace"].final_decision.value,
        "verification": result["verification_status"],
        "certificate_hash": result["certificate"].certificate_hash
    }, indent=2))

@cli.command()
@click.argument('trace_file', type=click.Path(exists=True))
def replay(trace_file):
    """Replay a governance trace."""
    from src.governance.models import GovernanceTrace
    with open(trace_file, 'r') as f:
        data = json.load(f)
    trace = GovernanceTrace(**data)
    engine = ReplayEngine()
    report = engine.replay(trace)
    click.echo(json.dumps(report, indent=2))