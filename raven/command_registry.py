"""Canonical, immutable presentation metadata for terminal commands and controls.

Handlers retain parsing, context checks and review enforcement. This module has
no scheduler or filesystem side effects. Core commands, feature contributions,
plugin hints, task workspaces, toolbar controls and operation forms all feed
palette/help consumers through this boundary.
"""
from __future__ import annotations

from .ui.catalog import SORTS, TABS
from dataclasses import dataclass, replace
from types import MappingProxyType


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    usage: str = ""
    arguments: tuple[str, ...] = ()
    example: str = ""
    owner: str = "controller"
    job_ids: bool = False
    paths: bool = False
    raw_tail: bool = False
    review: str = "handler"
    core: bool = False
    handler: str = ""
    routes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Control:
    key: str
    label: str
    command: str = ""
    prompt: bool = False
    local: str = ""
    context: str = ""
    description: str = ""


@dataclass(frozen=True)
class Task:
    key: str
    title: str
    description: str
    operations: tuple[str, ...]


COMMANDS = (
    Command('cancel', 'Cancel the selected, marked, or explicit jobs (review required)', usage='cancel [ids | marked | all]', job_ids=True, review='required', core=True, handler='app._command_cancel'),
    Command('hold', 'Hold pending jobs after reviewing targets', usage='hold [ids | marked]', job_ids=True, review='required', core=True, handler='app._command_cancel'),
    Command('release', 'Release held jobs after review', usage='release [ids | marked]', job_ids=True, review='required', core=True, handler='app._command_cancel'),
    Command('requeue', 'Requeue jobs after reviewing targets', usage='requeue [ids | marked]', job_ids=True, review='required', core=True, handler='app._command_cancel'),
    Command('top', 'Prioritize your pending jobs', usage='top [ids | marked]', job_ids=True, review='required', core=True, handler='app._command_cancel'),
    Command('filter', 'Filter the current table or log file browser', usage='filter <text>', example='filter training', raw_tail=True, core=True, handler='app._command_filter'),
    Command('sort', "Choose a table's sort key", usage='sort COLUMN', core=True, handler='app._command_sort'),
    Command('days', 'Set the accounting and analytics window', usage='days DAYS', example='days 7', core=True, handler='app._command_days'),
    Command('tab', 'Open a main page', usage='tab PAGE', example='tab history', core=True, handler='app._command_tab'),
    Command('view', "Choose the current page's workspace", usage='view <job|history|timeline|advisor|compare>', core=True, handler='app._command_view'),
    Command('export', 'Export a table or report', usage='export <text|csv|json|report>', paths=True, core=True, handler='export'),
    Command('copy', 'Copy selected raw log lines or the entire file', usage='copy [all|a b] (log lines or screen rows, 1-based)', example='copy all', core=True, handler='app._command_copy'),
    Command('gpu', 'Enable or disable GPU sampling', usage='gpu <on|off>', core=True, handler='app._command_gpu'),
    Command('bell', 'Enable or disable notification sounds', usage='bell <on|off>', core=True, handler='app._command_bell'),
    Command('source', 'Enable or disable a named scheduler source', usage='source <name> <on|off>', core=True, handler='app._command_source'),
    Command('theme', 'Choose terminal colors and contrast', usage='theme THEME', example='theme reader', core=True, handler='app._command_theme'),
    Command('refresh', 'Refresh scheduler and project sources', usage='refresh', core=True, handler='app._command_refresh'),
    Command('mark', 'Mark explicit jobs or all visible jobs', usage='mark <ids | all>', core=True, handler='app._command_mark'),
    Command('unmark', 'Clear explicit or all job marks', usage='unmark [ids]', core=True, handler='app._command_unmark'),
    Command('log', "Open a job's logs", usage='log <id>', example='log JOBID', job_ids=True, core=True, handler='app._command_log'),
    Command('find', 'Search log content with a regular expression', usage='find <text>  (regular expression; N / P next / previous match)', example='find ERROR', raw_tail=True, core=True, handler='app._command_find'),
    Command('profile', 'Switch a cluster configuration profile', usage='profile NAME', core=True, handler='app._command_profile'),
    Command('eval', 'Evaluate a local expression against cached jobs', usage='eval <expression>  e.g. [j.id for j in running if j.cpu < 0.3]', raw_tail=True, core=True, handler='evaluate'),
    Command('advise', 'Inspect evidence-based resource advice', usage='advise [id | name]  (what the job should ask for: --mem --cpus-per-task --time)', core=True, handler='advise'),
    Command('compare', 'Compare selected jobs', usage='compare [ids | marked | clear]  (the Analytics compare view)', job_ids=True, core=True, handler='app._command_compare'),
    Command('tag', 'Tag selected jobs', usage='tag [ids] <tags...>  (on the marked or selected jobs; filter #tag finds them)', job_ids=True, core=True, handler='app._command_tag'),
    Command('untag', 'Remove a job tag', usage='untag [ids] <tags...>', job_ids=True, core=True, handler='app._command_tag'),
    Command('pin', 'Prioritize selected jobs within batch order', usage='pin [ids]  (toggle: first within batches and between equally ranked batches)', job_ids=True, core=True, handler='app._command_pin'),
    Command('note', 'Annotate a job', usage='note [id] <text>  (a note shown with the job; empty clears)', job_ids=True, raw_tail=True, core=True, handler='app._command_note'),
    Command('chain', 'Review an action on a job and its downstream dependencies', usage='chain <cancel|hold|release> [id]  (the job and everything downstream)', review='required', core=True, handler='app._command_chain'),
    Command('resubmit', 'Clone and review a job submission', usage='resubmit [id] [--mem 12G] [--time 03:00:00] [-c 4] [--gres gpu:a100:2] [-p gpu] [--dependency=] [--script PATH] [--advised]  (clone the job; sbatch --test-only previews it)', job_ids=True, review='required', core=True, handler='app._command_resubmit'),
    Command('replay', 'Navigate a recorded session', usage='replay <pause|play|back|fwd|slower|faster|speed N|seek HH:MM|+60|50%>', core=True, handler='replay'),
    Command('wrap', 'Toggle wrapped log lines', usage='wrap  (Log tab: wrap or cut long lines)', core=True, handler='wrap'),
    Command('bookmark', 'Bookmark a log line', usage="bookmark  (Log tab: mark the current line; ' jumps)", core=True, handler='bookmark'),
    Command('help', 'Search keyboard and page help', usage='help', owner='command_ui', core=True, handler='app._command_help'),
    Command('quit', 'Exit Raven', usage='quit', core=True, handler='app._command_quit'),
    Command('metric', 'Record a metric sample', usage='metric FILE --value NAME=NUMBER [--step N] [--completed N --total N]', paths=True, core=True),
    Command('metrics', 'Attach an experiment metric stream', usage='metrics FILE (attach a JSONL experiment stream)', paths=True, core=True),
    Command('passport', 'Capture, inspect, or compare reproducibility records', usage='passport capture SCRIPT --workdir DIR | show FILE | compare LEFT RIGHT', paths=True, core=True),
    Command('validate', 'Validate declared project outputs', usage='validate CONTRACT ROOT (validate declared outputs)', paths=True, core=True),
    Command('artifacts', 'Attach an output contract', usage='artifacts CONTRACT ROOT (attach a declared output contract)', paths=True, core=True),
    Command('prepare', 'Validate a batch script offline', usage='prepare SCRIPT --workdir DIR [sbatch flags] (offline preflight)', paths=True, core=True),
    Command('submit', 'Review and submit a prepared batch script', usage='submit [SCRIPT --workdir DIR --passport-dir DIR] (confirmation required)', paths=True, review='required', core=True),
    Command('investigate', "Inspect a job's failure evidence", usage='investigate JOBID (scheduler, log and output evidence)', job_ids=True, core=True),
    Command('array', 'Inspect array tasks and prepare failed-task retries', usage='array retry ARRAYID SCRIPT --workdir DIR [--indices RANGE] [--limit N]', paths=True, core=True),
    Command('predict', 'Compare historical resource requirements', usage='predict [JOBID] [--file FILE] (resource intervals for comparable runs)', job_ids=True, paths=True, core=True),
    Command('forecast', 'Inspect scheduler start estimates', usage='forecast [JOBID] [--file FILE] (scheduler point and calibrated interval)', job_ids=True, paths=True, core=True),
    Command('blockers', 'Investigate why a job is waiting', usage='blockers [JOBID] [--file FILE] (scheduler reasons and evidence)', job_ids=True, paths=True, core=True),
    Command('tradeoffs', 'Compare explicit resource candidates', usage='tradeoffs FILE (compare explicit resource candidates)', paths=True, core=True),
    Command('checkpoint-advice', 'Review measured checkpoint costs and failure uncertainty', usage='checkpoint-advice FILE (read-only interval and sensitivity review)', paths=True, core=True),
    Command('scaling', 'Plan, run, or analyze scaling experiments', usage='scaling analyze FILE | plan RECIPE [--workdir DIR]', paths=True, core=True),
    Command('workflow', 'Inspect or execute a dependency workflow', usage='workflow analyze FILE | plan RECIPE [--workdir DIR]', paths=True, core=True),
    Command('choose', 'Prepare a resource candidate', usage='choose INDEX SCRIPT --workdir DIR (prepare a tradeoff candidate)', paths=True, core=True),
    Command('about', 'Show Raven version and update controls', owner='toolbar'),
    Command('asof', 'Inspect retained observations without changing live jobs', usage='asof [UTC_ISO_OR_UNIX [CLOCK_EPOCH] | live | record on/off | save NEW_DIRECTORY | load FILE]', owner='historical_ui', paths=True),
    Command('activity', 'Inspect notifications and background work', arguments=('show', 'clear'), owner='activity_ui'),
    Command('advisor-job', 'Select a published running job in Advisor', owner='job_selection'),
    Command('alerts', 'Configure alerts and quiet hours', owner='session_tools'),
    Command('arraymap', 'Inspect array task input mapping', owner='array_manifest_ui'),
    Command('artifact', 'Preview a declared artifact', owner='project_ui', paths=True),
    Command('back', 'Return to the previous location and selection', owner='navigation_ui'),
    Command('chart', 'Inspect exact chart samples and timestamps', example='chart preset 30m; chart axis fixed 0 100; chart mode raw; chart range 1 20; chart events', owner='analysis_ui'),
    Command('chartzoom', 'Inspect or reset a chart selection', owner='chart_interaction'),
    Command('columns', 'Choose visible job-table columns', arguments=('show', 'hide', 'reset', 'jobs', 'history'), example='columns show rss', owner='table_ui'),
    Command('commands', 'Search and execute commands', owner='command_ui'),
    Command('dashboard', 'Pin, reorder, hide, or color metrics', arguments=('list', 'reset', 'search', 'pin', 'unpin', 'hide', 'show', 'expand', 'collapse', 'move', 'color'), owner='analysis_ui'),
    Command('density', 'Choose comfortable, compact, or focused information density', arguments=('comfortable', 'compact', 'focused'), example='density compact', owner='workspace_layout'),
    Command('diff', 'Compare jobs or reproducibility passports', owner='analysis_ui', job_ids=True),
    Command('drill', 'Inspect the selected table field', owner='table_tools'),
    Command('execution', 'Inspect execution receipts, recovery, and observed results', arguments=('resume', 'retry', 'recover', 'collect'), owner='execution_ui', paths=True),
    Command('explain', "Explain a column's meaning, units, formula and source freshness", example='explain ce', owner='navigation_tools'),
    Command('exports', 'Browse retained exports', owner='activity_ui'),
    Command('facet', 'Filter jobs by state, partition, tag, name, or identity', owner='table_ui'),
    Command('filters', 'Edit structured table filters', owner='table_tools'),
    Command('focus', 'Move focus between independently scrollable workspace panels', arguments=('main', 'details', 'next'), owner='workspace_layout'),
    Command('focusbuttons', 'Navigate visible controls with arrow keys', owner='interaction'),
    Command('forward', 'Return to a location after Back', example='forward', owner='navigation_ui'),
    Command('freeze', 'Pause or resume the displayed inventory', owner='table_tools'),
    Command('gpuprovider', 'Choose and inspect GPU telemetry providers', owner='gpu_provider_ui'),
    Command('grouping', 'Automatically group, ungroup, expand, or collapse job groups', owner='grouping_toolbar'),
    Command('joblist', 'Choose grouped, sequential, or batch-ranked job lists', usage='joblist CONTEXT grouped|sequential|sort-min|sort-max', arguments=('jobs', 'recent', 'history', 'group', 'analytics:advisor', 'history:analytics', 'history:research', 'history:log', 'history:deps'), owner='grouping_toolbar'),
    Command('joblistmenu', 'Choose the view, ordering, or group actions for a job list',
            usage='joblistmenu CONTEXT view|order|groups', arguments=('jobs', 'recent', 'history', 'group', 'analytics:advisor'), owner='grouping_toolbar'),
    Command('headers', 'Toggle table field descriptions', owner='table_tools'),
    Command('history-browser', 'Show or hide the job history browser', owner='history_browser'),
    Command('history-dock', 'Choose the job history browser placement', owner='history_browser'),
    Command('history-focus', 'Focus the job history browser', owner='history_browser'),
    Command('history-job', 'Select a job from the history browser', owner='history_browser'),
    Command('history-scroll', 'Scroll the job history browser', owner='history_browser'),
    Command('launchsetup', 'Save project reporting declarations and launch recipes', owner='data_export_ui'),
    Command('launchjob', 'Review and launch an existing job script', owner='launch_wizard_ui', review='required'),
    Command('gettingstarted', 'Set up Raven for a workstation or cluster', owner='onboarding_ui'),
    Command('profiles', 'Create and review named site and lab profiles', owner='onboarding_ui'),
    Command('exportdata', 'Export retained measurements for exact recorded attempts', usage='exportdata [chart|close]', arguments=('chart', 'close'), owner='data_export_ui'),
    Command('historylogs', 'Export all registered logs for selected historical jobs', owner='history_log_export'),
    Command('historyrange', 'Choose the accounting history interval', owner='table_tools'),
    Command('inbox', 'Review completed jobs', owner='session_tools'),
    Command('inspect', 'Open the unified job inspector', owner='analysis_ui', job_ids=True),
    Command('jobactions', 'Review actions for selected jobs', owner='table_tools'),
    Command('jobgroup', 'Group selected jobs, detach members, or open and close a group', arguments=('create', 'ungroup', 'reset', 'open', 'close', 'toggle'), owner='table_ui'),
    Command('jobgroupmenu', 'Open grouping actions for selected jobs', owner='job_group_menu'),
    Command('jobgroups', 'Show or hide detected and manual job groups', arguments=('on', 'off'), owner='table_ui'),
    Command('jobpanel', 'Choose the selected job Details view', owner='job_panels'),
    Command('jump', 'Search cached jobs, runs, log files, views, workspaces, commands and locations', example='jump JOBID', owner='navigation_tools'),
    Command('keybindings', 'Edit, test and reset main-page keybindings', arguments=('reset',), example='keybindings', owner='navigation_tools'),
    Command('layout', 'Save, load, or resize a workspace layout', arguments=('list', 'save', 'load', 'delete', 'split'), owner='workspace_layout'),
    Command('location', 'Save, open, or delete an exact destination', arguments=('list', 'save', 'open', 'delete'), example='location save failure', owner='navigation_tools'),
    Command('logalign', 'Align split log streams', owner='log_workbench'),
    Command('logdiff', 'Compare log streams', owner='log_workbench'),
    Command('logdivergence', 'Compare declared rank logs within role cohorts', owner='log_workbench'),
    Command('logfold', 'Fold repeated log lines', owner='log_workbench'),
    Command('loggoto', 'Go to an exact log line', owner='log_tools'),
    Command('loggroup', 'Fold a group in the log-file browser', owner='log_workbench'),
    Command('logjson', 'Configure structured JSON log fields', owner='log_workbench'),
    Command('logmark', 'Name a log bookmark', owner='log_tools'),
    Command('logmarks', 'Browse log bookmarks', owner='log_tools'),
    Command('logolder', 'Load earlier log content', owner='log_tools'),
    Command('logpan', 'Scroll horizontally through wide log lines', owner='log_workbench'),
    Command('logpreview', 'Preview a selected log before opening it', arguments=('on', 'off'), owner='log_workbench'),
    Command('logresults', 'Browse log search results', owner='log_tools'),
    Command('logsearch', 'Search the current log', owner='log_tools'),
    Command('logsearchmode', 'Choose the log search mode', owner='log_tools'),
    Command('logunread', 'Jump to unread log content', owner='log_workbench'),
    Command('logview', 'Choose original, split, or structured JSON log presentation', arguments=('plain', 'split', 'json'), owner='log_workbench'),
    Command('marked', 'Inspect marked jobs', owner='table_tools'),
    Command('maximize', 'Enlarge the focused panel', arguments=('on', 'off'), owner='workspace_layout'),
    Command('menu', 'Open a toolbar menu', arguments=('File', 'Edit', 'View', 'Setup', 'Help'), owner='toolbar'),
    Command('favorite', 'Pin or unpin the current page in your saved navigation favorites',
            usage='favorite [toggle|list|clear]', arguments=('toggle', 'list', 'clear'), owner='toolbar'),
    Command('preferences', 'Open advanced display and background worker preferences', owner='toolbar'),
    Command('metric-live', 'Toggle a metric between live and complete-history views', owner='metric_live'),
    Command('metric-sampling', 'Set or reset a metric sampling interval', owner='metric_live'),
    Command('metric-window', 'Set a live metric time window', owner='metric_live'),
    Command('metricdisplay', "Set a metric's display label, declared unit and precision", example="metricdisplay METRIC label 'Friendly label' | unit DECLARED_UNIT | precision 3 | reset", owner='analysis_ui'),
    Command('node', 'Inspect a cluster node', owner='table_tools'),
    Command('notifications', 'Inspect full alerts, possible causes, and proposed fixes', usage='notifications [critical|moderate|mild|all]', arguments=('critical', 'moderate', 'mild', 'all'), owner='notification_ui', routes=tuple((option, 'activity_ui') for option in ('filter', 'level', 'job', 'task', 'persistent', 'clear', 'show'))),
    Command('ops', 'Inspect research operations and review their actions', arguments=('catalog', 'workspace', 'find', 'open', 'run', 'field', 'inspect', 'review', 'apply', 'form', 'copy', 'cancel', 'close', 'pin', 'unpin'), example='ops open investigation-workflow', owner='ops_ui'),
    Command('orchestrate', 'Review and execute a workflow or scaling plan', arguments=('workflow', 'scaling'), owner='execution_ui', paths=True),
    Command('outputs', 'Browse declared output artifacts', owner='project_ui'),
    Command('pane-focus', 'Focus a named scrollable pane', owner='pane_drag'),
    Command('pane-resize', 'Resize the focused split pane', owner='pane_drag'),
    Command('panel-scroll', 'Scroll the focused panel', arguments=('up', 'down', 'page-up', 'page-down', 'home', 'end'), owner='workspace_layout'),
    Command('peek', "Read and copy the selected record's complete value", example='peek name', owner='navigation_tools'),
    Command('preflight', "Edit and validate a batch script's effective resources", owner='execution_ui', paths=True),
    Command('project', 'Discover a standard project and its run inventory', example="project '/path with spaces/project'", owner='project_ui', paths=True),
    Command('projectguide', 'Connect and validate a standard project', example="projectguide '/path with spaces/project'", owner='ops_ui', paths=True),
    Command('queue-drag', 'Enable reviewed queue ordering by dragging job groups', owner='group_queue_drag'),
    Command('rate', 'Set or reset the scheduler update interval', owner='refresh_rate'),
    Command('refreshsettings', 'Configure queue polling, recording reads, and telemetry updates together', owner='telemetry_settings_ui'),
    Command('connectdata', 'Connect recordings and metrics, validate a sample, and preview data', owner='connect_data_ui', paths=True),
    Command('runhome', 'Follow the selected run from submission through recordings and results', owner='run_home_ui'),
    Command('recents', 'Configure completed-job grouping and preview size', usage='recents grouped|sequential|5|10|25|auto|expand|collapse|window DURATION|window all', arguments=('grouped', 'sequential', '5', '10', '25', 'auto', 'expand', 'collapse', 'window'), owner='table_tools'),
    Command('rerun', 'Review an exact retained cancelled job for a new attempt', usage='rerun [JOBID]', job_ids=True, review='required', owner='ops_ui'),
    Command('run', 'Bind, inspect, or clear a project run', arguments=('select', 'passport', 'clear'), example='run select RUN_ID', owner='project_ui'),
    Command('runs', 'Select a project run and execution attempt', owner='project_ui'),
    Command('savedview', 'Save or restore a filtered and sorted job table', arguments=('list', 'save', 'load', 'delete'), example='savedview save gpu-failures', owner='table_ui'),
    Command('series-scroll', 'Scroll the selected job series document', owner='analytics_document'),
    Command('settings', 'Preview and save terminal, sampler and clipboard preferences', arguments=('reset',), example='settings', owner='navigation_tools'),
    Command('telemetrysettings', 'Change Heavy telemetry and analytics update presets live',
            usage='telemetrysettings', owner='telemetry_settings_ui'),
    Command('setup', 'Configure allocation accounts and connect Research and Analytics evidence',
            usage='setup [overview|allocation|updates|project|exports|telemetry|installation|research VIEW|analytics VIEW|close]',
            arguments=('overview', 'allocation', 'updates', 'project', 'exports', 'telemetry', 'installation', 'research', 'analytics', 'close'),
            owner='setup_ui'),
    Command('shellcheck', 'Inspect a prepared shell script', owner='shell_checks_ui'),
    Command('smoothscroll', 'Toggle smooth scrolling', owner='scrolling'),
    Command('sortby', 'Combine column sorts; set ascending, descending, or off independently', example='sortby jobs id asc', owner='table_ui'),
    Command('sorteditor', 'Edit reversible cascading column sorts', owner='table_tools'),
    Command('startup', 'Toggle or preview the startup animation', owner='startup'),
    Command('task', 'Inspect or cancel background work', arguments=('show', 'cancel'), owner='activity_ui'),
    Command('telemetry', 'Inspect metric sampling capabilities and source coverage', owner='telemetry_ui'),
    Command('raven', 'Inspect and review Light/Heavy compute allocation controls',
            usage='raven [status|request|retry|reuse JOBID|cancel|release|inspect|close]',
            arguments=('status', 'request', 'retry', 'reuse', 'cancel', 'release', 'inspect', 'close'),
            owner='raven_ui', review='required'),
    Command('terminal', 'Open and manage embedded shell sessions, copy mode, and CLI fusion tools',
            usage='terminal [show|details|focus|blur|new|next|previous|session ID|close|copy-mode|copy|follow|scroll|search|rename|menu|health]',
            arguments=('show', 'details', 'focus', 'blur', 'new', 'next', 'previous', 'session', 'close',
                       'copy-mode', 'copy', 'follow', 'scroll', 'search', 'rename', 'menu', 'health'),
            owner='terminal_ui', paths=True),
    Command('terminaldoctor', 'Inspect terminal capabilities', owner='session_tools'),
    Command('terminaltest', 'Test terminal keys, mouse, and glyphs', owner='session_tools'),
    Command('timeline', 'Browse observed job and application events', arguments=('events', 'seek'), owner='analysis_ui'),
    Command('trace', 'Inspect or capture stacks for the selected execution attempt', owner='trace_ui'),
    Command('traces', 'Open the live and retained run trace workspace', owner='trace_ui'),
    Command('viewpicker', 'Choose a saved table view', owner='table_tools'),
    Command('where', 'Set structured table filters', owner='table_tools'),
    Command('workbench', 'Open a task workspace or pinned tools', arguments=('run', 'investigate', 'compare', 'results', 'connect', 'pinned', 'all'), example='workbench investigate', owner='ops_ui'),
    Command('workers', 'Switch or inspect background worker concurrency', usage='workers [single|multi|toggle|status] (background collection; the UI remains separate)', arguments=('single', 'multi', 'toggle', 'status'), example='workers status', owner='worker_ui'),
    Command('workspace', 'Open a Research workspace by name', owner='navigation_ui'),
    Command('workspaces', 'Search the Research workspaces', owner='navigation_ui'),
)

