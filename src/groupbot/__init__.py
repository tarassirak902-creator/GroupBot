"""GroupBot implementation based on the current MASTER specification."""

# Compatibility for the legacy group-control UX router. The effective rank
# limit replaced the old TEST-only helper, but the UX router is still loaded
# before the main group-control router and imports the historical name.
# Keep the alias here until that duplicate router is consolidated.
from groupbot.routers import group_control as _group_control

if not hasattr(_group_control, "_trial_rank_limit"):
    _group_control._trial_rank_limit = _group_control._rank_limit
