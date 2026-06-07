#!/usr/bin/env bash
#
# stream.sh — capture the Raspberry Pi OS screen and push it to an RTMP server.
#
# Mirrors what the PAT Signage kiosk shows (Chromium fullscreen) so the live
# display can be monitored remotely. Works on both Raspberry Pi OS graphical
# stacks: X11 (Bullseye / older) via ffmpeg x11grab, and Wayland (Bookworm on
# Pi 4/5, wayfire/labwc) via wf-recorder.
#
# Config is read from the device .env (~/.config/pat-sig/dsm/.env), but every
# value can be overridden by an environment variable, and RTMP_URL may also be
# passed as the first argument.
#
#   RTMP_URL           rtmp://host/app/key       (required)
#   STREAM_FPS         frames per second         (default 25)
#   STREAM_RESOLUTION  WxH to scale to, e.g.     (default: native screen size)
#                      1280x720; empty = native
#   STREAM_BITRATE     video bitrate             (default 2500k)
#   STREAM_ENCODER     libx264 | h264_v4l2m2m    (default libx264)
#                      (h264_v4l2m2m = Pi hardware H.264, Pi 4 and earlier)
#   STREAM_AUDIO       silent | pulse | none     (default silent)
#                      silent = inject a quiet AAC track (RTMP ingests that
#                      reject video-only streams, e.g. YouTube, need this)
#   DISPLAY            X11 display               (default :0, auto-detected)
#
# Examples:
#   ./stream.sh rtmp://10.0.0.5/live/ps12
#   STREAM_BITRATE=1500k STREAM_RESOLUTION=1280x720 ./stream.sh
#   STREAM_ENCODER=h264_v4l2m2m ./stream.sh rtmp://media/live/key   # Pi 4
#
set -euo pipefail

ENV_FILE="${PAT_SIG_ENV:-$HOME/.config/pat-sig/dsm/.env}"

log() { printf '[pat-sig][stream] %s\n' "$*" >&2; }
error() { printf '[pat-sig][stream][error] %s\n' "$*" >&2; exit 1; }

# --- Load config from .env (env vars already set win) ----------------------
# Only pull in the keys we care about so a malformed .env line can't run code.
if [[ -f "$ENV_FILE" ]]; then
  while IFS='=' read -r key value; do
    case "$key" in
      RTMP_URL|STREAM_FPS|STREAM_RESOLUTION|STREAM_BITRATE|STREAM_ENCODER|STREAM_AUDIO)
        # Don't clobber a value the caller already exported.
        [[ -n "${!key:-}" ]] || printf -v "$key" '%s' "$value"
        ;;
    esac
  done < <(grep -E '^[A-Z_]+=' "$ENV_FILE" || true)
fi

# CLI arg overrides everything for the URL.
RTMP_URL="${1:-${RTMP_URL:-}}"

FPS="${STREAM_FPS:-25}"
RESOLUTION="${STREAM_RESOLUTION:-}"
BITRATE="${STREAM_BITRATE:-2500k}"
ENCODER="${STREAM_ENCODER:-libx264}"
AUDIO="${STREAM_AUDIO:-silent}"

[[ -n "$RTMP_URL" ]] || error "RTMP_URL not set (pass as arg, env, or in $ENV_FILE)"
command -v ffmpeg >/dev/null 2>&1 || error "ffmpeg not found — install it: sudo apt install ffmpeg"

# --- Detect the graphical backend (same logic as the kiosk launcher) -------
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -z "${WAYLAND_DISPLAY:-}" && -S "$XDG_RUNTIME_DIR/wayland-0" ]]; then
  export WAYLAND_DISPLAY=wayland-0
fi
if [[ -z "${WAYLAND_DISPLAY:-}" && -z "${DISPLAY:-}" ]]; then
  export DISPLAY=:0
fi

# --- Build the video encoder args ------------------------------------------
# libx264 takes preset/tune; the V4L2 hardware encoder does not.
enc_args=(-c:v "$ENCODER")
if [[ "$ENCODER" == "libx264" ]]; then
  enc_args+=(-preset ultrafast -tune zerolatency)
fi
enc_args+=(
  -pix_fmt yuv420p
  -b:v "$BITRATE" -maxrate "$BITRATE" -bufsize "$BITRATE"
  -g "$((FPS * 2))"   # keyframe every ~2s — good for RTMP seeking/recovery
)

# Optional scale filter (native size when RESOLUTION is empty).
scale_args=()
if [[ -n "$RESOLUTION" ]]; then
  scale_args=(-vf "scale=${RESOLUTION/x/:}")
fi

# --- Audio input + codec (shared by both backends) -------------------------
# Index of the video input is always 0; audio (when present) is input 1.
audio_in=()
audio_args=(-an)
case "$AUDIO" in
  silent)
    audio_in=(-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=44100)
    audio_args=(-c:a aac -b:a 128k -map 0:v:0 -map 1:a:0)
    ;;
  pulse)
    audio_in=(-f pulse -i default)
    audio_args=(-c:a aac -b:a 128k -map 0:v:0 -map 1:a:0)
    ;;
  none) ;;
  *) error "STREAM_AUDIO must be silent|pulse|none (got '$AUDIO')" ;;
esac

run_ffmpeg_x11() {
  log "X11 capture on DISPLAY=$DISPLAY -> $RTMP_URL (${FPS}fps, $BITRATE, $ENCODER)"
  # x11grab auto-detects the screen size when -video_size is omitted.
  exec ffmpeg -hide_banner -loglevel warning \
    -f x11grab -framerate "$FPS" -i "$DISPLAY" \
    "${audio_in[@]}" \
    "${scale_args[@]}" \
    "${enc_args[@]}" \
    "${audio_args[@]}" \
    -f flv "$RTMP_URL"
}

run_wayland() {
  command -v wf-recorder >/dev/null 2>&1 || \
    error "Wayland session detected but wf-recorder not found — install it: sudo apt install wf-recorder"
  log "Wayland capture (WAYLAND_DISPLAY=$WAYLAND_DISPLAY) -> $RTMP_URL (${FPS}fps, $BITRATE, $ENCODER)"
  # wf-recorder encodes via ffmpeg internally; --muxer flv targets RTMP.
  # Audio is best-effort: wf-recorder's --audio captures the system default,
  # so the silent-track trick (X11 only) doesn't apply here.
  wf_audio=()
  [[ "$AUDIO" == "pulse" ]] && wf_audio=(--audio)
  exec wf-recorder \
    --muxer=flv \
    --codec="$ENCODER" \
    --framerate="$FPS" \
    -p preset=ultrafast -p tune=zerolatency \
    -p b:v="$BITRATE" -p maxrate="$BITRATE" -p bufsize="$BITRATE" \
    "${wf_audio[@]}" \
    --file="$RTMP_URL"
}

if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
  run_wayland
else
  run_ffmpeg_x11
fi