BY_NAME = MappingProxyType({item.name: item for item in COMMANDS})
CORE_NAMES = tuple(item.name for item in COMMANDS if item.core)
FEATURE_NAMES = tuple(sorted(item.name for item in COMMANDS if item.owner != "controller"))
PATH_FLAGS = frozenset(['--contract', '--file', '--output', '--passport-dir', '--root', '--script', '--workdir'])

TASKS = (
    Task('run', 'Run', 'Prepare, inspect and control an exact execution attempt.',
         ('pending-edit', 'rerun-job', 'array-throttle', 'allocation-shell', 'placement', 'batch-script', 'trace-capture', 'runtime-steering', 'io-coordination', 'diagnostic-recipe')),
    Task('investigate', 'Investigate', 'Follow a failure or delay to its supporting evidence.',
         ('investigation-workflow', 'trace-inspect', 'trace-snapshot', 'causal-delay', 'collective-audit', 'bottlenecks', 'incidents', 'evidence', 'investigation-timeline', 'recommendation', 'investigation-plan', 'minimize-reproducer')),
    Task('compare', 'Compare', 'Test a performance or scientific hypothesis with a bounded experiment.',
         ('statistics', 'energy', 'numerical-stability', 'regression-investigation', 'regression-sweep', 'diagnostic-campaign', 'execution-simulation', 'scientific-field', 'information-query')),
    Task('results', 'Results', 'Inspect outputs, acceptance, lineage and justified reuse.',
         ('acceptance', 'reuse', 'checkpoint', 'staging', 'scientific-impact', 'research-portable', 'environment', 'workflow-engine')),
    Task('connect', 'Connect', 'Connect a project or qualify an optional site integration.',
         ('project-guide', 'site-policy', 'site-telemetry', 'adapter', 'qualification', 'support-bundle', 'clusters', 'slurm-doctor', 'storage', 'supervisor')),
)

