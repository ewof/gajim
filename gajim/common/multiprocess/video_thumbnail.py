# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import typing

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import gi

try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
except Exception:
    if typing.TYPE_CHECKING:
        from gi.repository import Gst

Gst.init(None)

log = logging.getLogger("gajim.c.multiprocess.video_thumbnail")


def _subprocess_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        # Avoid a console flash when the NSIS-installed Gajim runs ffmpeg.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def _find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    exe_name = f"{name}.exe" if sys.platform == "win32" else name
    candidates = [
        Path(sys.executable).resolve().parent / exe_name,
        Path(sys.argv[0]).resolve().parent / exe_name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def extract_video_thumbnail_and_properties(
    input_: Path, output: Path | None, preview_size: int
) -> tuple[bytes, dict[str, typing.Any]]:
    try:
        return _extract_with_gstreamer(input_, output, preview_size)
    except Exception as error:
        log.info("GStreamer thumbnail failed for %s: %s", input_, error)
        return _extract_with_ffmpeg(input_, output, preview_size)


def transcode_for_preview(input_: Path, output: Path) -> Path:
    """Re-encode to yuv420p H.264 so GStreamer playbin can play the file."""
    ffmpeg = _find_tool("ffmpeg")
    if ffmpeg is None:
        raise Exception("ffmpeg not found")

    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
        timeout=120,
        **_subprocess_kwargs(),
    )
    if not output.exists() or output.stat().st_size <= 0:
        raise Exception("ffmpeg produced an empty file")
    return output


def _extract_with_ffmpeg(
    input_: Path, output: Path | None, preview_size: int
) -> tuple[bytes, dict[str, typing.Any]]:
    ffmpeg = _find_tool("ffmpeg")
    if ffmpeg is None:
        raise Exception("ffmpeg not found")

    dest = output
    if dest is None:
        dest = input_.with_name(f"{input_.stem}_ffmpeg_thumb.png")

    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0.5",
            "-i",
            str(input_),
            "-frames:v",
            "1",
            "-vf",
            f"scale={preview_size}:-2",
            "-update",
            "1",
            str(dest),
        ],
        check=True,
        timeout=20,
        **_subprocess_kwargs(),
    )
    data = dest.read_bytes()
    if not data:
        raise Exception("ffmpeg produced an empty thumbnail")

    metadata: dict[str, typing.Any] = {"width": 0, "height": 0, "duration": 0.0}
    ffprobe = _find_tool("ffprobe")
    if ffprobe is not None:
        probe = subprocess.run(  # noqa: S603
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_entries",
                "format=duration:stream=width,height",
                str(input_),
            ],
            check=True,
            capture_output=True,
            timeout=15,
            **_subprocess_kwargs(),
        )
        info = json.loads(probe.stdout.decode())
        try:
            duration = info.get("format", {}).get("duration") or 0
            metadata["duration"] = float(duration) * 1000
        except (TypeError, ValueError):
            pass
        for stream in info.get("streams") or []:
            if stream.get("width") and stream.get("height"):
                metadata["width"] = int(stream["width"])
                metadata["height"] = int(stream["height"])
                break
    return data, metadata


