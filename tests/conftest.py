import os

# A dev agent session inherits FALCONFOX_INSTANCE from its daemon, and would
# otherwise run the suite against the instance's nested layout instead of the
# default one the tests write their fixtures into.
os.environ.pop("FALCONFOX_INSTANCE", None)
