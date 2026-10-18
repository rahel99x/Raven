# Experimental terminal plot images

Raven's Braille, ASCII and text views remain the default and the complete
accessible representation. An optional Kitty prototype can display one existing
Braille plot as a small RGBA image. It preserves the existing plot occupancy;
it does not create additional samples or increase scientific resolution. The
prototype uses monochrome pink occupancy. Plots with multiple occupied-cell
styles retain colored text to preserve threshold/category distinctions.

In the ordinary Raven UI configuration file, opt in explicitly:

```toml
terminal_raster = "kitty"
```

Restart Raven after changing this experimental setting. Return to `"off"` to
disable it. This setting is separate from the managed
runtime's `raven.config/v1` document. Raven requires an interactive terminal with
`TERM=xterm-kitty` and a numeric `KITTY_WINDOW_ID`. Unknown terminals,
noninteractive output, SSH sessions, tmux and Screen retain the ordinary plot.
A requested `"sixel"` mode also retains text; it is not treated as Kitty.

The prototype rasterizes at most one fully visible 80-column by 24-row plot,
using its existing 2-by-4 Braille dot positions. Axes, labels, copy/export data,
and mouse coordinates retain their ordinary behavior. Opening an overlapping
menu, selecting text, dragging a chart, changing views, resizing, clipping the
plot or leaving Raven removes the image and restores text. Pager and external
terminal handoffs also remove Raven's own image. Other applications' images are
never deleted.

Updates are bounded to two images per second and 128 KiB encoded bytes per
image. If a source changes sooner, Raven displays the fresh Braille plot instead
of leaving an old image under a new axis. No terminal response is requested:
Kitty's `q=2` suppresses graphics replies, so keyboard/PTY ownership remains with
Raven's existing input parser. On output failure, the current frame is repainted
as text. Exports and accessibility never depend on image support.

## Protocol scope and qualification

[Kitty graphics](https://sw.kovidgoyal.net/kitty/graphics-protocol/) uses explicit
image identifiers, RGBA transfer, placement and deletion. Raven chunks base64
payloads at 4096 characters, positions in terminal cells, restores the cursor,
and deletes only its own image identifier. Output participates in the existing
synchronized frame when that capability is available.

[Sixel](https://vt100.net/docs/vt3xx-gp/chapter14.html) is a separate raster
protocol with palette and scrolling/lifecycle semantics that differ from
Kitty's identifier-based placements. No Sixel encoder or deployment support is
claimed. tmux/Screen passthrough and SSH capability forwarding require separate
qualification; copying the Kitty bytes into those transports is not sufficient.

Automated tests cover protocol bounds, source preservation, overlay and
selection fallback, view/resize cleanup, failure recovery, and the actual
curses flush hooks. Raw PTY transport measurements do not establish emulator
rendering quality, display latency, or remote usability. Actual Kitty/Sixel,
multiplexer and SSH visual qualification remains an external gate. The feature
must remain off by default until that evidence supports wider deployment.
