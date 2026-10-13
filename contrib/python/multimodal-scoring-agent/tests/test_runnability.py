def test_import():
    from multimodal_scoring_agent.agent import scoring_agent

    assert scoring_agent is not None
    assert scoring_agent.name == "multimodal_scoring_agent"
