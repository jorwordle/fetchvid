import yt_dlp
import random
import logging
from typing import Dict, Any, Optional, List
import time
import asyncio
from functools import lru_cache

logger = logging.getLogger(__name__)

# User agents for rotation
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
]

class VideoExtractor:
    def __init__(self):
        self.attempt_count = 0
        self.last_error = None
        
    def get_base_opts(self) -> Dict[str, Any]:
        """Base configuration for yt-dlp"""
        return {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'skip_download': True,
            'no_check_certificate': True,
            'socket_timeout': 10,
            'retries': 3,
            'fragment_retries': 3,
            'ignoreerrors': False,
            'prefer_insecure': True,
        }
    
    def get_config_basic(self) -> Dict[str, Any]:
        """Basic configuration - fastest"""
        opts = self.get_base_opts()
        opts.update({
            'user_agent': random.choice(USER_AGENTS),
        })
        return opts
    
    def get_config_with_cookies(self) -> Dict[str, Any]:
        """Configuration with cookie support for age-restricted content"""
        opts = self.get_base_opts()
        opts.update({
            'user_agent': random.choice(USER_AGENTS),
            'age_limit': None,
            'cookiesfrombrowser': 'chrome',  # Try to use Chrome cookies
        })
        return opts
    
    def get_config_android(self) -> Dict[str, Any]:
        """Android client configuration - often bypasses restrictions"""
        opts = self.get_base_opts()
        opts.update({
            'user_agent': 'com.google.android.youtube/17.36.4 (Linux; U; Android 12; GB) gzip',
            'extractor_args': {
                'youtube': {
                    'player_client': ['android'],
                    'player_skip': ['webpage', 'configs'],
                }
            }
        })
        return opts
    
    def get_config_ios(self) -> Dict[str, Any]:
        """iOS client configuration - alternative bypass"""
        opts = self.get_base_opts()
        opts.update({
            'user_agent': 'com.google.ios.youtube/17.33.2 (iPhone14,3; U; CPU iOS 15_6 like Mac OS X)',
            'extractor_args': {
                'youtube': {
                    'player_client': ['ios'],
                    'player_skip': ['webpage', 'configs'],
                }
            }
        })
        return opts
    
    def get_config_embedded(self) -> Dict[str, Any]:
        """Embedded player configuration - works for many restricted videos"""
        opts = self.get_base_opts()
        opts.update({
            'user_agent': random.choice(USER_AGENTS),
            'extractor_args': {
                'youtube': {
                    'player_client': ['web_embedded'],
                    'player_skip': ['configs'],
                }
            },
            'referer': 'https://www.youtube.com/',
        })
        return opts
    
    def get_config_tv(self) -> Dict[str, Any]:
        """TV client configuration - minimal restrictions"""
        opts = self.get_base_opts()
        opts.update({
            'user_agent': 'Mozilla/5.0 (SMART-TV; Linux; Tizen 5.5) AppleWebKit/538.1 (KHTML, like Gecko) Version/5.5 TV Safari/538.1',
            'extractor_args': {
                'youtube': {
                    'player_client': ['tv_embedded'],
                    'player_skip': ['webpage'],
                }
            }
        })
        return opts
    
    def get_config_with_proxy(self, proxy: Optional[str] = None) -> Dict[str, Any]:
        """Configuration with proxy support for geo-blocked content"""
        opts = self.get_base_opts()
        opts.update({
            'user_agent': random.choice(USER_AGENTS),
            'proxy': proxy or '',  # Add proxy support if provided
            'geo_bypass': True,
            'geo_bypass_country': 'US',
        })
        return opts
    
    async def extract_with_config(self, url: str, config: Dict[str, Any], config_name: str) -> Optional[Dict[str, Any]]:
        """Try to extract video info with a specific configuration"""
        try:
            logger.info(f"Attempting extraction with {config_name} configuration")
            with yt_dlp.YoutubeDL(config) as ydl:
                info = await asyncio.get_event_loop().run_in_executor(
                    None, ydl.extract_info, url, False
                )
                if info:
                    logger.info(f"Successfully extracted with {config_name}")
                    info['extraction_method'] = config_name
                    return info
        except Exception as e:
            logger.warning(f"Failed with {config_name}: {str(e)}")
            self.last_error = str(e)
            return None
    
    async def extract_video_info(self, url: str, progress_callback=None) -> Dict[str, Any]:
        """
        Extract video info with multiple fallback strategies
        progress_callback: Optional function to report progress (for frontend updates)
        """
        strategies = [
            ("basic", self.get_config_basic()),
            ("android", self.get_config_android()),
            ("embedded", self.get_config_embedded()),
            ("ios", self.get_config_ios()),
            ("tv", self.get_config_tv()),
            ("cookies", self.get_config_with_cookies()),
        ]
        
        for i, (name, config) in enumerate(strategies):
            self.attempt_count = i + 1
            
            if progress_callback:
                progress_callback({
                    'status': 'extracting',
                    'message': f'Trying extraction method {i+1} of {len(strategies)}...',
                    'progress': (i / len(strategies)) * 100
                })
            
            # Add delay between attempts (smart delay for ads)
            if i > 0:
                await asyncio.sleep(2)  # 2 second delay between attempts
            
            result = await self.extract_with_config(url, config, name)
            if result:
                return result
        
        # If all strategies fail, raise an exception
        raise Exception(f"Failed to extract video after {self.attempt_count} attempts. Last error: {self.last_error}")
    
    def get_robust_download_opts(self, format_id: str, output_path: str) -> Dict[str, Any]:
        """Get robust download options with fallbacks"""
        # Try multiple format selection strategies
        format_selectors = [
            f'{format_id}+bestaudio[ext=m4a]/best[ext=mp4]/best',
            f'{format_id}+bestaudio/best',
            f'{format_id}',
            'best[ext=mp4]/best',
            'best',
        ]
        
        return {
            'format': format_selectors[0],  # Start with best option
            'format_sort': ['quality', 'res', 'fps', 'codec:vp9'],  # Prefer higher quality
            'format_sort_force': True,
            'outtmpl': output_path,
            'quiet': False,
            'no_warnings': False,
            'no_check_certificate': True,
            'prefer_ffmpeg': True,
            'merge_output_format': 'mp4',
            'keepvideo': False,
            'user_agent': random.choice(USER_AGENTS),
            'retries': 5,
            'fragment_retries': 5,
            'skip_unavailable_fragments': True,
            'ignoreerrors': False,
            'continuedl': True,
            'noprogress': False,
            'ratelimit': None,  # No rate limit for faster downloads
            'http_chunk_size': 10485760,  # 10MB chunks
        }
    
    def get_audio_download_opts(self, format_id: str, output_path: str) -> Dict[str, Any]:
        """Get optimized options for audio downloads"""
        return {
            'format': f'{format_id}/bestaudio[ext=m4a]/bestaudio',
            'outtmpl': output_path,
            'quiet': False,
            'no_warnings': False,
            'no_check_certificate': True,
            'prefer_ffmpeg': True,
            'user_agent': random.choice(USER_AGENTS),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'keepvideo': False,
            'retries': 5,
            'fragment_retries': 5,
        }
    
    def get_subtitle_download_opts(self, subtitle_lang: str, output_path: str) -> Dict[str, Any]:
        """Get options for subtitle downloads"""
        return {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': [subtitle_lang, f'{subtitle_lang}-orig', 'en'],  # Fallback to English
            'subtitlesformat': 'srt/vtt/best',
            'outtmpl': output_path,
            'quiet': True,
            'no_warnings': True,
            'user_agent': random.choice(USER_AGENTS),
        }

# Singleton instance
extractor = VideoExtractor()

async def extract_with_retries(url: str, max_retries: int = 3, progress_callback=None) -> Dict[str, Any]:
    """
    Main extraction function with retries and progress reporting
    """
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                # Wait longer between retries (good for ads)
                await asyncio.sleep(3 * attempt)
                
            if progress_callback:
                progress_callback({
                    'status': 'retrying' if attempt > 0 else 'starting',
                    'message': f'Attempt {attempt + 1} of {max_retries}',
                    'progress': (attempt / max_retries) * 100
                })
            
            result = await extractor.extract_video_info(url, progress_callback)
            return result
            
        except Exception as e:
            logger.error(f"Extraction attempt {attempt + 1} failed: {str(e)}")
            if attempt == max_retries - 1:
                raise
    
    raise Exception("Max retries exceeded")