TOOLBAR_CONTROLS = MappingProxyType({
    'File': (
        Control('launch-job', 'Launch job...', 'launchjob'),
        Control('project', 'Open project...', 'project ', True, description='Discover a standard project and its run inventory.'),
        Control('workbench', 'Task workbench', 'workbench'),
        Control('project-guide', 'Connect and validate a project', 'projectguide'),
        Control('operations', 'Research and cluster operations', 'ops'),
        Control('runs', 'Project runs and attempts', 'runs', context='project'),
        Control('outputs', 'Run output artifacts', 'outputs', context='binding'),
        Control('metrics', 'Attach metrics stream...', 'metrics ', True),
        Control('artifacts', 'Attach output contract...', 'artifacts ', True),
        Control('prepare', 'Review batch script...', 'preflight ', True),
        Control('passport', 'Capture run passport...', 'passport capture ', True),
        Control('export-data', 'Export recorded data...', 'exportdata'),
        Control('export-chart-data', 'Export current chart range...', 'exportdata chart'),
        Control('export-text', 'Export current page as text', 'export text'),
        Control('export-csv', 'Export current table as CSV', 'export csv'),
        Control('export-json', 'Export selected jobs as JSON', 'export json'),
        Control('report', 'Export complete terminal report', 'export report'),
        Control('exports', 'Export library', 'exports'),
        Control('receipts', 'Execution receipts...', 'execution ', True),
        Control('profile', 'Switch connection profile...', 'profile ', True),
        Control('quit', 'Quit Raven', local='quit'),
    ),
    'Edit': (
        Control('copy-destination', 'Switch to Vim/Neovim yanking', local='copy-mode'),
        Control('copy', 'Copy current selection', 'copy'),
        Control('copy-all', 'Copy entire log / current page', 'copy all'),
        Control('select', 'Start line selection', local='select'),
        Control('select-all', 'Select entire log / current page', local='select-all'),
        Control('clear-selection', 'Clear line selection', local='clear-selection'),
        Control('filter', 'Filter this page...', 'filter ', True),
        Control('clear-filter', "Clear this page's filter", 'filter'),
        Control('filters', 'Structured table filters', 'filters', context='table'),
        Control('columns', 'Choose table columns', 'columns', context='table'),
        Control('sorts', 'Cascading sort editor', 'sorteditor', context='table'),
        Control('bookmark', 'Bookmark current log line', 'bookmark', context='log'),
        Control('pin', 'Pin / unpin selected jobs', 'pin', context='job'),
        Control('tag', 'Tag selected jobs...', 'tag ', True, context='job'),
        Control('note', 'Annotate selected job...', 'note ', True, context='job'),
        Control('save-location', 'Save current location...', 'location save ', True),
        Control('locations', 'Saved locations', 'location list'),
        Control('settings', 'Terminal and sampler settings', 'settings'),
        Control('shell-checks', 'Check prepared shell script', 'shellcheck'),
        Control('array-map', 'Array input mapping', 'arraymap'),
        Control('keys', 'Edit and test keybindings', 'keybindings'),
    ),
    'View': (
        Control('raven-status', 'Light/Heavy compute status', 'raven status'),
        Control('raven-request', 'Request compute...', 'raven request'),
        Control('raven-reuse', 'Review existing allocation...', 'raven reuse'),
        Control('raven-inspect', 'Inspect allocation', 'raven inspect'),
        Control('raven-cancel', 'Cancel compute request...', 'raven cancel'),
        Control('raven-release', 'Release compute...', 'raven release'),
        Control('workspaces', 'Search Research workspaces', 'workspaces'),
        Control('jump', 'Jump to job, run, file, or view', 'jump'),
        Control('back', 'Back to previous location', 'back'),
        Control('forward', 'Forward to next location', 'forward'),
        Control('comfortable', 'Comfortable panel density', 'density comfortable'),
        Control('compact', 'Compact panel density', 'density compact'),
        Control('focused', 'Focused panel density', 'density focused'),
        Control('focus-main', 'Focus job / main panel', 'focus main'),
        Control('focus-details', 'Focus Details panel', 'focus details'),
        Control('terminal', 'Open embedded terminal', 'terminal show'),
        Control('terminal-details', 'Show Details instead of terminal', 'terminal details'),
        Control('terminal-tools', 'Terminal sessions and CLI fusion tools', 'terminal menu'),
        Control('focus-buttons', 'Navigate buttons with arrow keys (F8)', 'focusbuttons'),
        Control('maximize', 'Maximize / restore focused panel', 'maximize'),
        Control('layouts', 'Saved workspace layouts', 'layout list'),
        Control('freeze', 'Pause inspection (sampling continues)', 'freeze'),
        Control('follow', 'Pause / follow current log', local='follow', context='log'),
        Control('wrap', 'Wrap / unwrap current log', 'wrap', context='log'),
        Control('refresh', 'Refresh all sources now', 'refresh'),
        Control('rate', 'Refresh settings', 'refreshsettings'),
        Control('gpu-provider', 'GPU source selection', 'gpuprovider'),
        Control('rate-reset', 'Reset queue polling', 'rate reset'),
        Control('workers-toggle', 'Switch background workers', 'workers toggle', local='workers', context='workers'),
        Control('workers-single', 'Use one background worker', 'workers single', local='workers', context='workers'),
        Control('workers-multi', 'Use concurrent background workers', 'workers multi', local='workers', context='workers'),
        Control('workers-status', 'Background worker status', 'workers status', local='workers', context='workers'),
        Control('smooth-scroll', 'Enable smooth scrolling', 'smoothscroll toggle'),
        Control('startup-toggle', 'Enable startup animation', 'startup toggle'),
        Control('startup-preview', 'Preview startup animation', 'startup preview'),
        Control('reader', 'Plain ASCII reader mode', 'theme reader'),
    ),
    'Setup': (
        Control('setup-connections', 'Connections and accounts', local='submenu:connections'),
        Control('setup-launch', 'Launch defaults', 'launchsetup'),
        Control('setup-recordings', 'Recordings and metrics', local='submenu:recordings'),
        Control('setup-refresh', 'Refresh and performance', local='submenu:performance'),
        Control('setup-installation', 'Installation and diagnostics', local='submenu:installation'),
    ),
    'connections': (
        Control('setup-profiles', 'Sites and lab profiles', 'profiles'),
        Control('setup-allocation', 'Heavy allocation settings', 'setup allocation'),
        Control('raven-status', 'Heavy status, cancel, and retry', 'raven status'),
    ),
    'recordings': (
        Control('connect-data', 'Connect data and validate a sample', 'connectdata'),
        Control('setup-project', 'Project reporting conventions', 'setup project'),
        Control('setup-telemetry', 'CPU, memory, and GPU recording guide', 'setup telemetry'),
        Control('setup-exports', 'Automated reporting and exports', 'setup exports'),
    ),
    'performance': (
        Control('refresh-settings', 'Refresh settings', 'refreshsettings'),
        Control('preferences', 'Advanced preferences', local='submenu:advanced'),
    ),
    'installation': (
        Control('setup-start', 'Getting started and Try Raven', 'gettingstarted'),
        Control('setup-installation', 'Installation guide', 'setup installation'),
        Control('doctor', 'Terminal diagnostics', 'terminaldoctor'),
        Control('terminal-test', 'Test keys, mouse, and glyphs', 'terminaltest'),
        Control('sources', 'Source health and freshness', 'tab sources'),
        Control('activity', 'Activity and background tasks', 'activity'),
    ),
    'Help': (
        Control('help', 'Page controls and searchable help', 'help'),
        Control('commands', 'Search all commands', 'commands'),
        Control('doctor', 'Terminal diagnostics', 'terminaldoctor'),
        Control('terminal-test', 'Test keys, mouse, and glyphs', 'terminaltest'),
        Control('sources', 'Source health and freshness', 'tab sources'),
        Control('telemetry', 'Metric sampling capabilities', 'telemetry'),
        Control('gpu-provider', 'GPU source selection', 'gpuprovider'),
        Control('activity', 'Activity and background tasks', 'activity'),
        Control('inbox', 'Completed-job review inbox', 'inbox all'),
        Control('alerts', 'Alert and quiet-hour controls', 'alerts'),
        Control('about', 'About Raven and update controls', local='about'),
    ),
})


