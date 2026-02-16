import os
import shlex
import subprocess
import tempfile
from pathlib import Path

import streamlit as st
import imageio_ffmpeg


st.set_page_config(page_title="Video Looper (No Re-encode)", layout="centered")


def ffmpeg_exe() -> str:
    # imageio-ffmpeg ships/downlaods a static ffmpeg binary usable on Streamlit Cloud.
    return imageio_ffmpeg.get_ffmpeg_exe()


def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    """Run a subprocess command and return (returncode, stdout, stderr)."""
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr


def loop_video_stream_copy_concat_demuxer(in_path: str, out_path: str, loops: int) -> None:
    """
    Loop by concatenating the same file N times using concat demuxer with stream copy.
    This keeps original encoded video/audio exactly (no re-encode), preserving quality and fps.
    """
    if loops < 1:
        raise ValueError("loops must be >= 1")

    # concat demuxer needs a text file listing inputs
    list_file = Path(out_path).with_suffix(".concat.txt")
    in_abs = str(Path(in_path).resolve())

    # Use forward slashes to reduce escaping issues on some platforms
    in_abs_norm = in_abs.replace("\\", "/")

    with open(list_file, "w", encoding="utf-8") as f:
        for _ in range(loops):
            # Quote path (concat demuxer expects: file '...').
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
        # Stream copy => original quality + original fps (no re-encoding)
        "-c",
        "copy",
        # Makes MP4 start faster in browser previews
        "-movflags",
        "+faststart",
        out_path,
    ]

    rc, _, err = run_cmd(cmd)

    # Clean up list file
    try:
        list_file.unlink(missing_ok=True)
    except Exception:
        pass

    if rc != 0:
        raise RuntimeError(err.strip() or "ffmpeg concat demuxer failed.")


def loop_video_stream_copy_ts_fallback(in_path: str, out_path: str, loops: int) -> None:
    """
    Fallback method (still no re-encode) using MPEG-TS intermediate.
    This is sometimes more tolerant than direct MP4 concat demuxer.

    Note: This can fail for some codecs; we attempt it only as a fallback.
    """
    if loops < 1:
        raise ValueError("loops must be >= 1")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ts_path = td / "segment.ts"

        # Create TS from input with stream copy (no re-encode).
        # h264_mp4toannexb is commonly needed for H.264 in MP4 -> TS.
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

        # Concat TS and remux back to MP4 (still stream copy).
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
            # Fix AAC when coming from TS (harmless if not applicable in some cases)
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
    """
    Try best no-reencode approach first; if it fails, attempt a fallback method.
    """
    try:
        loop_video_stream_copy_concat_demuxer(in_path, out_path, loops)
    except Exception as e1:
        # Fallback attempt
        try:
            loop_video_stream_copy_ts_fallback(in_path, out_path, loops)
        except Exception as e2:
            raise RuntimeError(
                "Could not loop the video with stream-copy (no re-encode).\n\n"
                "This usually happens if the container/codec parameters cannot be concatenated safely.\n\n"
                f"First attempt error:\n{e1}\n\nFallback attempt error:\n{e2}"
            )


def human_mb(n: int) -> str:
    return f"{n / (1024 * 1024):.2f} MB"


st.title("Video Looper (original quality, original FPS)")
st.caption(
    "This app loops a video **without re-encoding** (`ffmpeg -c copy`), so output quality and frame rate remain the same."
)

uploaded = st.file_uploader("Upload a video file", type=None)

loops = st.number_input(
    "Number of loops (X times)",
    min_value=1,
    value=2,
    step=1,
    help="Output duration and file size grow ~linearly with X.",
)

colA, colB = st.columns(2)
with colA:
    process = st.button("Create looped video", type="primary", disabled=uploaded is None)
with colB:
    st.write("")  # spacing


if uploaded is not None:
    st.subheader("Preview (original)")
    st.video(uploaded.getvalue())
    st.write(f"Uploaded size: **{human_mb(len(uploaded.getvalue()))}**")

if process and uploaded is not None:
    in_name = Path(uploaded.name).name
    stem = Path(in_name).stem

    # Always output MP4 for browser-friendly preview/download.
    out_name = f"{stem}_looped_{int(loops)}x.mp4"

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        in_path = td / in_name
        out_path = td / out_name

        # Save upload to disk
        in_path.write_bytes(uploaded.getvalue())

        with st.spinner("Processing (no re-encode)…"):
            loop_video_no_reencode(str(in_path), str(out_path), int(loops))

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
- This app loops videos using **stream copy** (no re-encode). That’s what preserves:
  1) **Original quality**  
  2) **Original frame rate**  
- Stream-copy concatenation can fail for some files (codec/container edge cases). The app tries a fallback, but **some inputs still cannot be concatenated without re-encoding**.
- Very large **X** will produce very large outputs and may exceed Streamlit Cloud resource limits.
"""
)
