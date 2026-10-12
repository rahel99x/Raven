# Log source paging and search

[README](../../README.md) · [Controls](../CONTROLS.md) · [Log presentation](log-view.md)

Read older log content and search beyond the retained live tail.
These operations use Raven's background reader and leave source files unchanged.
Each source page has its own cursor and selection. Copy operations retain that page's source identity.

**Prerequisites:** Select a job or project run with an accessible, declared log source.
The selected local or SSH file adapter determines where paths are read.

Open the command palette with `:`. Use `Ctrl-A` to inspect progress. Use
`:task cancel` to cancel an operation at the next bounded read.

<a id="feature-16"></a>

## Read older log content

1. Select a job in Jobs or History.
2. Press `l`. Select its log file and press Enter.
3. Enter `:logolder` to read content before the retained tail.
4. Press `[` to read an earlier source page. Press `]` to read the next page.
5. Press Esc to return to the original log view.

Use `:logolder 0` to start at the beginning. Use `:logolder BYTE_POSITION` to
start at a specific byte position. Positions start at zero. An arbitrary byte
position advances to the next complete line when that boundary is available.
A very long line appears as explicitly labelled continuation fragments.

Each page contains at most 256 KiB and 500 logical rows. The page header gives
its exact byte range and the initial file size. A line label starts with `L`
when its absolute line number is known. A label starts with `B` when only its
absolute byte position is known. No tail-relative index appears as an absolute
source line number.

| Control | Function |
| --- | --- |
| Up / Down | Move the source-page cursor. |
| Page Up / Page Down | Move by the visible row count, with one overlapping row. |
| Home / End | Move to the first or last row of the loaded page. |
| Left / Right | Move the horizontal display position by eight columns. |
| `[` / `]` | Read the previous or next file page in the background. |
| `v` | Start or stop a raw-line selection at the keyboard cursor. |
| `y` or `:copy selection` | Copy the selected raw lines, or the explicitly selected cursor line. |
| `Y` or `:copy all` | Copy the complete source file, including content outside the page. |
| Right-click | Clear the local selection and cursor highlight. Keep the dialog and exact source open. |
| Esc | Return to the previous results, bookmark list, or original log view. |

After right-click clearing, click a line or use navigation keys before copying a cursor line.
Shift-click extends a source-page line selection.
Right-click has the same local clearing behavior in search results and named-bookmark lists.
It does not open a result or remove a bookmark.
Select a row again before opening or deleting it.

An orange mark at the right edge identifies selected rows. The ASCII fallback
uses `*`. Copy operations retain CRLF, tabs, invalid UTF-8 bytes, and an
unterminated final line in a private export. Terminal clipboard delivery uses
the existing clipboard settings.

<a id="feature-17"></a>

## Search a complete log source

Enter `:logsearch ERROR` to search the selected file. The default search is a
case-insensitive literal search. It includes the file prefix outside the live
tail. It does not apply the retained-log `:find` window.

The search reads a snapshot of the file's initial byte range. If the producer
appends more content, rerun the search to include it. The result indicates
whether the complete initial range was inspected.

The default operation inspects at most 512 MiB per source, uses a 30-second
operation budget, and retains at most 2,000 matching logical lines. The result
states partial coverage if a limit prevents complete inspection. Cancellation
keeps the prior view and does not publish a partial new search as complete.

<a id="feature-18"></a>

## Search every declared job log

Enter `:logsearch --all ERROR` to search the selected job or project run's cached
log declarations. Raven searches at most 32 distinct paths. It includes stdout,
stderr, registered worker files, and declared external locations. It does not
recursively search unrelated directories.

Each match retains its source ID, exact path, job or run identity, and file
identity. An unavailable file has its own source error. Results from another
file remain available. The result indicates partial coverage if a source was
unavailable or a source limit was reached.

<a id="feature-19"></a>

## Select search modes

Use these options with `:logsearch`:

| Option | Function |
| --- | --- |
| `--literal` | Match the supplied text literally. This is the default. |
| `--regex` | Interpret the text as a regular expression. |
| `--case` | Make the search case-sensitive. |
| `--nocase` | Ignore case. This is the default. |
| `--word` | Require a whole-word match. |
| `--all` | Search the declared log catalog for the selected job or run. |
| `--` | Treat following arguments as search text, including leading dashes. |

Examples:

```text
:logsearch --case --word ERROR
:logsearch --all --regex "ERROR.*timeout"
:logsearch --literal -- --failed
```

Regular expressions must have predictable backtracking. Raven rejects
backreferences, look-around, nested repetitions, repeated alternatives, and
multiple variable repetitions. A regular-expression search omits logical lines
larger than 8 KiB and reports the omitted count as partial coverage. Use literal
search to inspect oversized lines; literal search includes fragments and UTF-8
characters that cross fragment boundaries.

Use `:logsearchmode literal case word` to set the retained-log search mode for
`:find`, `N`, and `P`. Use `:logsearchmode regex nocase partial` to restore the
regular-expression, case-insensitive, partial-word mode. The selected mode is
saved with the UI preferences. These controls affect retained-log navigation;
whole-file searches use the options supplied to `:logsearch`.