def command(name, app=None):
    """Describe a command without granting permission to execute it.

    Plugins retain their existing dispatch precedence. Their hints are resolved
    per session rather than retained in a global cache across connections.
    """
    descriptor = BY_NAME.get(name)
    if descriptor is None:
        descriptor = Command(name, "Raven command")
    plugins = getattr(getattr(app, "plugins", None), "commands", {})
    if name in plugins:
        if name not in FEATURE_NAMES:
            hint = str(plugins[name][1])
            return Command(name, hint, usage=hint, owner="plugin")
    return descriptor



def command_owner(name, args=()):
    """Resolve an explicitly registered grammar alias without probing owners."""
    descriptor = BY_NAME.get(name)
    if descriptor is None:
        return ""
    return dict(descriptor.routes).get(args[0], descriptor.owner) if args else descriptor.owner


def command_names(app=None, *, core=CORE_NAMES):
    plugins = getattr(getattr(app, "plugins", None), "commands", {})
    return list(dict.fromkeys(tuple(core) + FEATURE_NAMES + tuple(sorted(plugins))))


def execute_core(app, name, args, line):
    """Dispatch migrated core commands after feature/plugin precedence checks."""
    descriptor = BY_NAME.get(name)
    if descriptor is None or not descriptor.handler:
        return False
    if descriptor.handler.startswith("app."):
        getattr(app, descriptor.handler[4:])(args, line, name)
    else:
        from . import command_handlers
        getattr(command_handlers, descriptor.handler)(app, args, line)
    return True


