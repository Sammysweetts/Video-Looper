import re
import subprocess
import tempfile
from pathlib import Path

import streamlit as st
import imageio_ffmpeg

st.set_page_config(page_title="Video Looper (No Re-encode)", layout="centered")


def ffmpeg_exe() -> str:
    # imageio-ffmpeg ships/downloads a static ffmpeg binary usable on Streamlit Cloud.
    return imageio_ffmpeg.get_ffmpeg_exe()


def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    """Run a subprocess command and return (returncode, stdout, stderr)."""
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr


def human_mb(n: int) -> str:
    return f"{n / (1024 * 1024):.2f} MB"


def sec_to_timecode(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"  # HH:MM:SS.mmm


def timecode_to_sec(tc: str) -> float:
    """
    Parse HH:MM:SS(.mmm) into seconds.
    Accepts also MM:SS(.mmm) or SS(.mmm).
    """
    s = tc.strip()
    if not s:
        raise ValueError("Empty timecode")

    parts = s.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        mm = int(parts[0])
        ss = float(parts[1])
        return mm * 60 + ss
    if len(parts) == 3:
        hh = int(parts[0])
        mm = int(parts[1])
        ss = float(parts[2])
        return hh * 3600 + mm * 60 + ss
    raise ValueError("Invalid timecode format")


def get_duration_seconds_via_ffmpeg(in_path: str) -> float:
    """
    Get duration by parsing `ffmpeg -i` stderr output.
    Avoids needing ffprobe as an extra dependency/binary.
    """
    cmd = [ffmpeg_exe(), "-hide_banner", "-i", in_path]
    rc, out, err = run_cmd(cmd)
    text = (out or "") + "\n" + (err or "")

    # Example: Duration: 00:01:23.45,
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not m:
        raise RuntimeError("Could not read video duration (ffmpeg did not report Duration).")

    hh = int(m.group(1))
    mm = int(m.group(2))
    ss = float(m.group(3))
    return hh * 3600 + mm * 60 + ss


def extract_clip_stream_copy(in_path: str, out_path: str, start_s: float, end_s: float) -> None:
    """
    Extract [start_s, end_s] without re-encoding (stream copy).

    NOTE: With -c copy, cuts are typically keyframe-aligned (not frame-accurate) depending
    on the source. This preserves original quality + fps (no re-encode).
    """
    if end_s <= start_s:
        raise ValueError("End time must be greater than start time.")

    dur = end_s - start_s

    cmd = [
        ffmpeg_exe(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        # Fast seek (keyframe aligned) while preserving stream copy
        "-ss",
        f"{start_s:.6f}",
        "-i",
        in_path,
        "-t",
        f"{dur:.6f}",
        "-c",
        "copy",
        # Helps players start quickly
        "-movflags",
        "+faststart",
        # Avoid negative timestamps in some sources
        "-avoid_negative_ts",
        "make_zero",
        out_path,
    ]
    rc, _, err = run_cmd(cmd)
    if rc != 0:
        raise RuntimeError(err.strip() or "ffmpeg clip extraction failed.")


def loop_video_stream_copy_concat_demuxer(in_path: str, out_path: str, loops: int) -> None:
    """
    Loop by concatenating the same file N times using concat demuxer with stream copy.
    This keeps original encoded video/audio exactly (no re-encode), preserving quality and fps.
    """
    if loops < 1:
        raise ValueError("loops must be >= 1")

    list_file = Path(out_path).with_suffix(".concat.txt")
    in_abs = str(Path(in_path).resolve())
    in_abs_norm = in_abs.replace("\\", "/")

    with open(list_file, "w", encoding="utf-8") as f:
        for _ in range(loops):
            f.write(f"file '{in_abs_norm}'\n")

    cmd = [
        ffmpeg_exe(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        out_path,
    ]

    rc, _, err = run_cmd(cmd)

    try:
        list_file.unlink(missing_ok=True)
    except Exception:
        pass

    if rc != 0:
        raise RuntimeError(err.strip() or "ffmpeg concat demuxer failed.")


def loop_video_stream_copy_ts_fallback(in_path: str, out_path: str, loops: int) -> None:
    """
    Fallback method (still no re-encode) using MPEG-TS intermediate.
    Sometimes more tolerant than direct MP4 concat demuxer.
    """
    if loops < 1:
        raise ValueError("loops must be >= 1")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ts_path = td / "segment.ts"

        cmd1 = [
            ffmpeg_exe(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(in_path),
            "-c",
            "copy",
            "-bsf:v",
            "h264_mp4toannexb",
            "-f",
            "mpegts",
            str(ts_path),
        ]
        rc1, _, err1 = run_cmd(cmd1)
        if rc1 != 0:
            raise RuntimeError(err1.strip() or "ffmpeg TS remux step failed.")

        concat_input = "concat:" + "|".join([str(ts_path)] * loops)

        cmd2 = [
            ffmpeg_exe(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            concat_input,
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        rc2, _, err2 = run_cmd(cmd2)
        if rc2 != 0:
            raise RuntimeError(err2.strip() or "ffmpeg TS concat step failed.")


def loop_video_no_reencode(in_path: str, out_path: str, loops: int) -> None:
    """Try best no-reencode approach first; if it fails, attempt a fallback method."""
    try:
        loop_video_stream_copy_concat_demuxer(in_path, out_path, loops)
    except Exception as e1:
        try:
            loop_video_stream_copy_ts_fallback(in_path, out_path, loops)
        except Exception as e2:
            raise RuntimeError(
                "Could not loop the video with stream-copy (no re-encode).\n\n"
                "This usually happens if the container/codec parameters cannot be concatenated safely.\n\n"
                f"First attempt error:\n{e1}\n\nFallback attempt error:\n{e2}"
            )


# ---------------- UI ----------------

st.title("Video Looper (original quality, original FPS)")
st.caption(
    "Loop a **selected portion** of your video **without re-encoding** (`ffmpeg -c copy`). "
    "That preserves **original quality** and **original frame rate**."
)

uploaded = st.file_uploader("Upload a video file", type=None)

loops = st.number_input(
    "Number of loops (X times)",
    min_value=1,
    value=2,
    step=1,
    help="Output duration and file size grow ~linearly with X.",
)

if uploaded is not None:
    in_name = Path(uploaded.name).name
    stem = Path(in_name).stem

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        in_path = td / in_name
        in_path.write_bytes(uploaded.getvalue())

        st.subheader("Original")
        st.video(uploaded.getvalue())
        st.write(f"Uploaded size: **{human_mb(len(uploaded.getvalue()))}**")

        try:
            duration_s = get_duration_seconds_via_ffmpeg(str(in_path))
        except Exception as e:
            st.error(str(e))
            st.stop()

        # Session-state defaults
        if "range_start" not in st.session_state:
            st.session_state.range_start = 0.0
        if "range_end" not in st.session_state:
            st.session_state.range_end = float(duration_s)

        # Clamp session state if a new video is uploaded
        st.session_state.range_start = float(max(0.0, min(st.session_state.range_start, duration_s)))
        st.session_state.range_end = float(max(0.0, min(st.session_state.range_end, duration_s)))
        if st.session_state.range_end <= st.session_state.range_start:
            st.session_state.range_start = 0.0
            st.session_state.range_end = float(duration_s)

        st.subheader("Select Range (to loop)")

        top = st.container(border=True)
        with top:
            c1, c2, c3 = st.columns([1.2, 1.2, 1.0], vertical_alignment="bottom")
            with c1:
                start_tc = st.text_input(
                    "Start (HH:MM:SS.mmm)",
                    value=sec_to_timecode(st.session_state.range_start),
                    key="start_tc",
                )
            with c2:
                end_tc = st.text_input(
                    "End (HH:MM:SS.mmm)",
                    value=sec_to_timecode(st.session_state.range_end),
                    key="end_tc",
                )
            with c3:
                apply_tc = st.button("Apply timecodes", use_container_width=True)

            if apply_tc:
                try:
                    s = timecode_to_sec(st.session_state.start_tc)
                    e = timecode_to_sec(st.session_state.end_tc)
                    s = max(0.0, min(s, duration_s))
                    e = max(0.0, min(e, duration_s))
                    if e <= s:
                        raise ValueError("End time must be greater than start time.")
                    st.session_state.range_start = float(s)
                    st.session_state.range_end = float(e)
                    st.rerun()
                except Exception as ex:
                    st.error(f"Invalid timecodes: {ex}")

            # Slider (acts like a simple timeline range selector)
            r = st.slider(
                "Timeline range (seconds)",
                min_value=0.0,
                max_value=float(duration_s),
                value=(float(st.session_state.range_start), float(st.session_state.range_end)),
                step=0.1,
            )
            st.session_state.range_start, st.session_state.range_end = float(r[0]), float(r[1])

            c4, c5, c6 = st.columns(3)
            with c4:
                st.metric("Video duration", sec_to_timecode(duration_s))
            with c5:
                st.metric("Selection start", sec_to_timecode(st.session_state.range_start))
            with c6:
                st.metric("Selection end", sec_to_timecode(st.session_state.range_end))

            st.caption(
                "Preview + looping uses **stream copy** (no re-encode). "
                "For many videos, cuts are **keyframe-aligned**, so the preview/output may start slightly earlier than the exact frame."
            )

        # Preview selection
        preview_col1, preview_col2 = st.columns([1.0, 1.0], vertical_alignment="bottom")
        with preview_col1:
            preview = st.button("Preview selected portion", type="secondary", use_container_width=True)
        with preview_col2:
            process = st.button("Create looped video", type="primary", use_container_width=True)

        if preview:
            clip_name = f"{stem}_clip_preview.mp4"
            clip_path = td / clip_name
            try:
                with st.spinner("Building preview (no re-encode)…"):
                    extract_clip_stream_copy(
                        str(in_path),
                        str(clip_path),
                        float(st.session_state.range_start),
                        float(st.session_state.range_end),
                    )
                clip_bytes = clip_path.read_bytes()
                st.subheader("Preview (selected portion)")
                st.video(clip_bytes)
                st.write(f"Preview size: **{human_mb(len(clip_bytes))}**")
            except Exception as e:
                st.error(str(e))

        if process:
            out_name = f"{stem}_looped_{int(loops)}x.mp4"
            clip_name = f"{stem}_clip_for_loop.mp4"
            clip_path = td / clip_name
            out_path = td / out_name

            try:
                with st.spinner("Extracting selected portion (no re-encode)…"):
                    extract_clip_stream_copy(
                        str(in_path),
                        str(clip_path),
                        float(st.session_state.range_start),
                        float(st.session_state.range_end),
                    )

                with st.spinner("Looping selection (no re-encode)…"):
                    loop_video_no_reencode(str(clip_path), str(out_path), int(loops))

                out_bytes = out_path.read_bytes()

                st.success("Done.")

                st.subheader("Preview (looped output)")
                st.video(out_bytes)
                st.write(f"Output size: **{human_mb(len(out_bytes))}**")

                st.download_button(
                    label="Download looped video",
                    data=out_bytes,
                    file_name=out_name,
                    mime="video/mp4",
                )
            except Exception as e:
                st.error(str(e))

st.divider()
st.markdown(
    """
#### Notes / constraints (important for “no quality loss”)
- This app loops videos using **stream copy** (no re-encode). That’s what preserves:
  1) **Original quality**  
  2) **Original frame rate**  
- Range selection is also done with **stream copy**. For many codecs, **cuts are keyframe-aligned** (not frame-accurate) unless you re-encode.
- Stream-copy concatenation can fail for some files (codec/container edge cases). The app tries a fallback, but **some inputs still cannot be concatenated without re-encoding**.
- Very large **X** will produce very large outputs and may exceed Streamlit Cloud resource limits.
"""
)
