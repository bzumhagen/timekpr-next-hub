"""Pure, IO-free logic shared by the hub and the agent.

Nothing in this package talks to a network, a database, or DBUS. That is
deliberate (see the plan's "Tech stack" section): the convergence math and
calendar rules are the most safety-critical part of this project, and living
in one shared package means they are written once, tested once, and cannot
drift between the hub and the agent.
"""