def argument_choices(name, app=None):
    """Return static or cheap contextual options; never collect live evidence."""
    if name == "theme":
        from .palette import THEME_NAMES
        return THEME_NAMES
    if name == "workbench":
        return tuple(item.key for item in TASKS) + ("pinned", "all")
    if name == "tab":
        return tuple(key for key, _ in TABS)
    if name == "sort" and app is not None:
        return tuple(SORTS.get(app.tab, ("name",)))
    if name == "days" and app is not None:
        return tuple(f"{value:g}" for value in app.days_options)
    if name == "profile" and app is not None:
        return tuple(app.cfg.profiles())
    return command(name, app).arguments


def usage(name, app=None):
    descriptor = command(name, app)
    if descriptor.owner != "plugin" and name in ("sort", "days", "tab", "profile", "theme"):
        choices = argument_choices(name, app)
        if choices:
            suffix = "  (restarts on that cluster)" if name == "profile" else ""
            return name + " <" + "|".join(choices) + ">" + suffix
        if name == "profile":
            return "profile: none configured ([profiles.<name>] in the config)"
    return descriptor.usage or name + (" <" + "|".join(descriptor.arguments) + ">" if descriptor.arguments else "")


def task(key):
    return next((item for item in TASKS if item.key == key), None)


def operation_keys(key):
    value = task(key)
    return value.operations if value else ()


