from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
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
import json
import time
from datetime import datetime

# Import our new modules
from extractors import extractor, extract_with_retries
from cache_manager import cache_manager, session_manager, periodic_cleanup

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global progress storage for SSE
progress_store = {}

# Initialize FastAPI app
app = FastAPI(
    title="FetchVid API",
    description="YouTube video downloader with subtitle burning capabilities",
    version="2.0.0"  # Updated version with optimizations
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
    use_cache: Optional[bool] = True  # Allow bypassing cache if needed
    session_id: Optional[str] = None  # For tracking users

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
    session_id: Optional[str] = None  # For tracking users

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

async def get_video_info_with_cache(url: str, use_cache: bool = True, progress_callback=None) -> Dict[str, Any]:
    """Extract video information with caching and retry logic"""
    # Check cache first
    if use_cache:
        cached = await cache_manager.get(url)
        if cached:
            logger.info(f"Using cached info for: {url}")
            return cached
    
    # If not in cache or cache disabled, extract with retries
    try:
        info = await extract_with_retries(url, max_retries=2, progress_callback=progress_callback)
        
        # Cache the result
        if info and use_cache:
            await cache_manager.set(url, info, ttl=300)  # Cache for 5 minutes
        
        return info
    except Exception as e:
        logger.error(f"Failed to extract video info after retries: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Failed to extract video information: {str(e)}")

def filter_formats_enhanced(formats: List[Dict]) -> Dict[str, List[FormatInfo]]:
    """Enhanced format filtering with better fallbacks and quality detection"""
    grouped = {
        'mp4': {},
        'webm': {},
        'audio': [],
        'm4a': []  # Add m4a support
    }
    
    # Known good format IDs that usually work
    PREFERRED_VIDEO_FORMATS = {
        '22': '720p',   # mp4 720p with audio
        '18': '360p',   # mp4 360p with audio
        '137': '1080p', # mp4 1080p video only
        '136': '720p',  # mp4 720p video only
        '135': '480p',  # mp4 480p video only
        '134': '360p',  # mp4 360p video only
        '248': '1080p', # webm 1080p video only
        '247': '720p',  # webm 720p video only
        '244': '480p',  # webm 480p video only
        '243': '360p',  # webm 360p video only
    }
    
    PREFERRED_AUDIO_FORMATS = ['140', '141', '171', '172', '249', '250', '251', '139', '258', '256']
    
    # Process all formats
    for f in formats:
        format_id = f.get('format_id', '')
        if not format_id or not f.get('ext'):
            continue
            
        ext = f.get('ext', '').lower()
        vcodec = f.get('vcodec', 'none')
        acodec = f.get('acodec', 'none')
        height = f.get('height')
        fps = f.get('fps', 0)
        filesize = f.get('filesize', 0) or f.get('filesize_approx', 0) or 0
        tbr = f.get('tbr', 0) or f.get('abr', 0) or 0
        
        # Prioritize known good formats
        if format_id in PREFERRED_VIDEO_FORMATS and vcodec != 'none':
            quality = PREFERRED_VIDEO_FORMATS[format_id]
            if fps and fps > 30:
                quality = f"{quality}{int(fps)}"
            
            format_info = FormatInfo(
                quality=quality,
                ext='mp4' if ext in ['mp4', 'm4a'] else ext,
                format_id=format_id,
                filesize=filesize
            )
            
            target_ext = 'mp4' if ext in ['mp4', 'm4a'] else 'webm'
            grouped[target_ext][quality] = {
                'format_info': format_info,
                'tbr': tbr,
                'priority': 1  # Higher priority for known formats
            }
        
        # Audio formats
        elif vcodec == 'none' and acodec != 'none':
            if format_id in PREFERRED_AUDIO_FORMATS or ext in ['m4a', 'mp3', 'opus', 'webm']:
                grouped['audio'].append(FormatInfo(
                    quality='Audio Only (High Quality)' if format_id in ['140', '141'] else 'Audio Only',
                    ext='mp3',
                    format_id=format_id,
                    filesize=filesize
                ))
        
        # Regular video formats
        elif vcodec != 'none' and height:
            quality = f"{height}p"
            if fps and fps > 30:
                quality = f"{height}p{int(fps)}"
            
            format_info = FormatInfo(
                quality=quality,
                ext=ext if ext in ['mp4', 'webm'] else 'mp4',
                format_id=format_id,
                filesize=filesize
            )
            
            target_ext = 'mp4' if ext in ['mp4', 'm4a', 'mov'] else 'webm'
            
            # Only add if better than existing or doesn't exist
            if quality not in grouped[target_ext] or tbr > grouped[target_ext][quality].get('tbr', 0):
                grouped[target_ext][quality] = {
                    'format_info': format_info,
                    'tbr': tbr,
                    'priority': 0
                }
    
    # Convert to result format
    result = {
        'mp4': [],
        'webm': [],
        'audio': []
    }
    
    # Sort function for quality
    def get_resolution_sort_key(format_info):
        import re
        match = re.match(r'(\d+)p(\d*)', format_info.quality)
        if match:
            height = int(match.group(1))
            fps = int(match.group(2)) if match.group(2) else 30
            return height * 1000 + fps  # Prioritize resolution, then fps
        return 0
    
    # Process video formats
    for ext in ['mp4', 'webm']:
        if grouped[ext]:
            # Sort by priority first, then bitrate
            sorted_formats = sorted(
                grouped[ext].values(),
                key=lambda x: (x.get('priority', 0), x.get('tbr', 0)),
                reverse=True
            )
            
            # Extract format info and remove duplicates
            seen_qualities = set()
            for item in sorted_formats:
                if item['format_info'].quality not in seen_qualities:
                    result[ext].append(item['format_info'])
                    seen_qualities.add(item['format_info'].quality)
            
            # Sort by resolution
            result[ext].sort(key=get_resolution_sort_key, reverse=True)
    
    # Deduplicate and sort audio formats
    if grouped['audio']:
        seen_ids = set()
        unique_audio = []
        for audio in grouped['audio']:
            if audio.format_id not in seen_ids:
                unique_audio.append(audio)
                seen_ids.add(audio.format_id)
        result['audio'] = unique_audio[:5]  # Limit to 5 audio formats
    
    # Ensure at least one format of each type if possible
    if not result['mp4'] and not result['webm']:
        # Try to find ANY video format
        for f in formats:
            if f.get('vcodec') != 'none' and f.get('height'):
                result['mp4'] = [FormatInfo(
                    quality=f"{f.get('height', 'unknown')}p",
                    ext='mp4',
                    format_id=f.get('format_id'),
                    filesize=f.get('filesize')
                )]
                break
    
    if not result['audio']:
        # Try harder to find audio
        for f in formats:
            if f.get('acodec') != 'none':
                result['audio'] = [FormatInfo(
                    quality='Audio Only',
                    ext='mp3',
                    format_id=f.get('format_id'),
                    filesize=f.get('filesize')
                )]
                break
    
    # Remove empty groups
    result = {k: v for k, v in result.items() if v}
    
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
async def fetch_video_info(request: FetchRequest, req: Request):
    """Fetch video information with smart delays and caching"""
    try:
        url = str(request.url)
        logger.info(f"Fetching info for URL: {url}")
        
        # Get or create session
        client_ip = req.client.host if req.client else "unknown"
        user_agent = req.headers.get("user-agent", "unknown")
        session = await session_manager.get_or_create_session(client_ip, user_agent)
        session_id = session['id']
        
        # Check if user should see delays (for ads)
        show_delays = await session_manager.should_show_delay(session_id)
        
        # Progress tracking ID
        progress_id = f"{session_id}_{int(time.time())}"
        progress_store[progress_id] = []
        
        async def update_progress(data):
            """Update progress for SSE"""
            if progress_id in progress_store:
                progress_store[progress_id].append(data)
                
            # Add smart delays for ad display
            if show_delays and data.get('status') == 'extracting':
                delay_map = {
                    'Trying extraction method 1': 3,  # Initial delay for first ad
                    'Trying extraction method 2': 4,  # Longer delay for interstitial
                    'Trying extraction method 3': 3,  # Another ad opportunity
                }
                
                for key, delay in delay_map.items():
                    if key in data.get('message', ''):
                        await asyncio.sleep(delay)
                        break
        
        # Smart delay phase 1: "Initializing servers..."
        if show_delays:
            progress_store[progress_id].append({
                'status': 'initializing',
                'message': 'Initializing download servers...',
                'progress': 10
            })
            await asyncio.sleep(3)  # 3 seconds for initial banner ads to load
        
        # Smart delay phase 2: "Analyzing video..."
        if show_delays:
            progress_store[progress_id].append({
                'status': 'analyzing',
                'message': 'Analyzing video content...',
                'progress': 25
            })
            await asyncio.sleep(4)  # 4 seconds for interstitial ad
        
        # Get video info with cache and retries
        info = await get_video_info_with_cache(
            url, 
            use_cache=request.use_cache,
            progress_callback=update_progress if show_delays else None
        )
        
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
        
        # Smart delay phase 3: "Processing formats..."
        if show_delays:
            progress_store[progress_id].append({
                'status': 'processing',
                'message': 'Processing available formats...',
                'progress': 75
            })
            await asyncio.sleep(3)  # 3 seconds for native ads
        
        # Get formats with enhanced filtering
        formats = info.get('formats', [])
        grouped_formats = filter_formats_enhanced(formats)
        
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
        
        # Smart delay phase 4: "Finalizing..."
        if show_delays:
            progress_store[progress_id].append({
                'status': 'complete',
                'message': 'Video information ready!',
                'progress': 100
            })
            await asyncio.sleep(2)  # Final delay for any remaining ads
        
        # Clean up progress tracking
        if progress_id in progress_store:
            del progress_store[progress_id]
        
        # Track fetch in session
        session['fetch_count'] += 1
        
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

async def download_video_enhanced(url: str, format_id: str, temp_dir: str, show_progress: bool = False) -> str:
    """Enhanced video download with multiple fallback strategies"""
    try:
        video_file = os.path.join(temp_dir, "video")
        
        # Use robust download options from extractor
        download_opts = extractor.get_robust_download_opts(format_id, video_file)
        
        # Add progress hook if needed
        if show_progress:
            download_opts['progress_hooks'] = [lambda d: logger.info(f"Download progress: {d.get('status')}")]
        
        logger.info(f"Downloading with enhanced options: format {format_id}")
        
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

async def download_audio_enhanced(url: str, format_id: str, temp_dir: str) -> str:
    """Enhanced audio download with better format handling"""
    try:
        audio_file = os.path.join(temp_dir, "audio")
        
        # Use enhanced audio download options
        download_opts = extractor.get_audio_download_opts(format_id, audio_file)
        
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
async def download_video(request: DownloadRequest, req: Request, background_tasks: BackgroundTasks):
    """Enhanced download with smart delays and better reliability"""
    temp_dir = tempfile.mkdtemp()
    logger.info(f"Created temp directory: {temp_dir}")
    
    try:
        url = str(request.url)
        format_info = request.format
        subtitle_lang = request.subtitle_lang
        
        # Get session for tracking
        client_ip = req.client.host if req.client else "unknown"
        user_agent = req.headers.get("user-agent", "unknown")
        session = await session_manager.get_or_create_session(client_ip, user_agent)
        session_id = session['id']
        
        # Check rate limits
        rate_status = await session_manager.get_rate_limit_status(session_id)
        if rate_status['limited']:
            raise HTTPException(
                status_code=429, 
                detail=f"Daily download limit reached. Resets at {rate_status['reset_time']}"
            )
        
        # Show smart delays for ads
        show_delays = await session_manager.should_show_delay(session_id)
        
        if show_delays:
            # Delay 1: "Preparing download..."
            await asyncio.sleep(3)
            # This is where frontend shows first ad
        
        logger.info(f"Download request - URL: {url}, Format: {format_info.format_id}, Subtitle: {subtitle_lang}")
        
        # Get video info for filename
        info = get_video_info(url)
        title = info.get('title', 'video')
        # Clean filename
        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip()[:50]
        
        if show_delays:
            # Delay 2: "Connecting to servers..."
            await asyncio.sleep(4)
            # This is where frontend shows interstitial
        
        # Download based on format type with enhanced methods
        if format_info.ext == 'mp3':
            file_path = await download_audio_enhanced(url, format_info.format_id, temp_dir)
            filename = f"{safe_title}.mp3"
            media_type = "audio/mpeg"
        else:
            file_path = await download_video_enhanced(
                url, format_info.format_id, temp_dir, show_progress=True
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
        
        # Track download in session
        await session_manager.increment_download(session_id)
        
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
    cache_stats = cache_manager.get_stats()
    return {
        "status": "healthy",
        "ffmpeg": ffmpeg_available,
        "python": sys.version,
        "cache": cache_stats
    }

@app.get("/progress/{progress_id}")
async def get_progress(progress_id: str):
    """Get progress updates for a specific operation"""
    if progress_id in progress_store:
        return JSONResponse(content={"progress": progress_store[progress_id]})
    return JSONResponse(content={"error": "Progress ID not found"}, status_code=404)

@app.post("/track-ad")
async def track_ad_view(req: Request):
    """Track when user views an ad (for fast lane access)"""
    try:
        client_ip = req.client.host if req.client else "unknown"
        user_agent = req.headers.get("user-agent", "unknown")
        session = await session_manager.get_or_create_session(client_ip, user_agent)
        
        await session_manager.increment_ad_view(session['id'])
        
        # Check if user now has fast lane access
        has_bypass = not await session_manager.should_show_delay(session['id'])
        
        return {
            "success": True,
            "ad_count": session['ad_views'],
            "fast_lane": has_bypass,
            "message": "Watch 3 ads for 30 minutes of fast downloads!" if not has_bypass else "Fast lane activated!"
        }
    except Exception as e:
        logger.error(f"Error tracking ad: {str(e)}")
        return {"success": False}

@app.get("/session-status")
async def get_session_status(req: Request):
    """Get current session status including rate limits"""
    try:
        client_ip = req.client.host if req.client else "unknown"
        user_agent = req.headers.get("user-agent", "unknown")
        session = await session_manager.get_or_create_session(client_ip, user_agent)
        
        rate_status = await session_manager.get_rate_limit_status(session['id'])
        show_delays = await session_manager.should_show_delay(session['id'])
        
        return {
            "session_id": session['id'],
            "downloads_today": session['daily_downloads'],
            "downloads_remaining": rate_status['remaining'],
            "rate_limited": rate_status['limited'],
            "show_delays": show_delays,
            "ad_views": session['ad_views'],
            "fast_lane": not show_delays,
            "created_at": session['created_at'].isoformat(),
            "last_seen": session['last_seen'].isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting session status: {str(e)}")
        return {"error": str(e)}

@app.on_event("startup")
async def startup_event():
    """Initialize background tasks on startup"""
    logger.info("Starting FetchVid API v2.0 with optimizations")
    
    # Start periodic cleanup task
    asyncio.create_task(periodic_cleanup())
    
    # Check FFmpeg
    if not check_ffmpeg():
        logger.warning("FFmpeg not found. Some features may not work.")
    else:
        logger.info("FFmpeg is available")
    
    logger.info("API started successfully")

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("Shutting down FetchVid API")
    await cache_manager.clear()
    logger.info("Cache cleared")

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting FetchVid API v2.0 - Optimized Edition")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")