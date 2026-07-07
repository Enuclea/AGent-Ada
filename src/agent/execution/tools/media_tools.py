import os
from pathlib import Path
import json

def youtube_to_mp3(url: str) -> str:
    """Downloads the audio from a YouTube video and converts it to a high-quality MP3 file.
    
    Args:
        url: The full YouTube video URL (e.g. 'https://www.youtube.com/watch?v=dQw4w9WgXcQ').
    """
    import urllib.parse
    import yt_dlp

    proj_root = Path(__file__).resolve().parent.parent.parent.parent
    target_dir = proj_root / "share" / "data" / "mp3"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return f"Error: Failed to create directories: {e}"

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': str(target_dir / '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            mp3_path = Path(os.path.splitext(filename)[0] + '.mp3')
            
            if not mp3_path.exists():
                # Fallback check if it was already converted or renamed slightly
                possible_mp3 = Path(filename).with_suffix('.mp3')
                if possible_mp3.exists():
                    mp3_path = possible_mp3
                else:
                    return f"Error: Failed to locate converted MP3 file at {mp3_path}"
                    
            relative_url = f"/files/mp3/{urllib.parse.quote(mp3_path.name)}"
            # Return matching structure for the client download gateway
            return (
                f"Successfully downloaded and converted video to MP3.\n\n"
                f"🎵 **Song Title**: {info.get('title', mp3_path.stem)}\n"
                f"📂 **File Location**: {mp3_path}\n"
                f"🔗 **Download URL**: https://10.250.1.200:8443{relative_url}"
            )
    except Exception as e:
        return f"Error downloading and converting video: {e}"
