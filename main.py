from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, HttpUrl
import yt_dlp
import ffmpeg
import os
import tempfile
import asyncio
from typing import Optional, List, Dict, Any
import logging
import shutil
from pathlib import Path
import subprocess
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="FetchVid API",
    description="YouTube video downloader with subtitle burning capabilities",
    version="1.0.0"
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request models
class FetchRequest(BaseModel):
    url: HttpUrl

class FormatInfo(BaseModel):
    quality: str
    ext: str
    format_id: str
    filesize: Optional[int] = None

class SubtitleInfo(BaseModel):
    lang: str
    lang_name: str

class DownloadRequest(BaseModel):
    url: HttpUrl
    format: FormatInfo
    subtitle_lang: Optional[str] = None

class SubtitleDownloadRequest(BaseModel):
    url: HttpUrl
    subtitle_lang: str

# Response models
class VideoInfo(BaseModel):
    title: str
    thumbnail: str
    formats: Dict[str, List[FormatInfo]]  # Grouped by extension
    subtitles: List[SubtitleInfo]
    channel: Optional[str] = None
    channel_url: Optional[str] = None
    duration: Optional[int] = None  # Duration in seconds
    view_count: Optional[int] = None
    upload_date: Optional[str] = None  # YYYYMMDD format
    description: Optional[str] = None

# yt-dlp options for extraction
YDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'extract_flat': False,
    'skip_download': True,
    'no_check_certificate': True,
}

def check_ffmpeg():
    """Check if FFmpeg is available"""
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.error("FFmpeg not found. Please install FFmpeg.")
        return False

def get_video_info(url: str) -> Dict[str, Any]:
    """Extract video information using yt-dlp"""
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return info
        except Exception as e:
            logger.error(f"Error extracting video info: {str(e)}")
            raise HTTPException(status_code=400, detail=f"Failed to extract video information: {str(e)}")

