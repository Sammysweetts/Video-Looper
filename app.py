import re
import subprocess
import tempfile
from pathlib import Path

import streamlit as st
import imageio_ffmpeg


st.set_page_config(page_title="Video Looper (No Re-encode)", layout="centered")


def ffmpeg_exe() -> str:
    # imageio-ffmpeg provides a usable ffmpeg binary on Streamlit Cloud.
    return imageio_ffmpeg.get_ffmpeg_exe()


def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr


def parse_duration_seconds(ffmpeg_stderr: str) -> float | None:
    # Example: Duration: 00:01:23.45,
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?),", ffmpeg_stderr)
    if not m:
        return None
    hh, mm, ss = m.groups()
    return int(hh) * 3600 + int(mm) * 60 + float(ss)


def get_duration_seconds(in_path: str) -> float | None:
    # ffmpeg prints duration to stderr; return code is typically non-zero for probe-only runs.
    cmd = [ffmpeg_exe(), "-hide_banner", "-i", in_path]
    rc, out, err = run_cmd(cmd)
    _ = (rc, out)  # unused
    return parse_duration_seconds(err)


def human_mb(n: int) -> str:
    return f"{n / (1024 * 1024):.2f} MB"


def fmt_time(t: float) -> str:
    if t < 0:
        t = 0
    hh = int(t // 3600)
    mm = int((t % 3600) // 60)
    ss = t % 60
    return f"{hh:02d}:{mm:02d}:{ss:06.3f}"


def trim_video_stream_copy(in_path: str, out_path: str, start_s: float, end_s: float) -> None:
    """
    Trim a portion without re-encoding (-c copy).
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
        # Seek + trim
        "-ss",
        f"{start_s:.6f}",
        "-to",
        f"{end_s:.6f}",
        "-i",
        in_path,
        # Stream copy to preserve original quality and fps
        "-c",
        "copy",
        # Safer timestamps for later concatenation
        "-fflags",
        "+genpts",
        "-avoid_negative_ts",
        "make_zero",
        # Faster web preview
        "-movflags",
        "+faststart",
        out_path,
    ]

    rc, _, err = run_cmd(cmd)
    if rc != 0:
        raise RuntimeError(err.strip() or "ffmpeg trim failed.")


def loop_video_stream_copy_concat_demuxer(in_path: str, out_path: str, loops: int) -> None:
    """
    Loop by concatenating the same file N times using concat demuxer with stream copy.
    This keeps encoded audio/video exactly (no re-encode), preserving quality and fps.
    """
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
    """
    Fallback method (still no re-encode) using MPEG-TS intermediate.
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

if "trim_preview_bytes" not in st.session_state:
    st.session_state.trim_preview_bytes = None
if "trim_range" not in st.session_state:
    st.session_state.trim_range = None

if uploaded is not None:
    st.subheader("Preview (original)")
    original_bytes = uploaded.getvalue()
    st.video(original_bytes)
    st.write(f"Uploaded size: **{human_mb(len(original_bytes))}**")

    # Save input once to temp to probe duration and reuse
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
    default_end = min(duration, 5.0)
    start_s, end_s = st.slider(
        "Range (seconds)",
        min_value=0.0,
        max_value=float(duration),
        value=(0.0, float(default_end)),
        step=0.1,
        help="Select the portion that will be looped.",
    )
    st.write(f"Selected: **{fmt_time(start_s)} → {fmt_time(end_s)}** (length: **{end_s - start_s:.3f}s**)")

    col1, col2 = st.columns(2)
    with col1:
        preview_btn = st.button("Preview selected portion", type="secondary")
    with col2:
        process_btn = st.button("Create looped video", type="primary")

    def build_trim_preview_bytes() -> bytes:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            in_name2 = Path(uploaded.name).name
            in_path2 = td / in_name2
            in_path2.write_bytes(original_bytes)

            trim_path = td / f"{Path(in_name2).stem}_trim.mp4"
            trim_video_stream_copy(str(in_path2), str(trim_path), float(start_s), float(end_s))
            return trim_path.read_bytes()

    if preview_btn:
        with st.spinner("Creating preview (no re-encode)…"):
            trim_bytes = build_trim_preview_bytes()
        st.session_state.trim_preview_bytes = trim_bytes
        st.session_state.trim_range = (float(start_s), float(end_s))

    if st.session_state.trim_preview_bytes is not None:
        st.subheader("Preview (selected portion)")
        st.video(st.session_state.trim_preview_bytes)

    if process_btn:
        # Ensure we have a preview clip for the *current* selection; if not, create it.
        current_range = (float(start_s), float(end_s))
        if st.session_state.trim_preview_bytes is None or st.session_state.trim_range != current_range:
            with st.spinner("Preparing selected portion (no re-encode)…"):
                st.session_state.trim_preview_bytes = build_trim_preview_bytes()
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
