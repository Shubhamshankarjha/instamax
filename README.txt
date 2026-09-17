InstaMax v5 - full source-variant inspector

What changed:
- Shows every distinct accessible video representation exposed by yt-dlp.
- Keeps multiple bitrate variants at the same resolution instead of collapsing them to 2160/1440/1080/720/480/360 tiers.
- Displays exact dimensions, IDM-style resolution label, bitrate, estimated size, FPS, codec, extension, and whether audio is already included.
- Lets the user select an exact format_id.
- Downloads that selected source variant. If it is video-only, InstaMax muxes the best accessible audio with ffmpeg using stream copy, without re-encoding the video.
- Keeps the download in the same tab flow.

Run on Windows:
1. Extract this folder.
2. Double-click start.bat.
3. Open http://127.0.0.1:8787 if it does not open automatically.
4. Paste a real public Instagram URL and Analyze URL.

Requirements:
- Python 3.10+
- Internet access
- requirements.txt installs Flask, yt-dlp and imageio-ffmpeg.