Large retained logs build their first search index in the background.
The status shows indexing progress until the match count is available.
Raven reuses this index for navigation and updates it when lines arrive.
File replacement or a changed query requires a new index.
An incomplete index does not mean that the file has no matches.

<a id="feature-20"></a>

## Browse search results

A completed search opens the results browser. Use Up, Down, Page Up, Page Down,
Home, and End to select a match. The browser shows context before and after the
selected line and the exact source path.

Press Enter to read that match's source page. Press Esc to return to the same
selected result. Press Esc again to return to the original log view. Enter
`:logresults` to reopen the last search results.

The result header gives the number of sources, actual scanned bytes, source
errors, and oversized lines omitted by regular-expression search. Source
reports indicate complete or partial coverage. A file replacement invalidates
a result before its source page is opened. Search the new file again.

<a id="feature-21"></a>

## Go to an absolute source location

Use one of these commands:

```text
:loggoto line 2500
:loggoto byte 1048576
:loggoto percent 50
:loggoto time 2026-10-05T10:00:00Z
```

Line numbers start at one. Byte positions start at zero. A percentage must be
between zero and 100. A position at 100 percent opens the end of the inspected
snapshot, which can be an empty page.

A timestamp command selects the first reported ISO timestamp at or after the
specified time, in source order. Raven reads timestamps near the beginning of
each line. It compares timezone-aware timestamps with timezone-aware requests,
and local timestamps without an offset with requests that also have no offset.
It does not invent an offset for ambiguous timestamps. If the scan budget ends
before a location is found, Raven reports that limitation.

<a id="feature-22"></a>

## Save and select named log marks

1. Move the cursor to an important line in the original log or a source page.
2. Enter `:logmark first failure`.
3. Enter `:logmarks` to browse saved marks.
4. Press Enter to open the exact source location.
5. Press Esc to return to the marks list.

Use `d` or Delete in the marks list to remove a mark. Reusing a name for the
same path updates that mark. Raven retains at most 256 named marks in the UI
state. A mark stores the exact path, source identity, byte position, and absolute
line number when known. A replaced file requires a new mark. A mark from
another connection requires that original connection profile.

The older `m` and `'` controls remain available for retained-buffer bookmarks.
Named marks use source byte positions and can point outside the retained tail.

<a id="feature-23"></a>

## Return to each file's reading position

Raven retains each open source's vertical cursor and following or paused state
for the session. It restores that state when you switch back to the same job,
run, connection, and file. It starts each newly opened source at horizontal
column zero.

A file replacement, truncation, or change to the retained byte origin cancels a
stale position. Raven keeps at most 64 source positions. It does not restore a
selection onto a different file or copy a stale retained-line range.

## Public API methods

Run the `log_scan` file operations on the shared background worker.
Use the `log_tools` methods with published buffers on the terminal thread.
These methods preserve the selected source and report incomplete search coverage.

| Method | Result |
| --- | --- |
| `log_scan.snapshot(files, path)` | Validate a regular source's size, identity, and mutation metadata. |
| `log_scan.read_page(files, path, offset, ...)` | Publish bounded raw and display rows, exact byte ranges, line numbers when known, and coverage flags. |
| `log_scan.search_source(files, source, query, ...)` | Search one exact source and return matches, context, identity, and coverage. |
| `log_scan.search_sources(files, sources, query, ...)` | Search bounded declarations and retain per-source errors. |
| `log_scan.locate(files, source, kind, value, ...)` | Resolve an absolute line, byte, percentage, or timestamp to a source page. |
| `log_tools.before_source_change(app)` | Capture the old source's cursor and following state before a source reset. |
| `log_tools.observe_buffer(app, buffer)` | Restore a compatible published reading position. |
| `log_tools.handle_mouse(app, y, x, button, shift)` | Select a local row, or clear local selection on right-click without closing its dialog. |
| `log_tools.find_retained(app, buffer, backwards)` | Apply the retained-log search mode safely and return a handled flag plus the matched index. |
| `log_tools.count_retained(app, buffer)` | Read the cached match count; check `retained_pending` before treating it as complete. |
| `log_tools.retained_matches(app, line, pattern, source_bytes)` | Test one displayed line with the validated search mode and regex line limit. |
| `log_match_index.buffer_stamp(buffer)` | Capture the buffer revision to reject cached flags after an in-place refresh. |
| `log_match_index.build(buffer, context, matcher, regex)` | Build compact match flags from a published buffer; run large builds on the background worker. |
| `log_match_index.extend(index, buffer, context, matcher, ...)` | Update flags for bounded append and trim changes; return `None` when a rebuild is required. |
| `log_match_index.find(index, start, backwards)` | Find the next or previous match without reading source text again. |

Local reads use regular-file checks and nonblocking descriptor opens. SSH reads
use the existing remote adapter and exact byte offsets. No local-path fallback
is used for a remote-only adapter. Rotation, observed truncation, same-size
mutation, and incomplete reads prevent publication of a misleading snapshot.
