# ETX Clipboard Bridge working rules

- The public ETX bridge offers the clipboard (X11) channel and the configured GitHub Actions Runner channel for exact single-file reads. Runner refers to GitHub Actions. 剪切板, 剪贴板, and X11 refer to clipboard.
- The public single-file entry is etx_bridge.py. Use an authorized site and project configuration, and follow the original channel and request on resume. Other clipboard actions use their existing entry points.

- Keep source, real deployment settings, credentials and runtime results separate. Never commit `.private/`, `.work/`, keys, session files, business source, logs or waveforms.
- Use the owner's existing logged-in graphical ETX. Do not create, resume or terminate ETX sessions, focus windows, or send simulated keyboard/mouse input.
- On Windows use `clients/Invoke-EtxX11.ps1` as the sole transport entry. It delegates clipboard, cryptography and protocol work to `local/windows_etx_packet.ps1` and its canonical Python modules.
- Run a simulation only on an explicit request. Reading, reviewing, preparing a patch or checking status does not authorize a new case or rerun.
- Use only owner-authorized exact files and configured roots. UTF-8 text is limited to 256 MiB per file and per batch, at most ten exact files; chunks are at most 256 KiB. Check endpoint support before large transfers; older endpoints retain their previous limit. Supported PDF/Office binary downloads are read-only and at most 16 MiB. Never download an FSDB through the file interface.
- Upload only authorized UTF-8 patches with baseline SHA256, transaction backup and verified installation. Do not bypass conflicts or clear a queue to make an operation proceed.
- Treat a requested upgrade as a two-sided deployment. Use the same verified release on Windows and ETX, back up the files being replaced, reload the receiver, and verify its active release through the paired client. Complete available steps through the site's authorized installation path. When the current installation cannot accept a remote update, give the owner the single outstanding handoff and continue verification afterward. Follow docs/OPERATIONS.md.
- One task per Worker, at most five distinct profiles `work01`–`work05`. Follow the original request ID. A timeout or uncertain outcome is queried, never blindly redispatched.
- While simulations are running, combine status checks every 300 seconds. Do not poll Worker status when idle. This is a task-monitoring cadence, not a permanent clipboard watcher.
- Protect exact FSDB paths against active writers and overlapping readers. Preserve an uncertain resource reservation; it must not block unrelated Worker profiles or completed files.
- Clipboard synchronization is shared. Yield after a detected copy, wait 30 quiet seconds, keep the original request, and retain the 300-second interruption budget. Do not describe this as isolation or paste-completion detection.
- Perform functional changes and synthetic checks in an isolated candidate. Never alter a live receiver or Worker merely to run unit tests. Live EDA, backend changes, deployment and version-control actions follow the owner's authorization.

- Treat complete downloaded logs as temporary analysis data. Prefer bounded first-error/tail excerpts; after analysis retain key evidence and source identity/hashes, then remove verified task-owned local full logs and transient bodies. Preserve remote originals, source baselines, upload payloads and transaction credentials.
