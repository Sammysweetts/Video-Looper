import re
import subprocess
import tempfile
from pathlib import Path

import streamlit as st
import imageio_ffmpeg


st.set_page_config(page_title="Video Looper (No Re-encode)", layout="centered")


def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr


def parse_duration_seconds(ffmpeg_stderr: str) -> float | None:
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?),", ffmpeg_stderr)
    if not m:
        return None
    hh, mm, ss = m.groups()
    return int(hh) * 3600 + int(mm) * 60 + float(ss)


def get_duration_seconds(in_path: str) -> float | None:
    cmd = [ffmpeg_exe(), "-hide_banner", "-i", in_path]
    rc, out, err = run_cmd(cmd)
    _ = (rc, out)
    return parse_duration_seconds(err)


def human_mb(n: int) -> str:
    return f"{n / (1024 * 1024):.2f} MB"


def fmt_time(t: float) -> str:
    if t < 0:
        t = 0.0
    hh = int(t // 3600)
    mm = int((t % 3600) // 60)
    ss = t % 60
    return f"{hh:02d}:{mm:02d}:{ss:06.3f}"


def parse_timecode_to_seconds(tc: str) -> float:
    """
    Accepts:
      - SS[.ms]
      - MM:SS[.ms]
      - HH:MM:SS[.ms]
    """
    s = tc.strip()
    if not s:
        raise ValueError("Empty timestamp")

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

    raise ValueError("Invalid timestamp format")


def trim_video_stream_copy(in_path: str, out_path: str, start_s: float, end_s: float) -> None:
    """
    Trim without re-encoding (-c copy).
    Note: With stream-copy, cut points may align to nearest keyframes. Preview shows the actual result.
    """
    if end_s <= start_s:
        raise ValueError("End time must be greater than start time.")

    cmd = [
        ffmpeg_exe(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_s:.6f}",
        "-to",
        f"{end_s:.6f}",
        "-i",
        in_path,
        "-c",
        "copy",
        "-fflags",
        "+genpts",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        out_path,
    ]
    rc, _, err = run_cmd(cmd)
    if rc != 0:
        raise RuntimeError(err.strip() or "ffmpeg trim failed.")


def loop_video_stream_copy_concat_demuxer(in_path: str, out_path: str, loops: int) -> None:
    if loops < 1:
        raise ValueError("loops must be >= 1")

    list_file = Path(out_path).with_suffix(".concat.txt")
    in_abs = str(Path(in_path).resolve()).replace("\\", "/")

    with open(list_file, "w", encoding="utf-8") as f:
        for _ in range(loops):
            f.write(f"file '{in_abs}'\n")

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
    try:
        loop_video_stream_copy_concat_demuxer(in_path, out_path, loops)
    except Exception as e1:
        try:
            loop_video_stream_copy_ts_fallback(in_path, out_path, loops)
        except Exception as e2:
            raise RuntimeError(
                "Could not loop the video with stream-copy (no re-encode).\n\n"
                f"First attempt error:\n{e1}\n\nFallback attempt error:\n{e2}"
            )


def validate_range(start_s: float, end_s: float, duration: float) -> tuple[float, float]:
    if start_s < 0 or end_s < 0:
        raise ValueError("Timestamps must be >= 0.")
    if end_s <= start_s:
        raise ValueError("End time must be greater than start time.")
    if start_s > duration:
        raise ValueError("Start time is beyond video duration.")
    if end_s > duration:
        raise ValueError("End time is beyond video duration.")
    return start_s, end_s


# ---------------- UI ----------------

st.title("Video Looper (original quality, original FPS)")
st.caption(
    "Loops a selected portion **without re-encoding** (`-c copy`), preserving original quality and frame rate."
)

uploaded = st.file_uploader("Upload a video file", type=None)

loops = st.number_input(
    "Number of loops (X times)",
    min_value=1,
    value=2,
    step=1,
    help="Output duration and file size grow ~linearly with X.",
)

# State
if "trim_preview_bytes" not in st.session_state:
    st.session_state.trim_preview_bytes = None
if "trim_range" not in st.session_state:
    st.session_state.trim_range = None
if "file_sig" not in st.session_state:
    st.session_state.file_sig = None

if uploaded is None:
    st.stop()

original_bytes = uploaded.getvalue()
file_sig = (uploaded.name, len(original_bytes))

# Reset selection/preview when a different file is uploaded
if st.session_state.file_sig != file_sig:
    st.session_state.file_sig = file_sig
    st.session_state.trim_preview_bytes = None
    st.session_state.trim_range = None
    # (Re)initialize these later once we know duration
    for k in ["range_slider", "start_tc", "end_tc", "start_s", "end_s"]:
        if k in st.session_state:
            del st.session_state[k]

st.subheader("Preview (original)")
st.video(original_bytes)
st.write(f"Uploaded size: **{human_mb(len(original_bytes))}**")

# Probe duration (needs a file)
with tempfile.TemporaryDirectory() as td_probe:
    td_probe = Path(td_probe)
    in_name = Path(uploaded.name).name
    in_path = td_probe / in_name
    in_path.write_bytes(original_bytes)
    duration = get_duration_seconds(str(in_path))

if duration is None:
    st.error("Could not read video duration. Please try a different file.")
    st.stop()

st.subheader("Select timestamp range to loop")

# Initialize default range once per file
if "range_slider" not in st.session_state:
    default_end = min(float(duration), 5.0)
    st.session_state.range_slider = (0.0, float(default_end))
    st.session_state.start_s = 0.0
    st.session_state.end_s = float(default_end)
    st.session_state.start_tc = fmt_time(0.0)
    st.session_state.end_tc = fmt_time(float(default_end))

# Slider (coarse)
slider_start, slider_end = st.slider(
    "Range (seconds) — slider (coarse)",
    min_value=0.0,
    max_value=float(duration),
    value=st.session_state.range_slider,
    step=0.1,
    key="range_slider",
    help="Use the slider for rough selection, then fine-tune using timestamp inputs below.",
)

# Keep state in sync after slider movement
st.session_state.start_s = float(slider_start)
st.session_state.end_s = float(slider_end)
st.session_state.start_tc = fmt_time(st.session_state.start_s)
st.session_state.end_tc = fmt_time(st.session_state.end_s)

# Timestamp inputs (fine)
col_ts1, col_ts2, col_ts3 = st.columns([1, 1, 0.8])
with col_ts1:
    st.text_input(
        "Start timestamp (HH:MM:SS.mmm or MM:SS.mmm or SS.mmm)",
        key="start_tc",
    )
with col_ts2:
    st.text_input(
        "End timestamp (HH:MM:SS.mmm or MM:SS.mmm or SS.mmm)",
        key="end_tc",
    )
with col_ts3:
    apply_ts = st.button("Apply timestamps", type="secondary")

# Apply typed timestamps -> update slider + selection state
if apply_ts:
    try:
        start_s = parse_timecode_to_seconds(st.session_state.start_tc)
        end_s = parse_timecode_to_seconds(st.session_state.end_tc)
        start_s, end_s = validate_range(start_s, end_s, float(duration))

        # Update slider + internal seconds state
        st.session_state.range_slider = (float(start_s), float(end_s))
        st.session_state.start_s = float(start_s)
        st.session_state.end_s = float(end_s)

        # Invalidate cached preview if range changed
        st.session_state.trim_preview_bytes = None
        st.session_state.trim_range = None

        st.rerun()
    except Exception as e:
        st.error(f"Invalid timestamps: {e}")

# Show selected info (based on current state)
try:
    validate_range(st.session_state.start_s, st.session_state.end_s, float(duration))
    st.write(
        f"Selected: **{fmt_time(st.session_state.start_s)} → {fmt_time(st.session_state.end_s)}** "
        f"(length: **{(st.session_state.end_s - st.session_state.start_s):.3f}s**)"
    )
except Exception as e:
    st.error(str(e))
    st.stop()

col1, col2 = st.columns(2)
with col1:
    preview_btn = st.button("Preview selected portion", type="secondary")
with col2:
    process_btn = st.button("Create looped video", type="primary")


def build_trim_preview_bytes(start_s: float, end_s: float) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        in_name2 = Path(uploaded.name).name
        in_path2 = td / in_name2
        in_path2.write_bytes(original_bytes)

        trim_path = td / f"{Path(in_name2).stem}_trim.mp4"
        trim_video_stream_copy(str(in_path2), str(trim_path), float(start_s), float(end_s))
        return trim_path.read_bytes()


current_range = (float(st.session_state.start_s), float(st.session_state.end_s))

if preview_btn:
    with st.spinner("Creating preview (no re-encode)…"):
        trim_bytes = build_trim_preview_bytes(*current_range)
    st.session_state.trim_preview_bytes = trim_bytes
    st.session_state.trim_range = current_range

if st.session_state.trim_preview_bytes is not None:
    st.subheader("Preview (selected portion)")
    st.video(st.session_state.trim_preview_bytes)

if process_btn:
    # Ensure preview clip exists for the current selection
    if st.session_state.trim_preview_bytes is None or st.session_state.trim_range != current_range:
        with st.spinner("Preparing selected portion (no re-encode)…"):
            st.session_state.trim_preview_bytes = build_trim_preview_bytes(*current_range)
            st.session_state.trim_range = current_range

    trim_bytes = st.session_state.trim_preview_bytes

    out_name = f"{Path(uploaded.name).stem}_looped_{int(loops)}x.mp4"

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        clip_path = td / "selected_clip.mp4"
        clip_path.write_bytes(trim_bytes)

        out_path = td / out_name

        with st.spinner("Looping (no re-encode)…"):
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

st.divider()
st.markdown(
    """
#### Notes / constraints (important for “no quality loss”)
- This app trims and loops using **stream copy** (no re-encode), preserving:
  1) **Original quality**  
  2) **Original frame rate**
- With stream-copy trimming, cut points may align to nearest keyframes; the **selected-portion preview shows the actual trimmed result**.
- Very large **X** will produce very large outputs and may exceed Streamlit Cloud resource limits.
"""
)
