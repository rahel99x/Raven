"""Named worker adapters and compatibility execution API.

Policy and admission are separate lower-layer contracts; this application
composition module imports scientific adapters only inside admitted children.
"""
from __future__ import annotations

from .execution import policy as _policy
from .execution.admission import operation_route as operation_route
from .planning_input import planning_inputs as planning_inputs

# Preserve released imports, constants and ContextVar identities.
globals().update({name: value for name, value in vars(_policy).items() if not name.startswith("__")})


from .execution.worker import _execute_operation as _execute_operation


from .execution.worker import execute_operation as execute_operation



from .execution.log_export import large_log_export as large_log_export


from .execution.capabilities import capability_ledger as capability_ledger




from .execution.admission import cli_admission as cli_admission


from .execution.admission import needs_job_local as needs_job_local


from .execution.admission import job_local_admission as job_local_admission


from .execution.admission import reporting_submission as reporting_submission


from .execution.admission import helper_admission as helper_admission


from .execution.admission import research_command_route as research_command_route
