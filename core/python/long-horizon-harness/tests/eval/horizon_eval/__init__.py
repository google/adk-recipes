# Shim for `adk eval`, which resolves <pkg>.agent.root_agent. horizon's own
# __init__ deliberately does not import agent so `import horizon` stays offline.
from horizon import agent
