# Third-party licenses

This document tracks third-party software that ships with Hotkeys.
The dist redistributes these projects **unmodified**, satisfying the
"mere aggregation" boundary of any copyleft license they carry.

## Tenacity (bundled audio editor)

* **Project:** Tenacity, a community fork of Audacity
* **Upstream:** https://codeberg.org/tenacityteam/tenacity
* **Bundled version:** 1.3.4 (released 2025-01-01)
* **License:** GNU General Public License v2 (with documentation under CC BY 3.0)
* **Source:** Available at the upstream link above. We do not modify the
  binary on disk. The Hotkeys app sends a normal Win32 `WM_SETTEXT`
  message to the window after it appears to relabel its title to
  "Audio Editor", this is not a modification of the program.
* **License text:** Shipped verbatim inside the bundle at
  `audio_editor_assets/tenacity/LICENSE.txt`.
* **Why bundled:** Provides the Shift+F10 audio editor feature without
  forcing users to install anything separately.

## FFmpeg shared libraries (video/audio import for the audio editor)

* **Project:** FFmpeg, multimedia decode/encode toolkit
* **Upstream:** https://ffmpeg.org/
* **Build source:** BtbN/FFmpeg-Builds GitHub auto-builds, "n7.1 latest
  win64 GPL shared" variant
* **Bundled version:** 7.1 (libavformat 61, libavcodec 61, libavutil 59,
  libswresample 5, libswscale 8, libavfilter 10, libavdevice 61,
  libpostproc 58)
* **License:** GNU GPL v3 (this build mixes GPL components like x264,
  x265). Bundling unmodified GPL binaries alongside our app is
  "mere aggregation".
* **Why bundled:** Tenacity loads these DLLs to read video containers
  (mkv, mp4, mov, m4a, etc) and extract the audio track. Without them
  importing a video pops a "did not recognize the type" error.
* **Source:** https://ffmpeg.org/download.html plus the patches at
  https://github.com/BtbN/FFmpeg-Builds

## Excalidraw (bundled whiteboard)

* **Project:** Excalidraw
* **Upstream:** https://github.com/excalidraw/excalidraw
* **License:** MIT
* **Bundle location:** `whiteboard_assets/dist/`
* **Why bundled:** Provides the Shift+F8 whiteboard feature.

## Compliance summary

All bundled projects are redistributed in their original published
form. Source code for any GPL-licensed component is available at its
upstream link. Hotkeys itself does not link against any of these at
the binary level, they are launched as sibling processes and
communicated with via the operating system's standard IPC and window
messages.