def filter_formats(formats: List[Dict]) -> Dict[str, List[FormatInfo]]:
    """Filter and organize available formats grouped by extension"""
    grouped = {
        'mp4': {},
        'webm': {},
        'audio': []
    }
    
    # First, collect all formats and group by quality
    for f in formats:
        # Skip formats without required fields
        if not f.get('format_id') or not f.get('ext'):
            continue
            
        ext = f.get('ext', '').lower()
        vcodec = f.get('vcodec', 'none')
        acodec = f.get('acodec', 'none')
        height = f.get('height')
        fps = f.get('fps', 0)
        filesize = f.get('filesize', 0) or 0
        tbr = f.get('tbr', 0) or 0  # Total bitrate
        
        # Audio only formats
        if vcodec == 'none' and acodec != 'none':
            if f.get('format_id') in ['140', '141', '171', '172', '249', '250', '251', '139']:
                grouped['audio'].append(FormatInfo(
                    quality='Audio Only',
                    ext='mp3',
                    format_id=f.get('format_id'),
                    filesize=f.get('filesize')
                ))
        # Video formats
        elif vcodec != 'none' and height:
            # Build quality string with fps if > 30
            quality = f"{height}p"
            if fps and fps > 30:
                # Convert fps to int to avoid duplicates like 60.0 and 60
                quality = f"{height}p{int(fps)}"
            
            format_info = FormatInfo(
                quality=quality,
                ext=ext,
                format_id=f.get('format_id'),
                filesize=f.get('filesize')
            )
            
            # Group by extension and quality, keeping the best one
            if ext == 'mp4':
                if quality not in grouped['mp4'] or tbr > (grouped['mp4'][quality].get('tbr', 0)):
                    grouped['mp4'][quality] = {
                        'format_info': format_info,
                        'tbr': tbr
                    }
            elif ext == 'webm':
                if quality not in grouped['webm'] or tbr > (grouped['webm'][quality].get('tbr', 0)):
                    grouped['webm'][quality] = {
                        'format_info': format_info,
                        'tbr': tbr
                    }
    
    # Convert dictionaries to lists, keeping only format_info
    result = {
        'mp4': [],
        'webm': [],
        'audio': grouped['audio']
    }
    
    # Extract format_info from dictionaries and sort by resolution
    def get_resolution(format_info):
        import re
        match = re.match(r'(\d+)p', format_info.quality)
        return int(match.group(1)) if match else 0
    
    for ext in ['mp4', 'webm']:
        if grouped[ext]:
            result[ext] = [item['format_info'] for item in grouped[ext].values()]
            result[ext].sort(key=get_resolution, reverse=True)
    
    # Remove empty groups
    result = {k: v for k, v in result.items() if v}
    
    # Ensure we have at least one audio format
    if not result.get('audio'):
        # Try to find a good audio format
        audio_format = next((f for f in formats if f.get('format_id') == '140'), None)
        if not audio_format:
            audio_format = next((f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none'), None)
        if audio_format:
            result['audio'] = [FormatInfo(
                quality='Audio Only',
                ext='mp3',
                format_id=audio_format.get('format_id'),
                filesize=audio_format.get('filesize')
            )]
    
    return result

def get_subtitles_info(subtitles: Dict) -> List[SubtitleInfo]:
    """Extract subtitle information"""
    subtitle_list = []
    
    # Popular languages to prioritize
    priority_langs = ['en', 'es', 'fr', 'de', 'ja', 'ko', 'pt', 'ru', 'it', 'nl']
    
    for lang in priority_langs:
        if lang in subtitles:
            # Get the first available subtitle format
            sub_formats = subtitles[lang]
            if sub_formats and len(sub_formats) > 0:
                lang_name = sub_formats[0].get('name', lang.upper())
                subtitle_list.append(SubtitleInfo(lang=lang, lang_name=lang_name))
    
    # Add other languages not in priority list
    for lang, sub_formats in subtitles.items():
        if lang not in priority_langs and len(subtitle_list) < 10:
            if sub_formats and len(sub_formats) > 0:
                lang_name = sub_formats[0].get('name', lang.upper())
                subtitle_list.append(SubtitleInfo(lang=lang, lang_name=lang_name))
    
    # Limit to top 5 subtitles
    return subtitle_list[:5]

@app.post("/fetch", response_model=VideoInfo)
async def fetch_video_info(request: FetchRequest):
    """Fetch video information including formats and subtitles"""
    try:
        url = str(request.url)
        logger.info(f"Fetching info for URL: {url}")
        
        info = get_video_info(url)
        
        # Extract video details
        title = info.get('title', 'Unknown Title')
        thumbnail = info.get('thumbnail', '')
        
        # Extract additional metadata
        channel = info.get('uploader', info.get('channel', None))
        channel_url = info.get('uploader_url', info.get('channel_url', None))
        duration = info.get('duration', None)
        view_count = info.get('view_count', None)
        upload_date = info.get('upload_date', None)
        description = info.get('description', '')
        
        # Truncate description to first 200 characters
        if description and len(description) > 200:
            description = description[:197] + '...'
        
        # Get formats
        formats = info.get('formats', [])
        grouped_formats = filter_formats(formats)
        
        if not grouped_formats:
            logger.warning("No suitable formats found")
            raise HTTPException(status_code=404, detail="No suitable formats found for this video")
        
        # Get subtitles
        subtitles = info.get('subtitles', {})
        automatic_captions = info.get('automatic_captions', {})
        all_subtitles = {**subtitles, **automatic_captions}
        subtitle_info = get_subtitles_info(all_subtitles)
        
        total_formats = sum(len(formats) for formats in grouped_formats.values())
        logger.info(f"Found {total_formats} formats across {len(grouped_formats)} types and {len(subtitle_info)} subtitle languages")
        
        return VideoInfo(
            title=title,
            thumbnail=thumbnail,
            formats=grouped_formats,
            subtitles=subtitle_info,
            channel=channel,
            channel_url=channel_url,
            duration=duration,
            view_count=view_count,
            upload_date=upload_date,
            description=description
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in fetch: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

async def download_video_simple(url: str, format_id: str, temp_dir: str) -> str:
    """Download video with audio"""
    try:
        video_file = os.path.join(temp_dir, "video")
        
        # Download video with audio
        download_opts = {
            'format': f'{format_id}+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': video_file,
            'quiet': True,
            'no_warnings': True,
            'no_check_certificate': True,
            'prefer_ffmpeg': True,
            'merge_output_format': 'mp4',
        }
        logger.info(f"Downloading video+audio format: {format_id}+bestaudio")
        
        # Download video
        with yt_dlp.YoutubeDL(download_opts) as ydl:
            ydl.download([url])
        
        # Find the downloaded file
        downloaded_file = None
        for file in os.listdir(temp_dir):
            if file.startswith('video') and file.endswith(('.mp4', '.webm', '.mkv', '.m4a')):
                downloaded_file = os.path.join(temp_dir, file)
                break
        
        if not downloaded_file or not os.path.exists(downloaded_file):
            raise Exception("Video file not found after download")
        
        logger.info(f"Video downloaded: {downloaded_file} ({os.path.getsize(downloaded_file)} bytes)")
        return downloaded_file
        
    except Exception as e:
        logger.error(f"Download error: {str(e)}")
        raise

async def download_audio(url: str, format_id: str, temp_dir: str) -> str:
    """Download audio and convert to MP3"""
    try:
        audio_file = os.path.join(temp_dir, "audio.mp3")
        
        download_opts = {
            'format': format_id,
            'outtmpl': os.path.join(temp_dir, 'audio.%(ext)s'),
            'quiet': False,
            'no_warnings': False,
            'no_check_certificate': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
        
        logger.info(f"Starting audio download with format: {format_id}")
        with yt_dlp.YoutubeDL(download_opts) as ydl:
            ydl.download([url])
        
        # Find the mp3 file
        for file in os.listdir(temp_dir):
            if file.endswith('.mp3'):
                audio_file = os.path.join(temp_dir, file)
                logger.info(f"Audio file found: {audio_file} ({os.path.getsize(audio_file)} bytes)")
                return audio_file
        
        raise Exception("Audio file not found after download")
        
    except Exception as e:
        logger.error(f"Audio download error: {str(e)}")
        raise

def cleanup_temp_dir(temp_dir: str):
    """Clean up temporary directory"""
    try:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.info(f"Cleaned up temp directory: {temp_dir}")
    except Exception as e:
        logger.error(f"Error cleaning up temp dir: {str(e)}")

async def stream_file(file_path: str):
    """Stream file in chunks"""
    chunk_size = 1024 * 1024  # 1MB chunks
    try:
        with open(file_path, 'rb') as file:
            while chunk := file.read(chunk_size):
                yield chunk
    except Exception as e:
        logger.error(f"Error streaming file: {str(e)}")
        raise

@app.post("/download-subtitle")
async def download_subtitle(request: SubtitleDownloadRequest):
    """Download subtitle file only"""
    temp_dir = tempfile.mkdtemp()
    
    try:
        url = str(request.url)
        subtitle_lang = request.subtitle_lang
        
        logger.info(f"Downloading subtitle for language: {subtitle_lang}")
        
        # Get video info for filename
        info = get_video_info(url)
        title = info.get('title', 'video')
        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip()[:50]
        
        # Download options for subtitle only
        download_opts = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': [subtitle_lang],
            'subtitlesformat': 'srt/vtt/best',
            'outtmpl': os.path.join(temp_dir, 'subtitle'),
            'quiet': True,
            'no_warnings': True,
        }
        
        # Download subtitle
        with yt_dlp.YoutubeDL(download_opts) as ydl:
            ydl.download([url])
        
        # Find the subtitle file
        subtitle_file = None
        for file in os.listdir(temp_dir):
            if file.endswith(('.srt', '.vtt')) and subtitle_lang in file:
                subtitle_file = os.path.join(temp_dir, file)
                break
        
        if not subtitle_file:
            raise HTTPException(status_code=404, detail=f"Subtitle not found for language: {subtitle_lang}")
        
        # Convert VTT to SRT if needed
        if subtitle_file.endswith('.vtt'):
            srt_file = subtitle_file.replace('.vtt', '.srt')
            # Simple VTT to SRT conversion
            with open(subtitle_file, 'r', encoding='utf-8') as vtt:
                content = vtt.read()
            # Remove WEBVTT header and convert timestamps
            content = content.replace('WEBVTT\n\n', '')
            content = content.replace('.', ',')  # VTT uses . for milliseconds, SRT uses ,
            with open(srt_file, 'w', encoding='utf-8') as srt:
                srt.write(content)
            subtitle_file = srt_file
        
        filename = f"{safe_title}.{subtitle_lang}.srt"
        
        # Clean up temp dir after response
        def cleanup():
            cleanup_temp_dir(temp_dir)
        
        return FileResponse(
            subtitle_file,
            media_type="text/plain",
            filename=filename,
            background=BackgroundTask(cleanup)
        )
        
    except HTTPException:
        cleanup_temp_dir(temp_dir)
        raise
    except Exception as e:
        cleanup_temp_dir(temp_dir)
        logger.error(f"Subtitle download error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to download subtitle: {str(e)}")

@app.post("/download")
async def download_video(request: DownloadRequest, background_tasks: BackgroundTasks):
    """Download video with optional subtitle burning"""
    temp_dir = tempfile.mkdtemp()
    logger.info(f"Created temp directory: {temp_dir}")
    
    try:
        url = str(request.url)
        format_info = request.format
        subtitle_lang = request.subtitle_lang
        
        logger.info(f"Download request - URL: {url}, Format: {format_info.format_id}, Subtitle: {subtitle_lang}")
        
        # Get video info for filename
        info = get_video_info(url)
        title = info.get('title', 'video')
        # Clean filename
        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip()[:50]
        
        # Download based on format type
        if format_info.ext == 'mp3':
            file_path = await download_audio(url, format_info.format_id, temp_dir)
            filename = f"{safe_title}.mp3"
            media_type = "audio/mpeg"
        else:
            file_path = await download_video_simple(
                url, format_info.format_id, temp_dir
            )
            # Preserve original extension (mp4 or webm)
            ext = os.path.splitext(file_path)[1][1:] or format_info.ext
            filename = f"{safe_title}.{ext}"
            media_type = f"video/{ext}"
        
        # Verify file exists and has content
        if not os.path.exists(file_path):
            raise HTTPException(status_code=500, detail="Download failed - file not created")
        
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            raise HTTPException(status_code=500, detail="Download failed - file is empty")
        
        logger.info(f"Preparing to stream file: {file_path} ({file_size} bytes)")
        
        # Add cleanup task
        background_tasks.add_task(cleanup_temp_dir, temp_dir)
        
        # Use FileResponse for better compatibility
        return FileResponse(
            file_path,
            media_type=media_type,
            filename=filename,
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            }
        )
    
    except HTTPException:
        cleanup_temp_dir(temp_dir)
        raise
    except Exception as e:
        cleanup_temp_dir(temp_dir)
        logger.error(f"Download error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

@app.get("/")
async def root():
    """API root endpoint"""
    ffmpeg_status = "available" if check_ffmpeg() else "not found"
    return {
        "message": "FetchVid API",
        "version": "1.0.0",
        "ffmpeg": ffmpeg_status,
        "endpoints": {
            "fetch": "/fetch",
            "download": "/download",
            "download-subtitle": "/download-subtitle",
            "health": "/health"
        }
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    ffmpeg_available = check_ffmpeg()
    return {
        "status": "healthy",
        "ffmpeg": ffmpeg_available,
        "python": sys.version
    }

if __name__ == "__main__":
    import uvicorn
    # Check FFmpeg on startup
    if not check_ffmpeg():
        logger.warning("FFmpeg not found. Subtitle burning will not be available.")
    uvicorn.run(app, host="0.0.0.0", port=8000)