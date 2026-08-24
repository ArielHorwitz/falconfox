"""UI-agnostic coordinator engine.

Everything here talks to ACP agents and emits plain JSON-serializable events; it
holds no reference to any UI. The web layer subscribes to the event bus and
drives the engine through its methods.
"""