def _extract_with_gstreamer(
    input_: Path, output: Path | None, preview_size: int
) -> tuple[bytes, dict[str, typing.Any]]:
    pipeline = Gst.Pipeline.new()

    uridecodebin = Gst.ElementFactory.make("uridecodebin3")
    videoflip = Gst.ElementFactory.make("videoflip")
    videoconvert = Gst.ElementFactory.make("videoconvert")
    videoscale = Gst.ElementFactory.make("videoscale")
    capsfilter = Gst.ElementFactory.make("capsfilter")
    pngenc = Gst.ElementFactory.make("pngenc")
    appsink = Gst.ElementFactory.make("appsink")

    pipeline_elements = [
        uridecodebin,
        videoflip,
        videoconvert,
        videoscale,
        capsfilter,
        pngenc,
        appsink,
    ]
    if any(element is None for element in pipeline_elements):
        raise Exception(f"\n{__name__}: Some pipeline elements were None")

    assert uridecodebin is not None
    assert videoflip is not None
    assert videoconvert is not None
    assert videoscale is not None
    assert capsfilter is not None
    assert pngenc is not None
    assert appsink is not None

    appsink.set_property("emit-signals", False)
    appsink.set_property("sync", False)
    appsink.set_property("max-buffers", 1)
    appsink.set_property("drop", True)
    uridecodebin.set_property("uri", input_.as_uri())
    videoflip.set_property("method", 8)  # automatic

    pipeline.add(uridecodebin)
    pipeline.add(videoflip)
    pipeline.add(videoconvert)
    pipeline.add(videoscale)
    pipeline.add(capsfilter)
    pipeline.add(pngenc)
    pipeline.add(appsink)

    metadata: dict[str, typing.Any] = {"width": 0, "height": 0}

    def probe_original_size(
        pad: Gst.Pad, _info: Gst.PadProbeInfo
    ) -> Gst.PadProbeReturn:
        caps = pad.get_current_caps()
        if caps is None:
            return Gst.PadProbeReturn.OK

        structure = caps.get_structure(0)
        if structure.has_field("width") and structure.has_field("height"):
            metadata["width"] = structure.get_int("width")[1]
            metadata["height"] = structure.get_int("height")[1]
            return Gst.PadProbeReturn.REMOVE

        return Gst.PadProbeReturn.OK

    sink_pad = videoscale.get_static_pad("sink")
    assert sink_pad is not None
    sink_pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, probe_original_size)

    def on_pad_added(_bin: Gst.Bin, pad: Gst.Pad) -> None:
        assert pad is not None
        sink_pad = videoflip.get_static_pad("sink")
        assert sink_pad is not None
        if not sink_pad.is_linked():
            pad.link(sink_pad)

    handler_id = uridecodebin.connect("pad-added", on_pad_added)

    videoflip.link(videoconvert)
    videoconvert.link(videoscale)
    videoscale.link(capsfilter)
    capsfilter.link(pngenc)
    pngenc.link(appsink)

    # https://gitlab.freedesktop.org/gstreamer/gstreamer/-/blob/1.26/subprojects/gst-plugins-base/gst/videoconvertscale/gstvideoconvertscale.h#L63
    lanczos_filter = 3
    videoscale.set_property("method", lanczos_filter)
    caps = Gst.Caps.from_string(
        f"video/x-raw,width={preview_size},pixel-aspect-ratio=1/1"
    )
    capsfilter.set_property("caps", caps)

    pipeline.set_state(Gst.State.PAUSED)

    def cleanup() -> None:
        pipeline.set_state(Gst.State.NULL)
        uridecodebin.disconnect(handler_id)
        for elem in pipeline_elements:
            if elem is not None:
                pipeline.remove(elem)
                elem.set_state(Gst.State.NULL)
        pipeline.run_dispose()

    state_change, _, _ = pipeline.get_state(Gst.CLOCK_TIME_NONE)
    if state_change != Gst.StateChangeReturn.SUCCESS:
        cleanup()
        raise Exception(f"\n{__name__}: State change was not successful")

    success, duration_ns = pipeline.query_duration(Gst.Format.TIME)
    if not success:
        duration_ns = 0

    # Take timestamp after 2 seconds or earlier, if duration is shorter
    duration_ms = duration_ns / 1e6
    metadata["duration"] = duration_ms
    timestamp_ms = min(2000, int(0.5 * duration_ms))

    pipeline.seek_simple(
        Gst.Format.TIME,
        Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
        int(timestamp_ms) * Gst.MSECOND,
    )

    pipeline.set_state(Gst.State.PLAYING)
    sample = appsink.emit("try-pull-sample", 2 * Gst.SECOND)

    if sample is None and timestamp_ms != 0:
        # Accurate mid-stream seeks fail on some GIF-like MP4s (OpenH264 B-frames).
        pipeline.seek_simple(
            Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0
        )
        sample = appsink.emit("try-pull-sample", 2 * Gst.SECOND)

    if sample is None:
        cleanup()
        raise Exception(f"\n{__name__}: Failed to retrieve sample")

    buffer = sample.get_buffer()
    success, mapinfo = buffer.map(Gst.MapFlags.READ)

    if not success:
        cleanup()
        raise Exception(f"\n{__name__}: Failed to map buffer")

    bytes_ = bytes(mapinfo.data)
    buffer.unmap(mapinfo)

    cleanup()

    if output is not None:
        output.write_bytes(bytes_)
    return bytes_, metadata