def task_rows():
    return [{"key": item.key, "label": item.title, "command": "workbench " + item.key,
             "description": item.description, "operations": list(item.operations)} for item in TASKS]


def toolbar_controls(menu, overrides=None):
    """Reuse immutable static controls; copy only controls with changing labels."""
    overrides = overrides or {}
    return [replace(item, **overrides[item.key]) if item.key in overrides else item
            for item in TOOLBAR_CONTROLS[menu]]


_operation_snapshot = None


def operation_descriptors():
    """Build presentation metadata once per installed operation registry.

    Keep the source registry reference, avoiding recycled identity tokens and
    repeated deep copies of operation forms on every palette keypress.
    """
    global _operation_snapshot
    from . import operations
    registry = operations._registry()
    if _operation_snapshot is None or _operation_snapshot[0] is not registry:
        records = []
        for spec in operations.catalog():
            fields = tuple((field["key"], str(field.get("label", field["key"])),
                            tuple(field.get("choices", ()))) for field in spec.get("fields", ()))
            records.append((spec["key"], spec["title"], spec.get("summary", ""), fields))
        _operation_snapshot = (registry, tuple(records))
    return _operation_snapshot[1]


def operation_arguments(previous):
    """Complete supported ops grammar from live operation specifications."""
    values = previous[1:]
    operations = operation_descriptors()
    names = [(key, title + ": " + summary) for key, title, summary, _ in operations]
    if not values:
        return [(value, "operation action") for value in BY_NAME["ops"].arguments] + names
    if values == ["workspace"]:
        return [(item.key, item.description) for item in TASKS] + [("all", "all operations"), ("pinned", "pinned operations")]
    if len(values) == 1 and values[0] in ("open", "run", "pin", "unpin"):
        return names
    if len(values) >= 2 and values[0] == "run":
        record = next((item for item in operations if item[0] == values[1]), None)
        if record:
            supplied = {value.split("=", 1)[0] for value in values[2:] if "=" in value}
            return [(key + "=" + choice, label) for key, label, choices in record[3]
                    if key not in supplied for choice in (choices or ("",))]
    return []


def help_entries(app):
    """Generate searchable command help while retaining page-specific tutorials."""
    return [("Command reference", ":" + usage(name, app), command(name, app).description)
            for name in command_names(app)]
